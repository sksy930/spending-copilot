"""자연어 QA — 집계형 질문 경로, 에이전트 루프 (docs/project-overview.md 3.4.3).

가맹점 판단 루프(app/merchant_judgment.py)와 같은 패턴: 매 스텝 LLM이 "지금 결과로
답할 수 있는가, 도구를 더 불러야 하는가"를 스스로 결정한다. "지난달보다 늘었어?"처럼
쿼리 하나로 안 풀리는 질문에서 다음 조회를 스스로 판단해 이어가는 게 핵심이다.

과거엔 LLM이 SQL 문 전체를 텍스트로 생성했는데(안전성 검증을 정규식으로 따로 해야 했음),
지금은 `query_spending` 하나짜리 네이티브 function-calling 도구로 바꿨다. LLM은 기간/
카테고리/가맹점 같은 짧은 인자만 채우고, 실제 SQL은 파이썬에서 파라미터 바인딩으로
조립·실행한다 — 프롬프트에서 날짜 계산 규칙과 SQL 안전 규칙을 통째로 뺄 수 있어 매 스텝
토큰이 줄고, SQL 인젝션 여지도 구조적으로 없어진다.

가맹점 불명확 질문(Chroma RAG) 경로는 이번 스코프에서 제외한다 (3.3 참고,
RAG는 미매칭 거래량이 늘어나면 재검토).
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional, TypedDict

import litellm
import psycopg2
from langgraph.graph import END, StateGraph
from litellm.exceptions import RateLimitError

from app.db import connect
from app.merchant_judgment import CATEGORIES

GROQ_MODEL = os.environ.get("GROQ_MODEL", "groq/openai/gpt-oss-20b")
MAX_QUERY_STEPS = 3
RATE_LIMIT_RETRY_ATTEMPTS = 3
RATE_LIMIT_BACKOFF_SECONDS = 20

QUERY_AGENT_SYSTEM_PROMPT = """너는 개인 소비 데이터에 대해 질문에 답하는 에이전트다.

query_spending 도구로 필요한 만큼 조회한 뒤(비교 질문이면 여러 번 호출해도 된다),
충분히 답할 수 있게 되면 도구 호출 없이 한국어 답변 문장만 출력해서 끝낸다.

오늘 날짜(UTC): {today}

규칙:
- "카페 얼마 썼어"처럼 위 카테고리 목록에 있는 단어가 나오면 category를 채운다.
  merchant는 가맹점명이 명확히 특정됐을 때만 쓴다 (예: "스타벅스에서 얼마 썼어").
  category와 merchant를 같이 채우지 않는다.
- "지난달보다 늘었어?"처럼 비교/추세 질문은 query_spending을 필요한 기간만큼
  (예: this_month, last_month) 각각 호출해서 비교한다.
- 답변은 한두 문장, 간결하게. 금액은 천 단위 콤마를 넣어 "12,345원"처럼 표기.
- 조회해도 조건에 맞는 데이터가 없으면 억지로 추측하지 말고 그렇다고 답한다.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_spending",
            "description": "기간(및 선택적으로 카테고리/가맹점) 조건으로 확정된 소비를 집계 조회한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["this_week", "last_week", "this_month", "last_month", "all"],
                        "description": (
                            "this_week=최근 7일, last_week=8~14일 전, this_month=이번 달, "
                            "last_month=지난달, all=전체 기간"
                        ),
                    },
                    "category": {
                        "type": "string",
                        "enum": list(CATEGORIES),
                        "description": "특정 카테고리만 볼 때만 채운다. 비우면 카테고리별로 나눠서 반환.",
                    },
                    "merchant": {
                        "type": "string",
                        "description": "가맹점명이 질문에 명확히 나왔을 때만 채운다 (부분 일치 검색).",
                    },
                },
                "required": ["period"],
            },
        },
    }
]

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)

_PERIOD_CLAUSES = {
    "this_week": "created_at >= NOW() - INTERVAL '7 days'",
    "last_week": "created_at >= NOW() - INTERVAL '14 days' AND created_at < NOW() - INTERVAL '7 days'",
    "this_month": "TO_CHAR(created_at, 'YYYY-MM') = TO_CHAR(NOW(), 'YYYY-MM')",
    "last_month": "TO_CHAR(created_at, 'YYYY-MM') = TO_CHAR(NOW() - INTERVAL '1 month', 'YYYY-MM')",
    "all": "TRUE",
}


class QueryError(Exception):
    pass


