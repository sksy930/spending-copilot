"""주간 브리핑 (docs/project-overview.md 3.4.2).

Slack 대신 웹 화면에서 보여준다 — 별도 스케줄러 없이 페이지를 열 때마다 최근 7일치를
즉시 집계·요약한다. "해당 주"라는 고정 기간 SQL 스캔 결과를 LLM 요약기에 넣는 것이라
RAG가 아니다 (docs 3.4.2 참고).
"""

import json
import os
import sqlite3

import litellm
from litellm.exceptions import RateLimitError

from app.db import DB_PATH

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini/gemini-flash-latest")

BRIEFING_SYSTEM_PROMPT = """너는 개인 소비 데이터를 바탕으로 주간 브리핑을 써주는 비서다.
주어진 최근 7일 집계 데이터를 근거로 2~3문장, 자연스러운 한국어로 요약한다.
총 지출과 가장 많이 쓴 카테고리를 위주로 언급한다.
금액은 천 단위 콤마를 넣어 "12,345원"처럼 표기한다.
"""


def _fetch_week_summary() -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        totals = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count
            FROM transactions
            WHERE decision = 'confirm' AND category IS NOT NULL
              AND created_at >= datetime('now', '-7 days')
            """
        ).fetchone()
        by_category = conn.execute(
            """
            SELECT category, SUM(amount) AS total, COUNT(*) AS count
            FROM transactions
            WHERE decision = 'confirm' AND category IS NOT NULL
              AND created_at >= datetime('now', '-7 days')
            GROUP BY category
            ORDER BY total DESC
            """
        ).fetchall()
        return {
            "total": totals["total"],
            "count": totals["count"],
            "by_category": [dict(row) for row in by_category],
        }
    finally:
        conn.close()


def generate_weekly_briefing() -> dict:
    data = _fetch_week_summary()
    if data["count"] == 0:
        return {**data, "summary": "이번 주는 기록된 소비가 없어요."}

    try:
        response = litellm.completion(
            model=GEMINI_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": BRIEFING_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ],
        )
        summary = response.choices[0].message.content.strip()
    except RateLimitError:
        summary = "지금 Gemini 무료 할당량이 다 차서 요약 문장을 못 만들었어요. 아래 집계 숫자는 정상입니다."

    return {**data, "summary": summary}
