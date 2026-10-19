"""핫딜 키워드 알림: 펨코 핫딜 + 뽐뿌게시판 (GitHub Actions에서 5분마다 실행).

- 텔레그램 연결: TELEGRAM_CHAT_ID가 없으면 봇에게 처음 말 건 개인 채팅과 자동 연결
- 텔레그램 명령 처리: /add 키워드, /del 키워드, /list, /help (또는 +키워드, -키워드)
- 새 글 중 제목/쇼핑몰에 키워드가 들어간 글을 텔레그램으로 알림
- 상태(키워드, 사이트별 이미 본 글 번호 등)는 state.json에 저장 → 워크플로가 커밋

로컬 파싱 테스트: python -X utf8 hotdeal_alert.py --test fmkorea|ppomppu 저장한파일 키워드1 키워드2
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
BLOCK_BACKOFF_SEC = 3600  # 차단 의심 시 1시간 쉼
SEEN_MAX = 400  # 사이트별로 기억할 최근 글 번호 수

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", ""))  # 비우면 자동 연결


# ---------- 상태 ----------
def load_state():
    st = {"keywords": [], "tg_offset": 0, "sites": {}}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            st.update(json.load(f))
    for name in SITES:
        st["sites"].setdefault(name, {"seen": [], "blocked": False, "blocked_until": 0})
    for old in ("seen", "blocked", "blocked_until"):  # 펨코 전용 시절 형식 정리
        st.pop(old, None)
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------- 텔레그램 ----------
def tg(method, **params):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=20) as r:
        return json.load(r)


def send(text):
    tg("sendMessage", chat_id=CHAT_ID, text=text, disable_web_page_preview="true")


def kw_list_text(st):
    if not st["keywords"]:
        return "등록된 키워드 없음. /add 키워드 로 추가"
    return "현재 키워드: " + ", ".join(st["keywords"])


HELP = ("명령어\n"
        "/add 키워드  (쉼표로 여러 개: /add 에어팟, 닌텐도)\n"
        "/del 키워드\n"
        "/list\n"
        "줄여서 +키워드 / -키워드 도 됨\n"
        "※ 5분 간격으로 확인하므로 답장도 몇 분 걸림\n"
        "대상: 펨코 핫딜, 뽐뿌게시판")


def set_chat_id(st, chat_id):
    global CHAT_ID
    CHAT_ID = str(chat_id)
    st["chat_id"] = CHAT_ID


def handle_commands(st):
    res = tg("getUpdates", offset=st["tg_offset"], timeout=0)
    for upd in res.get("result", []):
        st["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat = msg.get("chat", {})
        if not CHAT_ID and chat.get("type") == "private":
            set_chat_id(st, chat["id"])  # 봇에게 처음 말 건 사람과 자동 연결
            send("✅ 연결 완료. 이제 이 채팅으로 알림이 옴\n\n" + HELP)
            continue
        if str(chat.get("id")) != CHAT_ID:
            continue  # 내 채팅 외에는 무시
        text = (msg.get("text") or "").strip()
        if text.startswith("+"):
            cmd, arg = "/add", text[1:]
        elif text.startswith("-"):
            cmd, arg = "/del", text[1:]
        else:
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
        words = [w.strip() for w in arg.split(",") if w.strip()]

        if cmd == "/add" and words:
            for w in words:
                if w not in st["keywords"]:
                    st["keywords"].append(w)
            send("추가됨: " + ", ".join(words) + "\n" + kw_list_text(st))
        elif cmd == "/del" and words:
            removed = [w for w in words if w in st["keywords"]]
            st["keywords"] = [k for k in st["keywords"] if k not in words]
            send(("삭제됨: " + ", ".join(removed) if removed else "없는 키워드") + "\n" + kw_list_text(st))
        elif cmd == "/list":
            send(kw_list_text(st))
        else:
            send(HELP)


# ---------- 공통 ----------
def fetch(url, referer=None):
    headers = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


# ---------- 펨코 핫딜 ----------
FMK_BASE = "https://www.fmkorea.com"
FMK_URLS = [FMK_BASE + "/hotdeal", FMK_BASE + "/index.php?mid=hotdeal&page=2"]


def parse_fmkorea(page):
    items = []
    for chunk in re.split(r'<li class="li\s', page)[1:]:
        m = re.search(r'<h3 class="title"[^>]*>\s*<a href="/(\d+)"[^>]*>.*?'
                      r'<span class="ellipsis-target">(.*?)</span>', chunk, re.S)
        if not m:
            continue
        info = dict(re.findall(r'(쇼핑몰|가격|배송):\s*<a[^>]*>(.*?)</a>', chunk, re.S))
        shop, price, ship = (clean(info.get(k, "")) for k in ("쇼핑몰", "가격", "배송"))
        items.append({
            "id": int(m.group(1)),
            "title": clean(m.group(2)),
            "shop": shop,
            "detail": " / ".join(x for x in (shop, price, ship) if x),
            "url": f"{FMK_BASE}/{m.group(1)}",
        })
    return items


def get_fmkorea(seen):
    """1페이지가 전부 새 글이면 2페이지까지 확인 (실행 지연 대비)."""
    found = {}
    for i, url in enumerate(FMK_URLS):
        if i:
            time.sleep(3)
        items = parse_fmkorea(fetch(url))
        if not items:
            raise RuntimeError("목록을 못 읽음 (차단 또는 페이지 구조 변경 의심)")
        for it in items:
            found[it["id"]] = it
        if not seen or any(it["id"] in seen for it in items):
            break
    return list(found.values())


# ---------- 뽐뿌게시판 (공식 RSS) ----------
# 게시판 HTML은 봇 요청을 403으로 막아서 RSS를 사용. RSS는 최근 15개만 줌.
PPOM_RSS = "https://www.ppomppu.co.kr/rss.php?id=ppomppu"


def parse_ppomppu(xml_text):
    items = []
    for it in ET.fromstring(xml_text.encode("utf-8")).findall("./channel/item"):
        link = it.findtext("link") or ""
        m = re.search(r"[?&]no=(\d+)", link)
        if not m:
            continue
        title = clean(it.findtext("title") or "")
        shop = re.match(r"\s*\[([^\]]+)\]", title)
        items.append({
            "id": int(m.group(1)),
            "title": title,  # 제목에 [쇼핑몰]·(가격/배송)이 이미 들어 있음
            "shop": shop.group(1) if shop else "",
            "detail": "",
            "url": f"https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&no={m.group(1)}",
        })
    return items


def get_ppomppu(seen):
    items = parse_ppomppu(fetch(PPOM_RSS, referer="https://www.ppomppu.co.kr/"))
    if not items:
        raise RuntimeError("RSS를 못 읽음 (차단 또는 형식 변경 의심)")
    return items


# 이름: (텔레그램 표시명, 가져오기 함수, 파서)
SITES = {
    "fmkorea": ("펨코", get_fmkorea, parse_fmkorea),
    "ppomppu": ("뽐뿌", get_ppomppu, parse_ppomppu),
}


# ---------- 매칭·알림 ----------
def norm(s):
    return re.sub(r"\s+", "", s).lower()


def matched(item, keywords):
    target = norm(item["title"] + " " + item["shop"])
    return [k for k in keywords if norm(k) and norm(k) in target]


def format_item(label, item, hits):
    lines = [f"🔔 {label} [{', '.join(hits)}]", item["title"]]
    if item["detail"]:
        lines.append(item["detail"])
    lines.append(item["url"])
    return "\n".join(lines)


def check_site(name, st):
    label, getter, _ = SITES[name]
    ss = st["sites"][name]
    if time.time() < ss["blocked_until"]:
        print(f"{label}: 차단 의심으로 대기 중")
        return
    seen = set(ss["seen"])
    try:
        items = getter(seen)
    except Exception as e:  # HTTP 오류(430/403/429 등) 포함
        print(f"{label}: 접속 실패 {e}")
        if not ss["blocked"]:
            send(f"⚠️ {label} 접속 실패: {e}\n1시간 쉬고 다시 시도함")
        ss["blocked"] = True
        ss["blocked_until"] = time.time() + BLOCK_BACKOFF_SEC
        return

    if ss["blocked"]:
        send(f"✅ {label} 접속 복구됨")
        ss["blocked"] = False

    first_run = not seen
    new = sorted((it for it in items if it["id"] not in seen), key=lambda x: x["id"])
    ss["seen"] = sorted(seen | {it["id"] for it in items})[-SEEN_MAX:]
    print(f"{label}: 새 글 {0 if first_run else len(new)}개")
    if first_run:
        return  # 첫 실행은 기준점만 잡고 알림 안 보냄
    for it in new:
        hits = matched(it, st["keywords"])
        if hits:
            send(format_item(label, it, hits))
            print("  알림:", it["title"])


def main():
    if not TOKEN:
        print("TELEGRAM_TOKEN 시크릿이 아직 없음 → 건너뜀")
        return
    st = load_state()
    if not CHAT_ID and st.get("chat_id"):
        set_chat_id(st, st["chat_id"])
    try:
        handle_commands(st)
        if not CHAT_ID:
            print("아직 연결 안 됨: 텔레그램에서 봇에게 아무 말이나 보내면 다음 실행 때 연결됨")
            return
        print("키워드:", st["keywords"])
        for i, name in enumerate(SITES):
            if i:
                time.sleep(2)
            check_site(name, st)
    finally:
        save_state(st)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--test":
        with open(sys.argv[3], encoding="utf-8") as f:
            parsed = SITES[sys.argv[2]][2](f.read())
        kws = sys.argv[4:]
        for it in parsed:
            mark = "★ " if matched(it, kws) else "  "
            print(f"{mark}{it['id']} | {it['shop']} | {it['title']} | {it['detail']}")
        print(f"총 {len(parsed)}개")
    else:
        main()
