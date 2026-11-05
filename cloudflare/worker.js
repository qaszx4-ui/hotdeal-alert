// 핫딜 키워드 알림 — Cloudflare Worker
// - 2분마다(Cron Trigger) 뽐뿌게시판 RSS + 텔레그램 채널 뽐질(t.me/s/jirum)을 확인
// - 텔레그램 명령은 webhook(/tg)으로 즉시 처리: /add /del /list /check /help, +키워드, -키워드
// - 저장: KV(바인딩 이름 STATE)
//     "config" = {keywords, chat_id}      ← 텔레그램 명령만 씀
//     "seen"   = {사이트별 본 글 번호·차단 상태} ← 정기 확인만 씀
//   (KV 무료 한도가 하루 쓰기 1,000회라서 두 키로 나누고, 바뀔 때만 씀)
// - 시크릿: TELEGRAM_TOKEN
// - 처음 실행 때 KV가 비어 있으면 GitHub 버전의 state.json에서 키워드·연결 정보를 가져옴

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";
const SEEN_MAX = 400;              // 사이트별로 기억할 최근 글 번호 수
const BLOCK_BACKOFF_MS = 3600_000; // 접속 실패 시 1시간 쉼
const MANUAL_MIN_GAP_MS = 60_000;  // /check, /run 연타 방지
const JIRUM_MAX_PAGES = 5;         // 채널 한 페이지 20개, 몰릴 때 이전 글까지 거슬러 봄
const IMPORT_URL = "https://raw.githubusercontent.com/qaszx4-ui/hotdeal-alert/main/state.json";

const HELP = [
  "명령어",
  "/add 키워드  (쉼표로 여러 개: /add 에어팟, 닌텐도)",
  "/del 키워드",
  "/list",
  "/check  지금 바로 확인",
  "줄여서 +키워드 / -키워드 도 됨",
  "대상: 뽐뿌게시판, 텔레그램 뽐질(t.me/jirum) · 2분마다 확인",
].join("\n");

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runCheck(env, false));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/tg" && request.method === "POST") {
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== await hookSecret(env)) {
        return new Response("forbidden", { status: 403 });
      }
      await handleUpdate(env, await request.json());
      return new Response("ok");
    }
    if (url.pathname === "/setup") {
      // 텔레그램에 이 Worker 주소를 webhook으로 등록 (여러 번 불러도 같은 결과)
      const res = await tg(env, "setWebhook", {
        url: url.origin + "/tg",
        secret_token: await hookSecret(env),
        allowed_updates: JSON.stringify(["message"]),
      });
      return Response.json(res);
    }
    if (url.pathname === "/run") {
      const log = await runCheck(env, true);
      return new Response(log.join("\n"), { headers: { "content-type": "text/plain; charset=utf-8" } });
    }
    return new Response("hotdeal-alert");
  },
};

// ---------- KV 상태 ----------
async function loadConfig(env) {
  let cfg = await env.STATE.get("config", "json");
  if (!cfg) {
    cfg = { keywords: [], chat_id: "" };
    try {
      const old = await (await fetch(IMPORT_URL)).json();
      cfg = { keywords: old.keywords || [], chat_id: String(old.chat_id || "") };
    } catch (e) { /* 가져오기 실패해도 새로 시작 */ }
    await env.STATE.put("config", JSON.stringify(cfg));
  }
  return cfg;
}

async function loadSeen(env) {
  const seen = (await env.STATE.get("seen", "json")) || { lastRun: 0 };
  for (const name of Object.keys(SITES)) {
    seen[name] ||= { seen: [], blocked: false, blocked_until: 0 };
  }
  return seen;
}

