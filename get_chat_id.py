"""텔레그램 chat id 확인용. 봇에게 아무 메시지나 보낸 뒤 실행하고, 토큰을 붙여넣는다."""
import json
import urllib.request

token = input("봇 토큰 붙여넣기: ").strip()
with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20) as r:
    data = json.load(r)

ids = {(u["message"]["chat"]["id"], u["message"]["chat"].get("first_name", ""))
       for u in data.get("result", []) if "message" in u}
if not ids:
    print("메시지가 없음. 텔레그램에서 봇에게 아무 말이나 보낸 뒤 다시 실행.")
for cid, name in ids:
    print(f"chat id: {cid}  ({name})")
