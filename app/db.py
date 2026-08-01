"""SQLite 저장소 (docs/project-overview.md 3.3 관계형 DB).

확정된 거래 레코드(정형 값)만 저장한다. 가맹점 판단 루프의 원문 트레이스/근거는
저장하지 않고 최종 category/decision/confidence/reason만 남긴다.
"""

import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "spending.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                merchant TEXT NOT NULL,
                amount INTEGER NOT NULL,
                category TEXT,
                decision TEXT NOT NULL,
                confidence REAL,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )


def insert_transaction(
    merchant: str,
    amount: int,
    category: Optional[str],
    decision: str,
    confidence: Optional[float],
    reason: str,
) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO transactions (merchant, amount, category, decision, confidence, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (merchant, amount, category, decision, confidence, reason),
        )


def fetch_transactions() -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM transactions ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]
