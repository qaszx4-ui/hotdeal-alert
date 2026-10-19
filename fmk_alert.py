"""펨코 핫딜 키워드 알림 (GitHub Actions에서 5분마다 실행).

- 텔레그램 명령 처리: /add 키워드, /del 키워드, /list, /help (또는 +키워드, -키워드)
- 새 핫딜 글 중 제목/쇼핑몰에 키워드가 들어간 글을 텔레그램으로 알림
- 상태(키워드, 이미 본 글 번호 등)는 state.json에 저장 → 워크플로가 커밋

로컬 파싱 테스트: python fmk_alert.py --test 저장한페이지.html 키워드1 키워드2
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
BASE = "https://www.fmkorea.com"
PAGE_URLS = [BASE + "/hotdeal", BASE + "/index.php?mid=hotdeal&page=2"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
BLOCK_BACKOFF_SEC = 3600  # 차단 의심 시 1시간 쉼
SEEN_MAX = 400  # 기억할 최근 글 번호 수

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", ""))


# ---------- 상태 ----------
def load_state():
    st = {"keywords": [], "seen": [], "tg_offset": 0, "blocked": False, "blocked_until": 0}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            st.update(json.load(f))
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
        "※ 5분 간격으로 확인하므로 답장도 몇 분 걸림")


def handle_commands(st):
    res = tg("getUpdates", offset=st["tg_offset"], timeout=0)
    for upd in res.get("result", []):
        st["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != CHAT_ID:
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


# ---------- 펨코 ----------
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse(page):
    items = []
    for chunk in re.split(r'<li class="li\s', page)[1:]:
        m = re.search(r'<h3 class="title"[^>]*>\s*<a href="/(\d+)"[^>]*>.*?'
                      r'<span class="ellipsis-target">(.*?)</span>', chunk, re.S)
        if not m:
            continue
        info = dict(re.findall(r'(쇼핑몰|가격|배송):\s*<a[^>]*>(.*?)</a>', chunk, re.S))
        items.append({
            "id": int(m.group(1)),
            "title": clean(m.group(2)),
            "shop": clean(info.get("쇼핑몰", "")),
            "price": clean(info.get("가격", "")),
            "ship": clean(info.get("배송", "")),
        })
    return items


def norm(s):
    return re.sub(r"\s+", "", s).lower()


def matched(item, keywords):
    target = norm(item["title"] + " " + item["shop"])
    return [k for k in keywords if norm(k) and norm(k) in target]


def format_item(item, hits):
    return (f"🔔 [{', '.join(hits)}]\n"
            f"{item['title']}\n"
            f"{item['shop']} / {item['price']} / {item['ship']}\n"
            f"{BASE}/{item['id']}")


def get_items(seen):
    """1페이지가 전부 새 글이면 2페이지까지 확인 (실행 지연 대비)."""
    found = {}
    for i, url in enumerate(PAGE_URLS):
        if i:
            time.sleep(3)
        items = parse(fetch(url))
        if not items:
            raise RuntimeError("목록을 못 읽음 (차단 또는 페이지 구조 변경 의심)")
        for it in items:
            found[it["id"]] = it
        if not seen or any(it["id"] in seen for it in items):
            break
    return sorted(found.values(), key=lambda x: x["id"])


def main():
    if not TOKEN or not CHAT_ID:
        sys.exit("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 시크릿이 없음")
    st = load_state()
    try:
        handle_commands(st)

        if time.time() < st["blocked_until"]:
            print("차단 의심으로 대기 중")
            return
        try:
            items = get_items(set(st["seen"]))
        except Exception as e:  # HTTP 오류(430/403/429 등) 포함
            print("fetch 실패:", e)
            if not st["blocked"]:
                send(f"⚠️ 펨코 접속 실패: {e}\n1시간 쉬고 다시 시도함")
            st["blocked"] = True
            st["blocked_until"] = time.time() + BLOCK_BACKOFF_SEC
            return

        if st["blocked"]:
            send("✅ 펨코 접속 복구됨")
            st["blocked"] = False

        first_run = not st["seen"]
        seen = set(st["seen"])
        new = [it for it in items if it["id"] not in seen]
        st["seen"] = sorted(seen | {it["id"] for it in items})[-SEEN_MAX:]
        print(f"새 글 {0 if first_run else len(new)}개, 키워드 {st['keywords']}")
        if first_run:
            return  # 첫 실행은 기준점만 잡고 알림 안 보냄
        for it in new:
            hits = matched(it, st["keywords"])
            if hits:
                send(format_item(it, hits))
                print("알림:", it["title"])
    finally:
        save_state(st)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        with open(sys.argv[2], encoding="utf-8") as f:
            parsed = parse(f.read())
        kws = sys.argv[3:]
        for it in parsed:
            hits = matched(it, kws)
            print(("★ " if hits else "  ") + f"{it['id']} | {it['shop']} | {it['title']} | {it['price']} | {it['ship']}")
        print(f"총 {len(parsed)}개")
    else:
        main()
