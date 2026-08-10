"""소비 대시보드 집계 (일별 합계 + 카테고리별 합계, 전체 기간).

그래프(LangGraph)도 에이전트도 아닌 단순 SQL 집계라 db.py 옆에 별도 모듈로 둔다 —
브리핑(최근 7일 고정)과 달리 대시보드는 저장된 전체 기간을 보여준다.
"""

from app.db import connect


def _fetch_daily_totals(cur) -> list[dict]:
    cur.execute(
        """
        WITH bounds AS (
            SELECT
                MIN((created_at AT TIME ZONE 'Asia/Seoul')::date) AS min_date,
                MAX((created_at AT TIME ZONE 'Asia/Seoul')::date) AS max_date
            FROM transactions
            WHERE decision = 'confirm' AND category IS NOT NULL
        ),
        days AS (
            SELECT generate_series(min_date, max_date, interval '1 day')::date AS date
            FROM bounds
            WHERE min_date IS NOT NULL
        ),
        totals AS (
            SELECT (created_at AT TIME ZONE 'Asia/Seoul')::date AS date, SUM(amount) AS total
            FROM transactions
            WHERE decision = 'confirm' AND category IS NOT NULL
            GROUP BY date
        )
        SELECT days.date, COALESCE(totals.total, 0) AS total
        FROM days LEFT JOIN totals ON totals.date = days.date
        ORDER BY days.date
        """
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_weekly_totals(cur) -> list[dict]:
    cur.execute(
        """
        SELECT date_trunc('week', created_at AT TIME ZONE 'Asia/Seoul')::date AS week_start,
               SUM(amount) AS total
        FROM transactions
        WHERE decision = 'confirm' AND category IS NOT NULL
        GROUP BY week_start
        ORDER BY week_start
        """
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_category_totals(cur) -> list[dict]:
    cur.execute(
        """
        SELECT category, SUM(amount) AS total, COUNT(*) AS count
        FROM transactions
        WHERE decision = 'confirm' AND category IS NOT NULL
        GROUP BY category
        ORDER BY total DESC
        """
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_current_month_total(cur) -> int:
    cur.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE decision = 'confirm' AND category IS NOT NULL
          AND TO_CHAR(created_at AT TIME ZONE 'Asia/Seoul', 'YYYY-MM')
              = TO_CHAR(NOW() AT TIME ZONE 'Asia/Seoul', 'YYYY-MM')
        """
    )
    return cur.fetchone()["total"]


def fetch_spending_overview() -> dict:
    with connect() as conn, conn.cursor() as cur:
        daily = _fetch_daily_totals(cur)
        weekly = _fetch_weekly_totals(cur)
        by_category = _fetch_category_totals(cur)
        month_total = _fetch_current_month_total(cur)
    return {
        "daily": daily,
        "weekly": weekly,
        "by_category": by_category,
        "total": sum(row["total"] for row in by_category),
        "count": sum(row["count"] for row in by_category),
        "month_total": month_total,
    }
