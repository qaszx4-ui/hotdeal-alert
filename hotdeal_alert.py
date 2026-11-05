"""핫딜 키워드 알림 - 뽐뿌게시판 담당 (GitHub Actions에서 실행).

뽐뿌는 Cloudflare 서버 접속을 막아서(2026-11-05 확인) 이 스크립트만 GitHub에서 돌린다.
텔레그램 채널 뽐질과 텔레그램 명령(/add /del /list)은 Cloudflare Worker(cloudflare/worker.js)가 맡는다.

- 키워드·연결 정보: Worker의 /kw 주소에서 가져옴 (실패하면 state.json에 저장해 둔 마지막 값 사용)
- 새 글 중 제목/쇼핑몰에 키워드가 들어간 글을 텔레그램으로 알림
- 상태(이미 본 글 번호 등)는 state.json에 저장 → 워크플로가 커밋

로컬 파싱 테스트: python -X utf8 hotdeal_alert.py --test 저장한RSS.xml 키워드1 키워드2
"""
import hashlib
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
BLOCK_BACKOFF_SEC = 3600  # 접속 실패 시 1시간 쉼
SEEN_MAX = 400  # 기억할 최근 글 번호 수
WORKER_KW_URL = "https://hotdeal-alert.qaszx4.workers.dev/kw"
LABEL = "뽐뿌"

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")


# ---------- 상태 ----------
def load_state():
    st = {"keywords": [], "chat_id": "", "ppomppu": {"seen": [], "blocked": False, "blocked_until": 0}}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            old = json.load(f)
        st["keywords"] = old.get("keywords", [])
        st["chat_id"] = str(old.get("chat_id", ""))
        # 이전 형식(sites.ppomppu) 정리
        st["ppomppu"].update(old.get("ppomppu") or old.get("sites", {}).get("ppomppu", {}))
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------- 키워드 (Worker에서) ----------
def sync_config(st):
    key = hashlib.sha256(("kw:" + TOKEN).encode()).hexdigest()[:48]
    url = WORKER_KW_URL + "?" + urllib.parse.urlencode({"key": key})
    # 기본 User-Agent(Python-urllib)는 Cloudflare가 error 1010으로 막아서 이름을 붙임
    req = urllib.request.Request(url, headers={"User-Agent": "hotdeal-alert-github/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            cfg = json.load(r)
        st["keywords"] = cfg.get("keywords", [])
        if cfg.get("chat_id"):
            st["chat_id"] = str(cfg["chat_id"])
    except Exception as e:
        print("키워드 가져오기 실패, 저장된 값 사용:", e)


# ---------- 텔레그램 ----------
def send(st, text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": st["chat_id"], "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(url, data=data, timeout=20) as r:
        return json.load(r)


# ---------- 뽐뿌게시판 (공식 RSS) ----------
# 게시판 HTML은 봇 요청을 403으로 막아서 RSS를 사용. RSS는 최근 15개만 줌.
PPOM_RSS = "https://www.ppomppu.co.kr/rss.php?id=ppomppu"


def fetch(url, referer=None):
    headers = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


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
            "url": f"https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&no={m.group(1)}",
        })
    return items


def get_ppomppu():
    items = parse_ppomppu(fetch(PPOM_RSS, referer="https://www.ppomppu.co.kr/"))
    if not items:
        raise RuntimeError("RSS를 못 읽음 (차단 또는 형식 변경 의심)")
    return items


# ---------- 매칭·알림 ----------
def norm(s):
    return re.sub(r"\s+", "", s).lower()


def matched(item, keywords):
    target = norm(" ".join((item["title"], item["shop"])))
    return [k for k in keywords if norm(k) and norm(k) in target]


def check(st):
    ss = st["ppomppu"]
    if time.time() < ss["blocked_until"]:
        print(f"{LABEL}: 접속 실패로 쉬는 중")
        return
    seen = set(ss["seen"])
    try:
        items = get_ppomppu()
    except Exception as e:  # HTTP 오류(403/429 등) 포함
        print(f"{LABEL}: 접속 실패 {e}")
        if not ss["blocked"]:
            send(st, f"⚠️ {LABEL} 접속 실패: {e}\n1시간 쉬고 다시 시도함")
        ss["blocked"] = True
        ss["blocked_until"] = time.time() + BLOCK_BACKOFF_SEC
        return

    if ss["blocked"]:
        send(st, f"✅ {LABEL} 접속 복구됨")
        ss["blocked"] = False

    first_run = not seen
    new = sorted((it for it in items if it["id"] not in seen), key=lambda x: x["id"])
    ss["seen"] = sorted(seen | {it["id"] for it in items})[-SEEN_MAX:]
    print(f"{LABEL}: 새 글 {0 if first_run else len(new)}개")
    if first_run:
        return  # 첫 실행은 기준점만 잡고 알림 안 보냄
    for it in new:
        hits = matched(it, st["keywords"])
        if hits:
            send(st, "\n".join([f"🔔 {LABEL} [{', '.join(hits)}]", it["title"], it["url"]]))
            print("  알림:", it["title"])


def main():
    if not TOKEN:
        print("TELEGRAM_TOKEN 시크릿이 아직 없음 → 건너뜀")
        return
    st = load_state()
    sync_config(st)
    try:
        if not st["chat_id"]:
            print("아직 연결 안 됨: 텔레그램에서 봇에게 아무 말이나 보내면 Worker가 연결함")
            return
        print("키워드:", st["keywords"])
        check(st)
    finally:
        save_state(st)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        with open(sys.argv[2], encoding="utf-8") as f:
            parsed = parse_ppomppu(f.read())
        kws = sys.argv[3:]
        for it in parsed:
            mark = "★ " if matched(it, kws) else "  "
            print(f"{mark}{it['id']} | {it['shop']} | {it['title']}")
        print(f"총 {len(parsed)}개")
    else:
        main()
