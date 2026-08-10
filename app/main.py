"""FastAPI 서버 (docs/project-overview.md 3.2, 3.4.1~3.4.3, 3.7).

GET /transactions, GET /briefing, GET / — 조회용.
POST /webhook/capture — 캡처 수신, 파싱, 0차 정확 매칭 → 가맹점 판단 에이전트 루프를
BackgroundTasks로 비동기 실행한다.
POST /query — 자연어 질문 → text-to-SQL → 답변 (집계형 질문만, docs 3.4.3).
"""

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import db
from app.briefing import generate_weekly_briefing
from app.new_transaction_graph import process_new_transaction
from app.parsing import parse_capture, raw_text_hash
from app.query import QueryError, answer_question
from app.stats import fetch_spending_overview

app = FastAPI(title="Spending Copilot (toy)")

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

CAPTURE_WEBHOOK_TOKEN = os.environ.get("CAPTURE_WEBHOOK_TOKEN")
STALE_CAPTURE_THRESHOLD_SECONDS = 5 * 60


class CapturePayload(BaseModel):
    raw_text: str
    captured_at: datetime


class QueryPayload(BaseModel):
    question: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/transactions")
def transactions() -> list[dict]:
    return db.fetch_transactions()


@app.get("/briefing")
def briefing() -> dict:
    """주간 브리핑 — 최근 7일 집계 + LLM 요약 (docs 3.4.2)."""
    return generate_weekly_briefing()


@app.get("/stats")
def stats() -> dict:
    """소비 대시보드 — 저장된 전체 기간의 일별/카테고리별 집계."""
    return fetch_spending_overview()


@app.post("/query")
def query(payload: QueryPayload) -> dict:
    """자연어 QA — 집계형 질문 경로 (docs 3.4.3)."""
    try:
        return answer_question(payload.question)
    except QueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/webhook/capture")
def webhook_capture(
    payload: CapturePayload,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
) -> dict:
    if not CAPTURE_WEBHOOK_TOKEN or authorization != CAPTURE_WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")

    text_hash = raw_text_hash(payload.raw_text)
    if db.is_duplicate_capture(text_hash):
        return {"status": "duplicate"}
    db.record_capture(text_hash, payload.captured_at.isoformat())

    captured_at = payload.captured_at
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - captured_at).total_seconds()
    if age_seconds > STALE_CAPTURE_THRESHOLD_SECONDS:
        db.insert_review(reason="stale_capture", raw_text=payload.raw_text)
        return {"status": "stale"}

    parsed = parse_capture(payload.raw_text)
    if parsed is None:
        db.insert_review(reason="ocr_parse_fail", raw_text=payload.raw_text)
        return {"status": "parse_failed"}

    background_tasks.add_task(process_new_transaction, parsed.merchant, parsed.amount)
    return {"status": "accepted"}
