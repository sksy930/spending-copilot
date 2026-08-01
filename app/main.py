"""FastAPI 스켈레톤 (docs/project-overview.md 3.2, 로드맵 2주차 항목의 최소 착수).

지금은 조회용 GET /transactions 하나만 구현한다. webhook/capture 등 캡처 관련
엔드포인트는 캡처 파이프라인 PoC(로드맵 2주차) 진행 시 추가한다.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import db

app = FastAPI(title="Spending Copilot (toy)")

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/transactions")
def transactions() -> list[dict]:
    return db.fetch_transactions()
