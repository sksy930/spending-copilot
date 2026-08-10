"""Postgres 저장소 (docs/project-overview.md 3.3 관계형 DB).

확정된 거래 레코드(정형 값)만 저장한다. 가맹점 판단 루프의 원문 트레이스/근거는
저장하지 않고 최종 category/decision/confidence/reason만 남긴다.
"""

import os
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ["DATABASE_URL"]


@contextmanager
def connect() -> Iterator[psycopg2.extensions.connection]:
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                merchant TEXT NOT NULL,
                amount INTEGER NOT NULL,
                category TEXT,
                decision TEXT NOT NULL,
                confidence REAL,
                reason TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS merchant_category_map (
                merchant TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS captures (
                id SERIAL PRIMARY KEY,
                raw_text_hash TEXT NOT NULL UNIQUE,
                captured_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS review_queue (
                id SERIAL PRIMARY KEY,
                reason TEXT NOT NULL,
                raw_text TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
    created_at: Optional[str] = None,
) -> None:
    """created_at을 안 주면 NOW()로 찍힌다. 과거 내역 백필(seed 스크립트) 용도로만 지정한다."""
    init_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transactions (merchant, amount, category, decision, confidence, reason, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()))
            """,
            (merchant, amount, category, decision, confidence, reason, created_at),
        )


def fetch_transactions() -> list[dict]:
    init_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM transactions ORDER BY id DESC")
        return [dict(row) for row in cur.fetchall()]


def lookup_category(merchant: str) -> Optional[str]:
    """0차 정확 매칭: merchant_category_map에서 완전 일치 조회."""
    init_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT category FROM merchant_category_map WHERE merchant = %s", (merchant,)
        )
        row = cur.fetchone()
        return row["category"] if row else None


def upsert_merchant_category(merchant: str, category: str) -> None:
    init_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO merchant_category_map (merchant, category, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (merchant) DO UPDATE SET category = excluded.category, updated_at = excluded.updated_at
            """,
            (merchant, category),
        )


def is_duplicate_capture(raw_text_hash: str) -> bool:
    init_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM captures WHERE raw_text_hash = %s", (raw_text_hash,))
        return cur.fetchone() is not None


def record_capture(raw_text_hash: str, captured_at: str) -> None:
    init_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO captures (raw_text_hash, captured_at) VALUES (%s, %s)",
            (raw_text_hash, captured_at),
        )


def insert_review(reason: str, raw_text: Optional[str]) -> None:
    init_db()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO review_queue (reason, raw_text) VALUES (%s, %s)",
            (reason, raw_text),
        )
