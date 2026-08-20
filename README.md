# 체크카드 소비 자동 캡처 & 분석 에이전트

> 실시간 결제 알림 스크린샷 OCR로 개인 소비 데이터를 수집하고, 트리거별로 분리된 LangGraph 워크플로우가 분류·조회·브리핑을 수행하는 개인 금융 도구. "확신 없을 때만 AI를 쓴다"는 원칙을 시스템 전체에 일관되게 적용한다 — 가맹점명이 이미 아는 것이면(0차 정확 매칭) LLM을 아예 안 부르고, 질문이 뻔한 패턴(기간+카테고리)이면 QA도 LLM 없이 즉답한다. 그래도 판단이 필요하면 에이전트 루프가 두 곳에서 돈다 — 가맹점 판단이 불확실할 때 LLM이 스스로 웹 검색 여부를 결정·반복하는 지점, 그리고 자연어 질문에 한 번의 조회로 답이 안 나올 때 LLM이 스스로 추가 조회를 결정·반복하는 지점. 그 판단마저 확신이 안 서면(escalate) 사람에게 푸시 알림을 보내 맡기고, 사람이 고른 답은 다시 캐시에 반영되어 다음번엔 안 물어본다.

전체 설계는 [docs/project-overview.md](docs/project-overview.md) 참고. 이 README는 지금 이 저장소에서 **실제로 돌아가는 것**과 **실행 방법**만 다룬다.

## 지금 뭐가 동작하는가

| 구성 요소 | 상태 |
|---|---|
| 신규 거래 그래프 (`POST /webhook/capture` → 파싱 → 0차 정확 매칭 → 가맹점 판단 에이전트 루프) | ✅ 동작 — 토큰 인증, dedup, 오래된 캡처/파싱 실패 리뷰 큐 이관까지 포함 ([app/main.py](app/main.py), [app/parsing.py](app/parsing.py), [app/new_transaction_graph.py](app/new_transaction_graph.py)) |
| 한 스크린샷에 결제 여러 건 캡처 | ✅ 동작 — 알림 센터를 며칠치 밀렸다가 한 번에 캡처해도 결제 블록마다 나눠서 전부 처리한다. 계좌 출금(네이버페이/카카오페이 충전 등, `10,000원 출금 / 내 토스뱅크 통장>> 네이버페이충전` 형식)은 가맹점명이 없어 판단 루프를 안 태우고 곧장 사람 확인으로 보낸다. 각 결제의 실제 시각(원문의 "오후 12:06" 등)을 복원해 `created_at`에 반영한다 ([app/parsing.py](app/parsing.py)) |
| 가맹점 판단 에이전트 루프 | ✅ 동작 — **Gemini가 기본, 실패 시 Groq로 자동 대체**([app/llm.py](app/llm.py)). 한국 브랜드 실세계 지식이 필요한 판단이라 Gemini가 실측 비교에서 더 정확했다(예: 폴바셋을 Groq는 "베이커리"로 오판, Gemini는 "카페"로 정답). 검색이 필요하면 Gemini Google Search grounding으로 웹 검색 후 재판단, 재시도 한도(2회) 초과 시 리뷰 큐 이관 ([app/merchant_judgment.py](app/merchant_judgment.py)) |
| Postgres 저장 + 대시보드 화면 | ✅ 동작 — 이번 달 지출, 요일별 히트맵(칸 클릭 시 그날 거래 목록 팝오버), 주간 지출 추세(점에 마우스 올리면 그 주 금액 표시), 카테고리별 지출(카테고리마다 다른 색), 최근 거래 목록(배지 클릭으로 카테고리 바로 수정) ([app/stats.py](app/stats.py), [app/static/index.html](app/static/index.html)) |
| 자연어 QA (`POST /query`) | ✅ 동작 — `query_spending(period, category, merchant)` 네이티브 tool-calling 에이전트 루프(한 번의 조회로 부족하면 스스로 추가 호출, 최대 3회). 기간+카테고리가 명확한 뻔한 질문은 **0차 매칭으로 LLM 호출 없이 즉답**하고, 채팅 UI에 "⚡ 즉답(AI 미사용)" vs "🔍 AI가 N단계 조회함"으로 어느 쪽이었는지 보여준다 ([app/query.py](app/query.py)) |
| 캡처 파이프라인 클라이언트 (Back Tap → 단축어 → OCR) | ✅ 동작 — 실제 아이폰에서 Back Tap → 단축어(최근 스크린샷 → Live Text OCR → `POST /webhook/capture`)로 실 결제 캡처 확인. 배너 팝업과 알림 센터 두 OCR 형식 모두 지원 (docs 3.1). 이 저장소엔 코드가 없음 — 아이폰 설정/단축어 앱 안의 설정이라 git으로 관리되지 않음 |
| 실 웹 검색 도구 연동 | ✅ 동작 — Gemini Google Search grounding으로 가맹점 검색 (docs 3.5). 검색/판단 중 LLM 장애가 나면 거래를 잃지 않고 리뷰 큐(`llm_unavailable`)로 이관 |
| Groq ↔ Gemini 자동 폴백 | ✅ 동작 — 한쪽 provider가 장애(예: 모델 단종, 일시적 과부하)를 일으키면 자동으로 다른 쪽으로 넘어간다. 실제로 이 프로젝트에서 Groq가 모델을 통보 없이 단종시켜 판단/QA/브리핑이 한꺼번에 죽었던 사고가 있었고, 그 사고 이후 추가한 안전장치 ([app/llm.py](app/llm.py)) |
| 주간 브리핑 (`GET /briefing`) | ✅ 동작 — 웹 화면에서 최근 7일 집계 + LLM 요약을 즉시 보여준다 (스케줄러 없이 페이지 열 때마다 재계산, docs 3.4.2) |
| escalate 사람 개입 | ✅ 동작 — 가맹점 판단 루프가 확신을 못 하면(검색해도 불명확하거나, 계좌 출금처럼 가맹점명 자체가 없으면) ntfy 푸시 알림 발송 → 폰에서 "확인하기"로 모바일 카테고리 선택 페이지 열림. **PC 화면에서도** 우상단 🔔 아이콘으로 미확정 건을 한 번에 확인·처리할 수 있고, **이미 확정된 거래도** 배지를 클릭하면 카테고리를 다시 고를 수 있다. 사람이 고른 카테고리는 `merchant_category_map`에 반영되어 같은 가맹점 다음 거래부터 0차 매칭된다 ([app/notify.py](app/notify.py), [app/main.py](app/main.py) `/review`, `/review/{id}`) |