// ---------- 텔레그램 ----------
async function tg(env, method, params) {
  const res = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/${method}`, {
    method: "POST",
    body: new URLSearchParams(params),
  });
  return res.json();
}

function send(env, chatId, text) {
  return tg(env, "sendMessage", { chat_id: chatId, text, disable_web_page_preview: "true" });
}

async function hookSecret(env) {
  // 토큰에서 만든 값이라 시크릿을 따로 넣을 필요 없음
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("hook:" + env.TELEGRAM_TOKEN));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 48);
}

function kwListText(cfg) {
  return cfg.keywords.length ? "현재 키워드: " + cfg.keywords.join(", ") : "등록된 키워드 없음. /add 키워드 로 추가";
}

async function handleUpdate(env, upd) {
  const msg = upd.message || {};
  const chat = msg.chat || {};
  const cfg = await loadConfig(env);

  if (!cfg.chat_id && chat.type === "private") {
    cfg.chat_id = String(chat.id); // 봇에게 처음 말 건 사람과 자동 연결
    await env.STATE.put("config", JSON.stringify(cfg));
    await send(env, cfg.chat_id, "✅ 연결 완료. 이제 이 채팅으로 알림이 옴\n\n" + HELP);
    return;
  }
  if (String(chat.id) !== cfg.chat_id) return; // 내 채팅 외에는 무시

  const text = (msg.text || "").trim();
  let cmd, arg;
  if (text.startsWith("+")) [cmd, arg] = ["/add", text.slice(1)];
  else if (text.startsWith("-")) [cmd, arg] = ["/del", text.slice(1)];
  else {
    const i = text.indexOf(" ");
    cmd = (i < 0 ? text : text.slice(0, i)).split("@")[0].toLowerCase();
    arg = i < 0 ? "" : text.slice(i + 1);
  }
  const words = arg.split(",").map((w) => w.trim()).filter(Boolean);

  if (cmd === "/add" && words.length) {
    for (const w of words) if (!cfg.keywords.includes(w)) cfg.keywords.push(w);
    await env.STATE.put("config", JSON.stringify(cfg));
    await send(env, cfg.chat_id, "추가됨: " + words.join(", ") + "\n" + kwListText(cfg));
  } else if (cmd === "/del" && words.length) {
    const removed = words.filter((w) => cfg.keywords.includes(w));
    cfg.keywords = cfg.keywords.filter((k) => !words.includes(k));
    await env.STATE.put("config", JSON.stringify(cfg));
    await send(env, cfg.chat_id, (removed.length ? "삭제됨: " + removed.join(", ") : "없는 키워드") + "\n" + kwListText(cfg));
  } else if (cmd === "/list") {
    await send(env, cfg.chat_id, kwListText(cfg));
  } else if (cmd === "/check") {
    const log = await runCheck(env, true);
    await send(env, cfg.chat_id, "확인함\n" + log.join("\n"));
  } else {
    await send(env, cfg.chat_id, HELP);
  }
}

// ---------- 공통 ----------
async function fetchText(url, referer) {
  const headers = { "User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9" };
  if (referer) headers.Referer = referer;
  const res = await fetch(url, { headers });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.text();
}

function decode(s) {
  return s
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number(d)))
    .replace(/&nbsp;/g, " ").replace(/&quot;/g, '"').replace(/&#39;|&apos;/g, "'")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
}

function clean(s) {
  return decode(s.replace(/<[^>]+>/g, "")).trim();
}

// ---------- 뽐뿌게시판 (공식 RSS, 최근 15개) ----------
// 게시판 HTML은 봇 요청을 403으로 막아서 RSS를 사용.
const PPOM_RSS = "https://www.ppomppu.co.kr/rss.php?id=ppomppu";

function parsePpomppu(xml) {
  const items = [];
  for (const block of xml.split("<item>").slice(1)) {
    const title = clean((block.match(/<title>([\s\S]*?)<\/title>/) || [])[1] || "");
    const no = (block.match(/<link>[^<]*[?&;]no=(\d+)/) || [])[1];
    if (!no || !title) continue;
    const shop = (title.match(/^\s*\[([^\]]+)\]/) || [])[1] || "";
    items.push({
      id: Number(no),
      title, // 제목에 [쇼핑몰]·(가격/배송)이 이미 들어 있음
      shop,
      body: "",
      url: `https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&no=${no}`,
    });
  }
  return items;
}

async function getPpomppu() {
  const items = parsePpomppu(await fetchText(PPOM_RSS, "https://www.ppomppu.co.kr/"));
  if (!items.length) throw new Error("RSS를 못 읽음 (차단 또는 형식 변경 의심)");
  return items;
}

// ---------- 텔레그램 채널 뽐질 (t.me/s/jirum 공개 미리보기) ----------
const JIRUM_URL = "https://t.me/s/jirum";

function parseJirum(page) {
  const items = [];
  for (const block of page.split('data-post="jirum/').slice(1)) {
    const id = (block.match(/^(\d+)/) || [])[1];
    const m = block.match(/class="tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)<\/div>/);
    if (!id || !m) continue;
    const text = clean(m[1].replace(/<br\s*\/?>/g, "\n"));
    // 제휴 수수료 안내 문구(✱ 이 포스팅은…, 이 포스팅은 쿠팡 파트너스…)와 링크 주소는 키워드 비교에서 뺌
    const lines = text.split("\n").map((l) => l.trim())
      .filter((l) => l && !l.startsWith("✱") && !l.includes("이 포스팅은") && !/^https?:\/\//.test(l));
    const head = lines.slice(0, 2).map((l) => l.replaceAll("🔥", "").trim()).filter(Boolean);
    items.push({
      id: Number(id),
      title: head.join("\n"),
      shop: "",
      body: lines.join("\n"),
      url: `https://t.me/jirum/${id}`,
    });
  }
  return items;
}

