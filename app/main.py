"""FastAPI 서버 (docs/project-overview.md 3.2, 3.4.1~3.4.3, 3.7).

GET /transactions, GET /briefing, GET / — 조회용.
POST /webhook/capture — 캡처 수신, 파싱, 0차 정확 매칭 → 가맹점 판단 에이전트 루프를
BackgroundTasks로 비동기 실행한다.
POST /query — 자연어 질문 → text-to-SQL → 답변 (집계형 질문만, docs 3.4.3).
"""

import html
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import db
from app.briefing import generate_weekly_briefing
from app.merchant_judgment import CATEGORIES
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


class ResolvePayload(BaseModel):
    category: str


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


@app.get("/review/{transaction_id}", response_class=HTMLResponse)
def review_page(transaction_id: int) -> str:
    """escalate 건 카테고리 선택 페이지 — ntfy 알림의 "확인하기" 액션이 여기로 연결된다."""
    transaction = db.fetch_transaction(transaction_id)
    if transaction is None:
        raise HTTPException(status_code=404, detail="not found")

    merchant = html.escape(transaction["merchant"])
    amount = f"{transaction['amount']:,}"

    if transaction["decision"] == "confirm":
        category = html.escape(transaction["category"] or "")
        body = f'<p class="done">이미 "{category}"(으)로 처리됐어요.</p>'
    else:
        buttons = "\n".join(
            f'<button onclick="pick(\'{html.escape(c)}\')">{html.escape(c)}</button>' for c in CATEGORIES
        )
        body = f"""
        <div class="cats" id="cats">{buttons}</div>
        <p class="done" id="done" style="display:none">저장됐어요 ✓</p>
        <script>
        async function pick(category) {{
          const res = await fetch("/review/{transaction_id}/resolve", {{
            method: "POST",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify({{category}}),
          }});
          if (res.ok) {{
            document.getElementById("cats").style.display = "none";
            document.getElementById("done").style.display = "block";
          }} else {{
            alert("실패: " + await res.text());
          }}
        }}
        </script>
        """

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>카테고리 확인</title>
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 0; padding: 24px; background: #111; color: #eee; }}
  h1 {{ font-size: 18px; margin-bottom: 4px; }}
  .txn {{ color: #8ecbff; margin-bottom: 20px; }}
  .cats {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  button {{ padding: 16px; font-size: 16px; border: none; border-radius: 10px; background: #2a2a2a; color: #eee; }}
  button:active {{ background: #444; }}
  .done {{ text-align: center; font-size: 20px; margin-top: 40px; }}
</style>
</head>
<body>
  <h1>카테고리를 선택하세요</h1>
  <div class="txn">{merchant} · {amount}원</div>
  {body}
</body>
</html>"""


@app.post("/review/{transaction_id}/resolve")
def resolve_review(transaction_id: int, payload: ResolvePayload) -> dict:
    if payload.category not in CATEGORIES:
        raise HTTPException(status_code=400, detail="invalid category")
    merchant = db.resolve_transaction_category(transaction_id, payload.category)
    if merchant is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"status": "ok"}
