# 체크카드 소비 자동 캡처 & 분석 에이전트

> 실시간 배너 스크린샷 OCR 기반 캡처 파이프라인으로 개인 소비 데이터를 수집하고, 트리거별로 분리된 LangGraph 워크플로우가 분류·주간 브리핑을 수행하는 개인 금융 도구. 유일한 에이전트 루프는 가맹점 판단이 불확실할 때 LLM이 스스로 웹 검색 여부를 결정·반복하는 지점 하나뿐이다.

전체 설계는 [docs/project-overview.md](docs/project-overview.md) 참고. 이 README는 지금 이 저장소에서 **실제로 돌아가는 것**과 **실행 방법**만 다룬다.

## 지금 뭐가 동작하는가

| 구성 요소 | 상태 |
|---|---|
| 신규 거래 그래프 (`POST /webhook/capture` → 파싱 → 0차 정확 매칭 → 가맹점 판단 에이전트 루프) | ✅ 동작 — 토큰 인증, dedup, 오래된 캡처/파싱 실패 리뷰 큐 이관까지 포함 ([app/main.py](app/main.py), [app/parsing.py](app/parsing.py), [app/merchant_judgment.py](app/merchant_judgment.py)) |
| 가맹점 판단 에이전트 루프 (실LLM, 단독 데모) | ✅ 동작 — Gemini 실제 호출로 검색/확정/에스컬레이트 트레이스 재현 ([poc/merchant_judgment_live_demo.py](poc/merchant_judgment_live_demo.py)) |
| 가맹점 판단 에이전트 루프 (mock) | ✅ 동작 — LLM/검색 없이 스크립트 응답으로 트레이스만 재현 ([poc/merchant_judgment_poc.py](poc/merchant_judgment_poc.py)) |
| SQLite 저장 + 채팅형 조회 화면 (`POST /query`) | ✅ 동작 — 집계형 자연어 질문(text-to-SQL) + 최근 거래 목록 ([app/query.py](app/query.py), [app/static/index.html](app/static/index.html)) |
| 캡처 파이프라인 클라이언트 (Back Tap → 단축어 → OCR) | ✅ 동작 — 실제 아이폰에서 Back Tap → 단축어(최근 스크린샷 → Live Text OCR → `POST /webhook/capture`)로 실 결제 캡처 확인 (docs 3.1). 이 저장소엔 코드가 없음 — 아이폰 설정/단축어 앱 안의 설정이라 git으로 관리되지 않음 |
| 실 웹 검색 도구 연동 | ✅ 동작 — Gemini Google Search grounding으로 가맹점 검색 (docs 3.5). 검색/판단 중 LLM 장애가 나면 거래를 잃지 않고 리뷰 큐(`llm_unavailable`)로 이관 |
| 주간 브리핑 (`GET /briefing`) | ✅ 동작 — Slack 대신 웹 화면에서 최근 7일 집계 + LLM 요약을 즉시 보여준다 (스케줄러 없이 페이지 열 때마다 재계산, docs 3.4.2) |

## 빠른 시작 (Docker)

### 1. 환경 준비

```bash
cp .env.example .env             # GEMINI_API_KEY, CAPTURE_WEBHOOK_TOKEN 채워넣기
docker compose build
```

### 2. 조회 화면 (FastAPI 서버)

```bash
docker compose up
```

브라우저에서 http://127.0.0.1:8000 접속 — 상단 채팅창에 "이번 주 카페 얼마 썼어?"처럼 물어보면 text-to-SQL로 변환해 답해주고, 아래에는 최근 거래 목록이 카드 형태로 보인다. `GET /transactions`로 JSON도 바로 조회 가능. `data/`는 호스트와 볼륨 마운트되어 있어 컨테이너를 내려도 SQLite 데이터(`data/spending.db`)는 유지된다.

### 3. 가맹점 판단 에이전트 루프 데모

컨테이너 안에서 1회성으로 실행한다 (서버는 별도로 계속 떠 있어도 됨):

```bash
# mock (LLM/검색 없음, 재현성 검증용)
docker compose run --rm app python poc/merchant_judgment_poc.py

# 실LLM (Gemini 실제 호출, .env의 GEMINI_API_KEY 필요)
docker compose run --rm app python poc/merchant_judgment_live_demo.py
```

두 스크립트 모두 docs 3.4.1의 두 트레이스(검색 후 확정 / 재시도 한도 초과 후 리뷰 큐 이관)를 재현한다. 실LLM 버전은 실행할 때마다 결과를 SQLite(`data/spending.db`)에 저장한다.

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

질문을 SQLite `SELECT` 쿼리로 변환(text-to-SQL) → 실행 → 결과를 근거로 LLM이 자연어 답변을 생성한다. `SELECT` 이외의 문장(INSERT/UPDATE/DROP 등)은 안전성 검사에서 차단된다. 날짜/카테고리 조건이 명확한 "집계형 질문"만 지원하고, "저번에 갔던 그 카페 얼마였지?" 같은 가맹점이 불명확한 질문은 지원하지 않는다 — 이 프로젝트 스코프에서는 RAG를 쓰지 않기로 했다 (docs 3.3 참고).

### Docker 없이 로컬에서 (선택)

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # GEMINI_API_KEY 채워넣기
python -m uvicorn app.main:app --port 8000
```

## 프로젝트 구조

```
docs/project-overview.md         전체 시스템 설계 문서
poc/merchant_judgment_poc.py     에이전트 루프 mock 재현
poc/merchant_judgment_live_demo.py  에이전트 루프 실LLM 데모 (SQLite 저장 포함)
app/merchant_judgment.py         가맹점 판단 에이전트 루프 (poc·웹훅 공용)
app/parsing.py                   raw_text 파싱 + dedup용 해시
app/query.py                     자연어 QA — text-to-SQL 변환 + 답변 생성 (집계형 질문만)
app/briefing.py                  주간 브리핑 — 최근 7일 집계 + LLM 요약
app/db.py                        SQLite 저장소 (transactions / merchant_category_map / captures / review_queue)
app/main.py                      FastAPI: POST /webhook/capture, POST /query, GET /transactions, GET /briefing, GET /
app/static/index.html            채팅형 조회 화면 (주간 요약 + 질문창 + 최근 거래 목록)
poc/batch_replay.py              과거 거래 CSV 일괄 재생 + 0차 히트율/decision 분포 통계
Dockerfile                       앱 이미지 (Python 3.11-slim)
docker-compose.yml                로컬 실행 (포트 8000, .env, data/ 볼륨 마운트)
```