## 빠른 시작 (Docker)

### 1. 환경 준비

```bash
cp .env.example .env             # GEMINI_API_KEY, GROQ_API_KEY, CAPTURE_WEBHOOK_TOKEN, POSTGRES_PASSWORD(+ DATABASE_URL) 채워넣기
docker compose build
```

escalate 알림을 폰으로 받으려면 `.env`에 `NTFY_TOPIC`(랜덤 문자열, 예: `openssl rand -hex 16`)과 `PUBLIC_BASE_URL`(폰이 웹훅을 보내는 것과 같은 LAN IP:포트)도 채워넣는다. 아이폰에 [ntfy](https://ntfy.sh) 앱을 설치하고 그 토픽을 구독하면 된다.

### 2. 조회 화면 (FastAPI 서버)

```bash
docker compose up
```

브라우저에서 http://127.0.0.1:8000 접속. `app` 컨테이너는 `db`(Postgres) 컨테이너가 healthy 상태가 된 뒤에 뜨고, Postgres 데이터는 `pgdata` named volume에 저장되어 컨테이너를 내려도 유지된다.

### 3. 웹훅으로 신규 거래 캡처

서버가 떠 있는 상태에서 캡처 하나를 시뮬레이션:

```bash
curl -X POST http://127.0.0.1:8000/webhook/capture \
  -H "Content-Type: application/json" \
  -H "Authorization: $CAPTURE_WEBHOOK_TOKEN" \
  -d '{"raw_text":"토스뱅크 체크카드\n3,200원 결제 | 이디야커피(강남점)\n잔액 705,872원","captured_at":"2026-08-05T18:00:00+09:00"}'
```

`Authorization` 헤더가 `.env`의 `CAPTURE_WEBHOOK_TOKEN`과 정확히 일치해야 한다 (없거나 틀리면 401). 응답은 즉시 `{"status": "accepted", "count": N}`으로 오고(`raw_text` 안에 결제/출금이 몇 건 있었는지가 `count`), 각 건이 백그라운드로 처리되어 잠시 후 `GET /transactions`에 나타난다. 같은 `raw_text`를 다시 보내면 `duplicate`, 형식이 하나도 안 맞으면 `parse_failed`(리뷰 큐 이관), `captured_at`이 5분 넘게 오래됐으면 `stale`을 반환한다.

### 4. 자연어로 소비 물어보기

```bash
curl -X POST http://127.0.0.1:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question":"이번 주 카페 얼마 썼어?"}'
```

기간+카테고리가 명확한 질문은 `query_spending`을 LLM 호출 없이 바로 조회해서 답한다(`used_llm: false`). "지난달보다 늘었어?" 같은 비교 질문이나 가맹점명이 섞인 질문은 LLM 에이전트 루프로 넘어가 `query_spending`을 필요한 만큼 반복 호출한다(`used_llm: true`, `steps`). `SELECT` 이외의 SQL은 애초에 LLM이 쓸 수 없는 구조라(파라미터 바인딩된 고정 쿼리만 실행) 인젝션 여지가 없다. 날짜/카테고리/가맹점 조건이 명확한 질문만 지원하고, "저번에 갔던 그 카페 얼마였지?" 같은 가맹점이 불명확한 질문은 지원하지 않는다 — 이 프로젝트 스코프에서는 RAG를 쓰지 않기로 했다 (docs 3.3 참고).

### 5. 주간 브리핑 보기

```bash
curl http://127.0.0.1:8000/briefing
```

최근 7일 SQL 집계(총액, 카테고리별 합계) + LLM이 생성한 한두 문장 요약을 함께 반환한다.

### 6. escalate 건 확인·처리

```bash
curl http://127.0.0.1:8000/review          # 미확정(escalate) 거래 목록
```

브라우저에서 우상단 🔔 아이콘을 누르면 같은 목록을 화면에서 바로 처리할 수 있다. 폰으로는 ntfy 알림의 "확인하기" 액션이 `GET /review/{id}` 모바일 페이지로 연결된다.

### 가맹점 판단 에이전트 루프 단독 데모

컨테이너 안에서 1회성으로 실행한다 (서버는 별도로 계속 떠 있어도 됨):

```bash
# mock (LLM/검색 없음, 재현성 검증용)
docker compose run --rm app python poc/merchant_judgment_poc.py

# 실LLM (Gemini/Groq 실제 호출)
docker compose run --rm app python poc/merchant_judgment_live_demo.py
```

두 스크립트 모두 docs 3.4.1의 두 트레이스(검색 후 확정 / 재시도 한도 초과 후 리뷰 큐 이관)를 재현한다. 실LLM 버전은 실행할 때마다 결과를 Postgres(`db` 컨테이너)에 저장한다.

### Docker 없이 로컬에서 (선택)

DB는 Postgres라 서버가 어딘가에는 떠 있어야 한다 — `docker compose up -d db`로 컨테이너의 db 서비스만 띄우고 `DATABASE_URL`을 `localhost`로 바꿔 접속하는 방법이 가장 간단하다 (compose가 `db` 포트를 `127.0.0.1:5432`로 게시해둠).

```bash
docker compose up -d db          # Postgres만 로컬 포트로 기동
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # GEMINI_API_KEY, GROQ_API_KEY, POSTGRES_PASSWORD 채워넣고
                                  # DATABASE_URL의 호스트를 db → localhost로 바꾸기
python -m uvicorn app.main:app --port 8000
```

## 프로젝트 구조

```
docs/project-overview.md         전체 시스템 설계 문서
poc/merchant_judgment_poc.py     에이전트 루프 mock 재현
poc/merchant_judgment_live_demo.py  에이전트 루프 실LLM 데모 (Postgres 저장 포함)
poc/seed_history.py              과거 실거래 CSV를 신규 거래 그래프로 실제 분류해 백필 (데모 데이터 세팅)
poc/batch_replay.py              과거 거래 CSV 일괄 재생 + 0차 히트율/decision 분포 통계
app/llm.py                       공용 LLM 호출 헬퍼 — 기본 provider 실패 시 다른 provider로 자동 대체
app/merchant_judgment.py         가맹점 판단 에이전트 루프 — LangGraph StateGraph (poc·웹훅 공용)
app/new_transaction_graph.py     신규 거래 처리 그래프 — 0차 정확 매칭 → 가맹점 판단 루프(또는 곧장 escalate) → 저장/리뷰 큐 (LangGraph)
app/parsing.py                   raw_text 파싱(결제/출금, 여러 건, 실제 시각 복원) + dedup용 해시
app/query.py                     자연어 QA — 0차 매칭 fast path + tool-calling 에이전트 루프 (LangGraph)
app/briefing.py                  주간 브리핑 그래프 — 최근 7일 집계 + LLM 요약 (LangGraph, fetch→summarize)
app/notify.py                    escalate 건 ntfy 푸시 알림
app/stats.py                     대시보드 집계 (일별/주별/카테고리별 합계, 전체 기간)
app/db.py                        Postgres 저장소 (transactions / merchant_category_map / captures / review_queue)
app/main.py                      FastAPI: POST /webhook/capture, POST /query, GET /transactions, GET /briefing, GET /stats, GET·POST /review[/{id}[/resolve]], GET /
app/static/index.html            대시보드 + 채팅형 조회 화면 (요일별 히트맵, 주간 추세, 카테고리 차트, 최근 거래, 리뷰 패널)
Dockerfile                       앱 이미지 (Python 3.11-slim)
docker-compose.yml                로컬 실행 (app: 포트 8000, .env, data/ 볼륨 마운트 / db: Postgres, pgdata 볼륨)
```
