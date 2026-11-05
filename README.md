# 핫딜 키워드 알림 (텔레그램 채널 뽐질 + 뽐뿌게시판)

텔레그램 채널 [뽐질](https://t.me/jirum)과 뽐뿌게시판에 키워드가 들어간 **새 글**이 올라오면 텔레그램으로 알려준다. PC를 꺼도 동작한다.

| 대상 | 어디서 도나 | 간격 |
|---|---|---|
| 뽐질(t.me/jirum) | Cloudflare Worker (`cloudflare/worker.js`) | 8분마다 |
| 뽐뿌게시판 | GitHub Actions (`hotdeal_alert.py`) | 5분으로 예약했지만 실제로는 몇 시간씩 늦을 때가 많음 |
| 텔레그램 명령 (/add 등) | Cloudflare Worker (webhook) | 바로 답장 |

뽐뿌만 GitHub에 남은 이유: 뽐뿌가 Cloudflare 서버의 접속을 막는다(2026-11-05 확인: 빈 응답/403).
GitHub 예약 실행은 2026-11 기록으로 평균 153분, 길게는 10시간 간격이었다.

## 키워드 넣고 빼기 (텔레그램 봇 채팅창에서)

| 입력 | 동작 |
|---|---|
| `/add 에어팟` 또는 `+에어팟` | 추가 (쉼표로 여러 개: `/add 에어팟, 닌텐도`) |
| `/del 에어팟` 또는 `-에어팟` | 삭제 |
| `/list` | 현재 키워드 보기 |
| `/check` | 뽐질 지금 바로 확인 |

- 띄어쓰기·대소문자 무시: `에어팟 프로` = `에어팟프로` = `에어팟PRO`
- 쇼핑몰 이름도 검사함: `쿠팡` 넣으면 쿠팡 핫딜 전부
- 키워드는 Worker(KV)에 저장되고, 뽐뿌 쪽(GitHub)은 실행할 때마다 Worker의 `/kw` 주소에서 받아 간다.

## 구성

### Cloudflare Worker `hotdeal-alert`
- 주소: https://hotdeal-alert.qaszx4.workers.dev
- 코드: `cloudflare/worker.js`를 대시보드 편집기에 붙여넣고 Deploy
- 시크릿 `TELEGRAM_TOKEN` (봇 토큰)
- KV 바인딩 `STATE` → 네임스페이스 `hotdeal-alert-state`
  - `config` = 키워드·연결된 채팅 id
  - `seen` = 이미 본 글 번호 (무료 한도가 하루 쓰기 1,000회라 바뀔 때만 씀)
- Cron Trigger: Every 8 minutes
- 주소별 기능
  - `/setup`: 텔레그램 webhook 등록 (토큰을 바꾸면 한 번 열기)
  - `/run`: 지금 확인 (1분에 한 번)
  - `/kw?key=…`: GitHub 쪽이 키워드를 가져가는 주소 (key는 토큰에서 계산, 없으면 403)
  - `/tg`: 텔레그램 webhook (토큰에서 계산한 비밀값이 없으면 403)

### GitHub Actions (이 저장소)
- 시크릿 `TELEGRAM_TOKEN` (Worker와 같은 봇 토큰)
- `hotdeal_alert.py`: 뽐뿌 RSS 확인 후 `state.json`에 본 글 번호를 저장하고 커밋
- 텔레그램 명령은 처리하지 않음 (webhook을 쓰면 getUpdates를 쓸 수 없음)

## 동작 메모
- 뽐뿌는 게시판 페이지가 봇 요청을 막아서(403) 공식 RSS(`rss.php?id=ppomppu`, 최근 15개)를 읽는다. 실행 간격 사이에 15개 넘게 올라오면 일부를 놓친다. GitHub 실행이 몇 시간씩 늦으면 그동안 올라온 글 대부분을 놓칠 수 있다.
- 뽐질은 공개 미리보기(`t.me/s/jirum`, 한 페이지 20개)를 읽고, 이미 본 글이 나올 때까지 최대 5페이지(100개)까지 거슬러 본다. 쿠팡 파트너스 안내 문구와 링크 주소는 키워드 비교에서 뺀다.
- 첫 실행은 기준점만 잡고 알림을 보내지 않는다.
- 접속이 실패하면 1시간 쉬었다가 다시 시도하고, 실패·복구 때 한 번씩 텔레그램으로 알린다.
- 펨코는 GitHub 서버 IP를 `HTTP Error 430`으로 막아서(2026-10-20 확인) 대상에서 뺐다.
- 저장소에 60일간 활동이 없으면 GitHub 예약 실행이 꺼질 수 있다. 뽐뿌 알림이 끊기면 Actions 탭에서 다시 켠다.
- 뽐뿌 파싱 테스트: `python -X utf8 hotdeal_alert.py --test 저장한RSS.xml 키워드`
