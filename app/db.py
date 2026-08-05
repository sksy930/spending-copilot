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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS merchant_category_map (
                merchant TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS captures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_text_hash TEXT NOT NULL UNIQUE,
                captured_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS review_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reason TEXT NOT NULL,
                raw_text TEXT,
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


def lookup_category(merchant: str) -> Optional[str]:
    """0차 정확 매칭: merchant_category_map에서 완전 일치 조회."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT category FROM merchant_category_map WHERE merchant = ?", (merchant,)
        ).fetchone()
        return row["category"] if row else None


def upsert_merchant_category(merchant: str, category: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO merchant_category_map (merchant, category, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(merchant) DO UPDATE SET category = excluded.category, updated_at = excluded.updated_at
            """,
            (merchant, category),
        )


def is_duplicate_capture(raw_text_hash: str) -> bool:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM captures WHERE raw_text_hash = ?", (raw_text_hash,)
        ).fetchone()
        return row is not None


def record_capture(raw_text_hash: str, captured_at: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO captures (raw_text_hash, captured_at) VALUES (?, ?)",
            (raw_text_hash, captured_at),
        )


def insert_review(reason: str, raw_text: Optional[str]) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO review_queue (reason, raw_text) VALUES (?, ?)",
            (reason, raw_text),
        )