async function getJirum(seenSet) {
  const found = new Map();
  let url = JIRUM_URL;
  for (let i = 0; i < JIRUM_MAX_PAGES; i++) {
    const items = parseJirum(await fetchText(url));
    if (!items.length) {
      if (i) break;
      throw new Error("채널 글을 못 읽음 (차단 또는 형식 변경 의심)");
    }
    for (const it of items) found.set(it.id, it);
    if (!seenSet.size || items.some((it) => seenSet.has(it.id))) break;
    url = `${JIRUM_URL}?before=${Math.min(...items.map((it) => it.id))}`;
  }
  return [...found.values()];
}

// 이름: [텔레그램 표시명, 가져오기 함수]
const SITES = {
  ppomppu: ["뽐뿌", getPpomppu],
  jirum: ["뽐질(텔레그램)", getJirum],
};

// ---------- 매칭·알림 ----------
const norm = (s) => s.replace(/\s+/g, "").toLowerCase();

function matched(item, keywords) {
  const target = norm([item.title, item.shop, item.body].join(" "));
  return keywords.filter((k) => norm(k) && target.includes(norm(k)));
}

function formatItem(label, item, hits) {
  return [`🔔 ${label} [${hits.join(", ")}]`, item.title, item.url].join("\n");
}

async function runCheck(env, manual) {
  const log = [];
  const cfg = await loadConfig(env);
  if (!cfg.chat_id) return ["아직 연결 안 됨: 텔레그램에서 봇에게 아무 말이나 보내면 연결됨"];
  const state = await loadSeen(env);
  const before = JSON.stringify(state);
  const now = Date.now();
  if (manual && now - state.lastRun < MANUAL_MIN_GAP_MS) return ["방금 확인했음. 1분 뒤에 다시"];
  state.lastRun = now;

  for (const [name, [label, getter]] of Object.entries(SITES)) {
    const ss = state[name];
    if (now < ss.blocked_until) { log.push(`${label}: 접속 실패로 쉬는 중`); continue; }
    const seenSet = new Set(ss.seen);
    let items;
    try {
      items = await getter(seenSet);
    } catch (e) {
      log.push(`${label}: 접속 실패 ${e.message}`);
      if (!ss.blocked) await send(env, cfg.chat_id, `⚠️ ${label} 접속 실패: ${e.message}\n1시간 쉬고 다시 시도함`);
      ss.blocked = true;
      ss.blocked_until = now + BLOCK_BACKOFF_MS;
      continue;
    }
    if (ss.blocked) {
      await send(env, cfg.chat_id, `✅ ${label} 접속 복구됨`);
      ss.blocked = false;
    }
    const firstRun = seenSet.size === 0;
    const fresh = items.filter((it) => !seenSet.has(it.id)).sort((a, b) => a.id - b.id);
    ss.seen = [...new Set([...ss.seen, ...items.map((it) => it.id)])].sort((a, b) => a - b).slice(-SEEN_MAX);
    log.push(`${label}: 새 글 ${firstRun ? 0 : fresh.length}개`);
    if (firstRun) continue; // 첫 실행은 기준점만 잡고 알림 안 보냄
    for (const it of fresh) {
      const hits = matched(it, cfg.keywords);
      if (hits.length) {
        await send(env, cfg.chat_id, formatItem(label, it, hits));
        log.push("  알림: " + it.title.split("\n").join(" / "));
      }
    }
  }
  // 정기 실행은 새 글이 없으면 KV에 쓰지 않음 (lastRun만 바뀐 경우 제외)
  const changed = JSON.stringify({ ...state, lastRun: 0 }) !== JSON.stringify({ ...JSON.parse(before), lastRun: 0 });
  if (changed || manual) await env.STATE.put("seen", JSON.stringify(state));
  return log;
}

export { parsePpomppu, parseJirum, matched }; // 로컬 시험용
