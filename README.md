# 체크카드 소비 자동 캡처 & 분석 에이전트

> 실시간 배너 스크린샷 OCR 기반 캡처 파이프라인으로 개인 소비 데이터를 수집하고, 트리거별로 분리된 LangGraph 워크플로우가 분류·주간 브리핑을 수행하는 개인 금융 도구. 에이전트 루프는 딱 두 곳뿐이다 — 가맹점 판단이 불확실할 때 LLM이 스스로 웹 검색 여부를 결정·반복하는 지점, 그리고 자연어 질문에 답할 때 한 번의 조회로 부족하면 LLM이 스스로 추가 쿼리를 결정·반복하는 지점.

전체 설계는 [docs/project-overview.md](docs/project-overview.md) 참고. 이 README는 지금 이 저장소에서 **실제로 돌아가는 것**과 **실행 방법**만 다룬다.

## 지금 뭐가 동작하는가

| 구성 요소 | 상태 |
|---|---|
| 신규 거래 그래프 (`POST /webhook/capture` → 파싱 → 0차 정확 매칭 → 가맹점 판단 에이전트 루프) | ✅ 동작 — 토큰 인증, dedup, 오래된 캡처/파싱 실패 리뷰 큐 이관까지 포함 ([app/main.py](app/main.py), [app/parsing.py](app/parsing.py), [app/merchant_judgment.py](app/merchant_judgment.py)) |
| 가맹점 판단 에이전트 루프 (실LLM, 단독 데모) | ✅ 동작 — Groq 실제 호출로 검색/확정/에스컬레이트 트레이스 재현 ([poc/merchant_judgment_live_demo.py](poc/merchant_judgment_live_demo.py)) |
| 가맹점 판단 에이전트 루프 (mock) | ✅ 동작 — LLM/검색 없이 스크립트 응답으로 트레이스만 재현 ([poc/merchant_judgment_poc.py](poc/merchant_judgment_poc.py)) |
| Postgres 저장 + 채팅형 조회 화면 (`POST /query`) | ✅ 동작 — 집계형 자연어 질문에 답하는 두 번째 에이전트 루프(쿼리 결과가 부족하면 스스로 추가 쿼리, 최대 3회) + 최근 거래 목록 ([app/query.py](app/query.py), [app/static/index.html](app/static/index.html)) |
| 캡처 파이프라인 클라이언트 (Back Tap → 단축어 → OCR) | ✅ 동작 — 실제 아이폰에서 Back Tap → 단축어(최근 스크린샷 → Live Text OCR → `POST /webhook/capture`)로 실 결제 캡처 확인 (docs 3.1). 이 저장소엔 코드가 없음 — 아이폰 설정/단축어 앱 안의 설정이라 git으로 관리되지 않음 |
| 실 웹 검색 도구 연동 | ✅ 동작 — Gemini Google Search grounding으로 가맹점 검색 (docs 3.5). 판단/QA/브리핑은 Groq(저지연 분류), 검색만 Gemini로 나눠 씀. 검색/판단 중 LLM 장애가 나면 거래를 잃지 않고 리뷰 큐(`llm_unavailable`)로 이관 |
| 주간 브리핑 (`GET /briefing`) | ✅ 동작 — Slack 대신 웹 화면에서 최근 7일 집계 + LLM 요약을 즉시 보여준다 (스케줄러 없이 페이지 열 때마다 재계산, docs 3.4.2) |

## 빠른 시작 (Docker)

### 1. 환경 준비

```bash
cp .env.example .env             # GEMINI_API_KEY, GROQ_API_KEY, CAPTURE_WEBHOOK_TOKEN, POSTGRES_PASSWORD(+ DATABASE_URL) 채워넣기
docker compose build
```

### 2. 조회 화면 (FastAPI 서버)

```bash
docker compose up
```

브라우저에서 http://127.0.0.1:8000 접속 — 상단 채팅창에 "이번 주 카페 얼마 썼어?"처럼 물어보면 text-to-SQL로 변환해 답해주고, 아래에는 최근 거래 목록이 카드 형태로 보인다. `GET /transactions`로 JSON도 바로 조회 가능. `app` 컨테이너는 `db`(Postgres) 컨테이너가 healthy 상태가 된 뒤에 뜨고, Postgres 데이터는 `pgdata` named volume에 저장되어 컨테이너를 내려도 유지된다.

### 3. 가맹점 판단 에이전트 루프 데모

컨테이너 안에서 1회성으로 실행한다 (서버는 별도로 계속 떠 있어도 됨):