def query_spending(period: str, category: Optional[str] = None, merchant: Optional[str] = None) -> dict:
    """query_spending 도구의 실제 구현. LLM이 채운 인자로 파라미터 바인딩된 SQL을 조립·실행한다."""
    if period not in _PERIOD_CLAUSES:
        raise QueryError(f"알 수 없는 기간: {period}")
    if category is not None and category not in CATEGORIES:
        raise QueryError(f"알 수 없는 카테고리: {category}")

    where = ["decision = 'confirm'", "category IS NOT NULL", _PERIOD_CLAUSES[period]]
    params: list = []
    if category:
        where.append("category = %s")
        params.append(category)
    if merchant:
        where.append("merchant ILIKE %s")
        params.append(f"%{merchant}%")

    sql = (
        "SELECT category, SUM(amount) AS total, COUNT(*) AS count FROM transactions "
        f"WHERE {' AND '.join(where)} GROUP BY category ORDER BY total DESC"
    )
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [dict(row) for row in cur.fetchall()]
    except psycopg2.Error as exc:
        raise QueryError(f"쿼리 실행에 실패했어요: {exc}") from exc

    return {
        "total": sum(r["total"] for r in rows),
        "count": sum(r["count"] for r in rows),
        "by_category": rows,
    }


def _call_query_agent(messages: list[dict]):
    """Groq 무료 티어의 요청 제한(429)에 걸리면 잠깐 대기 후 재시도한다."""
    for attempt in range(1, RATE_LIMIT_RETRY_ATTEMPTS + 1):
        try:
            response = litellm.completion(model=GROQ_MODEL, temperature=0, messages=messages, tools=TOOLS)
            return response.choices[0].message
        except RateLimitError:
            if attempt == RATE_LIMIT_RETRY_ATTEMPTS:
                raise QueryError("지금 LLM 무료 할당량이 다 찼어요. 잠시 후 다시 시도해주세요.")
            print(f"  [rate limit] {RATE_LIMIT_BACKOFF_SECONDS}초 대기 후 재시도 ({attempt}/{RATE_LIMIT_RETRY_ATTEMPTS})")
            time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
    raise RuntimeError("unreachable")


class QAState(TypedDict):
    messages: list[dict]
    step: int
    trace: list[dict]
    pending_tool_calls: Optional[list]
    answer: Optional[str]


def _decide_node(state: QAState) -> dict:
    message = _call_query_agent(state["messages"])
    step = state["step"] + 1

    if message.tool_calls:
        assistant_msg = {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in message.tool_calls
            ],
        }
        return {
            "messages": state["messages"] + [assistant_msg],
            "step": step,
            "pending_tool_calls": message.tool_calls,
        }

    answer = _THINK_RE.sub("", message.content or "").strip()
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": answer}],
        "step": step,
        "answer": answer,
    }


def _route_after_decide(state: QAState) -> str:
    return "end" if state.get("answer") is not None else "run_tools"


def _run_tools_node(state: QAState) -> dict:
    tool_messages = []
    trace = list(state["trace"])
    for call in state["pending_tool_calls"]:
        try:
            args = json.loads(call.function.arguments)
        except json.JSONDecodeError as exc:
            raise QueryError("도구 호출 형식이 잘못됐어요.") from exc

        period, category, merchant = args.get("period"), args.get("category"), args.get("merchant")
        try:
            result = query_spending(period=period, category=category, merchant=merchant)
        except QueryError as exc:
            result = {"error": str(exc)}

        trace.append({"period": period, "category": category, "merchant": merchant, "result": result})
        tool_messages.append(
            {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False, default=str)}
        )

    if state["step"] >= MAX_QUERY_STEPS:
        raise QueryError(f"질문이 복잡해서 {MAX_QUERY_STEPS}번 조회해도 답을 못 정했어요. 더 구체적으로 물어봐 주세요.")
    return {"messages": state["messages"] + tool_messages, "trace": trace, "pending_tool_calls": None}


def _build_qa_graph():
    graph = StateGraph(QAState)
    graph.add_node("decide", _decide_node)
    graph.add_node("run_tools", _run_tools_node)
    graph.set_entry_point("decide")
    graph.add_conditional_edges("decide", _route_after_decide, {"end": END, "run_tools": "run_tools"})
    graph.add_edge("run_tools", "decide")
    return graph.compile()


_qa_graph = _build_qa_graph()


def answer_question(question: str) -> dict:
    """자연어 QA — 집계형 질문 에이전트 루프 (docs 3.4.3) — LangGraph StateGraph로 구현.

    decide 노드가 매 스텝 "지금 답할 수 있는가, query_spending을 더 불러야 하는가"를
    스스로 결정하고, 도구 호출이면 run_tools로 넘어갔다가 다시 decide로 돌아가는 루프다.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    initial_state: QAState = {
        "messages": [
            {"role": "system", "content": QUERY_AGENT_SYSTEM_PROMPT.format(today=today)},
            {"role": "user", "content": question},
        ],
        "step": 0,
        "trace": [],
        "pending_tool_calls": None,
        "answer": None,
    }
    final_state = _qa_graph.invoke(initial_state)
    return {
        "answer": final_state["answer"],
        "calls": final_state["trace"],
        "steps": final_state["step"],
    }
