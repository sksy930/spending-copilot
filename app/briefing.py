"""주간 브리핑 그래프 (docs/project-overview.md 3.4.2, 3.5 LLM 라우팅).

Slack 대신 웹 화면에서 보여준다 — 별도 스케줄러 없이 페이지를 열 때마다 최근 7일치를
즉시 집계·요약한다. "해당 주"라는 고정 기간 SQL 스캔 결과를 LLM 요약기에 넣는 것이라
RAG가 아니다 (docs 3.4.2 참고). 도구 호출이 필요 없는 단순 요약이라 Groq를 쓴다.
fetch → summarize 순서가 고정된 워크플로우이며 에이전트 루프는 아니다.
"""

import json
import os
from typing import TypedDict

import litellm
from langgraph.graph import END, StateGraph
from litellm.exceptions import RateLimitError

from app.db import connect

GROQ_MODEL = os.environ.get("GROQ_MODEL", "groq/llama-3.3-70b-versatile")

BRIEFING_SYSTEM_PROMPT = """너는 개인 소비 데이터를 바탕으로 주간 브리핑을 써주는 비서다.
주어진 최근 7일 집계 데이터를 근거로 2~3문장, 자연스러운 한국어로 요약한다.
총 지출과 가장 많이 쓴 카테고리를 위주로 언급한다.
금액은 천 단위 콤마를 넣어 "12,345원"처럼 표기한다.
"""


class BriefingState(TypedDict, total=False):
    total: int
    count: int
    by_category: list[dict]
    summary: str


def _fetch_node(state: BriefingState) -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count
            FROM transactions
            WHERE decision = 'confirm' AND category IS NOT NULL
              AND created_at >= NOW() - INTERVAL '7 days'
            """
        )
        totals = cur.fetchone()
        cur.execute(
            """
            SELECT category, SUM(amount) AS total, COUNT(*) AS count
            FROM transactions
            WHERE decision = 'confirm' AND category IS NOT NULL
              AND created_at >= NOW() - INTERVAL '7 days'
            GROUP BY category
            ORDER BY total DESC
            """
        )
        by_category = cur.fetchall()
        return {
            "total": totals["total"],
            "count": totals["count"],
            "by_category": [dict(row) for row in by_category],
        }


def _summarize_node(state: BriefingState) -> dict:
    if state["count"] == 0:
        return {"summary": "이번 주는 기록된 소비가 없어요."}

    try:
        response = litellm.completion(
            model=GROQ_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": BRIEFING_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "total": state["total"],
                            "count": state["count"],
                            "by_category": state["by_category"],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        summary = response.choices[0].message.content.strip()
    except RateLimitError:
        summary = "지금 LLM 무료 할당량이 다 차서 요약 문장을 못 만들었어요. 아래 집계 숫자는 정상입니다."

    return {"summary": summary}


def _build_briefing_graph():
    graph = StateGraph(BriefingState)
    graph.add_node("fetch", _fetch_node)
    graph.add_node("summarize", _summarize_node)
    graph.set_entry_point("fetch")
    graph.add_edge("fetch", "summarize")
    graph.add_edge("summarize", END)
    return graph.compile()


_briefing_graph = _build_briefing_graph()


def generate_weekly_briefing() -> dict:
    return _briefing_graph.invoke({})