```bash
# mock (LLM/검색 없음, 재현성 검증용)
docker compose run --rm app python poc/merchant_judgment_poc.py

# 실LLM (Groq 실제 호출, .env의 GROQ_API_KEY 필요)
docker compose run --rm app python poc/merchant_judgment_live_demo.py
```

두 스크립트 모두 docs 3.4.1의 두 트레이스(검색 후 확정 / 재시도 한도 초과 후 리뷰 큐 이관)를 재현한다. 실LLM 버전은 실행할 때마다 결과를 Postgres(`db` 컨테이너)에 저장한다.

### 4. 웹훅으로 신규 거래 캡처

서버가 떠 있는 상태에서 캡처 하나를 시뮬레이션:

```bash
curl -X POST http://127.0.0.1:8000/webhook/capture \
  -H "Content-Type: application/json" \
  -H "Authorization: $CAPTURE_WEBHOOK_TOKEN" \
  -d '{"raw_text":"토스뱅크 체크카드\n3,200원 결제 | 이디야커피(강남점)\n잔액 705,872원","captured_at":"2026-08-05T18:00:00+09:00"}'
```

`Authorization` 헤더가 `.env`의 `CAPTURE_WEBHOOK_TOKEN`과 정확히 일치해야 한다 (없거나 틀리면 401). 응답은 즉시 `{"status": "accepted"}`로 오고, 파싱된 가맹점명(`merchant_category_map`에 0차 매칭 없으면 가맹점 판단 에이전트 루프)이 백그라운드로 처리되어 잠시 후 `GET /transactions`에 나타난다. 같은 `raw_text`를 다시 보내면 `duplicate`, 형식이 안 맞으면 `parse_failed`(리뷰 큐 이관), `captured_at`이 5분 넘게 오래됐으면 `stale`을 반환한다.

### 5. 자연어로 소비 물어보기

```bash
curl -X POST http://127.0.0.1:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question":"이번 주 카페/디저트에 얼마 썼어?"}'
```

가맹점 판단 루프와 같은 패턴의 에이전트 루프다: 질문을 Postgres `SELECT` 쿼리로 변환(text-to-SQL) → 실행 → 결과로 답변 가능한지 LLM이 스스로 판단 → 부족하면(예: "지난달보다 늘었어?" 같은 비교 질문) 추가 쿼리를 스스로 요청하며 반복, 충분하면 답변 문장을 생성하고 종료한다 (재시도 한도 3회). `SELECT` 이외의 문장(INSERT/UPDATE/DROP/TRUNCATE 등)은 매 쿼리마다 안전성 검사에서 차단된다. 날짜/카테고리 조건이 명확한 "집계형 질문"만 지원하고, "저번에 갔던 그 카페 얼마였지?" 같은 가맹점이 불명확한 질문은 지원하지 않는다 — 이 프로젝트 스코프에서는 RAG를 쓰지 않기로 했다 (docs 3.3 참고).

### 6. 주간 브리핑 보기

```bash
curl http://127.0.0.1:8000/briefing
```

최근 7일 SQL 집계(총액, 카테고리별 합계) + LLM이 생성한 한두 문장 요약을 함께 반환한다. 스케줄러 없이 호출할 때마다 다시 계산한다 (docs 3.4.2, Slack 대신 웹으로 대체).

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
app/merchant_judgment.py         가맹점 판단 에이전트 루프 — LangGraph StateGraph (poc·웹훅 공용)
app/new_transaction_graph.py     신규 거래 처리 그래프 — 0차 정확 매칭 → 가맹점 판단 루프 → 저장/리뷰 큐 (LangGraph)
app/parsing.py                   raw_text 파싱 + dedup용 해시
app/query.py                     자연어 QA 에이전트 루프 — text-to-SQL 변환 + 답변 생성 (LangGraph, 집계형 질문만)
app/briefing.py                  주간 브리핑 그래프 — 최근 7일 집계 + LLM 요약 (LangGraph, fetch→summarize)
app/db.py                        Postgres 저장소 (transactions / merchant_category_map / captures / review_queue)
app/main.py                      FastAPI: POST /webhook/capture, POST /query, GET /transactions, GET /briefing, GET /
app/static/index.html            채팅형 조회 화면 (주간 요약 + 질문창 + 최근 거래 목록)
poc/batch_replay.py              과거 거래 CSV 일괄 재생 + 0차 히트율/decision 분포 통계
Dockerfile                       앱 이미지 (Python 3.11-slim)
docker-compose.yml                로컬 실행 (app: 포트 8000, .env, data/ 볼륨 마운트 / db: Postgres, pgdata 볼륨)
```


