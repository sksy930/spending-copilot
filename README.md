# 체크카드 소비 자동 캡처 & 분석 에이전트

> 실시간 배너 스크린샷 OCR 기반 캡처 파이프라인으로 개인 소비 데이터를 수집하고, 트리거별로 분리된 LangGraph 워크플로우가 분류·이상탐지·주간 브리핑을 수행하는 개인 금융 도구. 유일한 에이전트 루프는 가맹점 판단이 불확실할 때 LLM이 스스로 웹 검색 여부를 결정·반복하는 지점 하나뿐이다.

전체 설계는 [docs/project-overview.md](docs/project-overview.md) 참고. 이 README는 지금 이 저장소에서 **실제로 돌아가는 것**과 **실행 방법**만 다룬다.

## 지금 뭐가 동작하는가

| 구성 요소 | 상태 |
|---|---|
| 가맹점 판단 에이전트 루프 (실LLM) | ✅ 동작 — Gemini 실제 호출로 검색/확정/에스컬레이트 트레이스 재현 ([poc/merchant_judgment_live_demo.py](poc/merchant_judgment_live_demo.py)) |
| 가맹점 판단 에이전트 루프 (mock) | ✅ 동작 — LLM/검색 없이 스크립트 응답으로 트레이스만 재현 ([poc/merchant_judgment_poc.py](poc/merchant_judgment_poc.py)) |
| SQLite 저장 + 조회 화면 | ✅ 동작 — 에이전트 루프 결과를 저장하고 FastAPI로 조회 ([app/](app/)) |
| 캡처 파이프라인 (Back Tap → 단축어 → Webhook, OCR) | ⬜ 설계만 (docs 3.1) |
| 0차 정확 매칭 / 1차 과거거래 유사도 / 2차 업종분류 DB RAG | ⬜ 설계만 (docs 3.4.1) |
| Anomaly Detection, 주간 브리핑, 자연어 QA | ⬜ 설계만 (docs 3.4.1~3.4.3) |

## 빠른 시작

### 1. 환경 준비

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # GEMINI_API_KEY 채워넣기
```

### 2. 가맹점 판단 에이전트 루프 데모

```bash
# mock (LLM/검색 없음, 재현성 검증용)
python poc/merchant_judgment_poc.py

# 실LLM (Gemini 실제 호출, .env의 GEMINI_API_KEY 필요)
python poc/merchant_judgment_live_demo.py
```

두 스크립트 모두 docs 3.4.1의 두 트레이스(검색 후 확정 / 재시도 한도 초과 후 리뷰 큐 이관)를 재현한다. 실LLM 버전은 실행할 때마다 결과를 SQLite(`data/spending.db`)에 저장한다.

### 3. 조회 화면

```bash
python -m uvicorn app.main:app --port 8000
```

브라우저에서 http://127.0.0.1:8000 접속 — 실LLM 데모를 실행할 때마다 쌓인 판단 결과(가맹점/금액/카테고리/판정/확신도/이유)가 표로 보인다. `GET /transactions`로 JSON도 바로 조회 가능.

## 프로젝트 구조

```
docs/project-overview.md         전체 시스템 설계 문서
poc/merchant_judgment_poc.py     에이전트 루프 mock 재현
poc/merchant_judgment_live_demo.py  에이전트 루프 실LLM 데모 (SQLite 저장 포함)
app/db.py                        SQLite 저장소 (transactions 테이블)
app/main.py                      FastAPI: GET /transactions, GET /
app/static/index.html            조회 화면
```


