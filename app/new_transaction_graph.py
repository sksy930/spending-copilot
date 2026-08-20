"""신규 거래 처리 그래프 (docs/project-overview.md 3.4.1).

0차 정확 매칭(워크플로우, AI 미사용) → 미스 시 가맹점 판단 에이전트 루프
(app/merchant_judgment.py) → 결과 저장. LLM 장애로 판단 자체를 못 하면
거래를 잃지 않고 리뷰 큐로 이관한다.
"""

from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

from app import db
from app.merchant_judgment import RealSearchTool, Transaction, run_merchant_judgment_loop
from app.notify import notify_review_needed


class NewTransactionState(TypedDict):
    merchant: str
    amount: int
    category: Optional[str]
    decision: Optional[str]
    confidence: Optional[float]
    reason: str
    judgment_failed: bool


def _zero_shot_match_node(state: NewTransactionState) -> dict:
    category = db.lookup_category(state["merchant"])
    if category is None:
        return {}
    return {
        "category": category,
        "decision": "confirm",
        "confidence": 1.0,
        "reason": "0차 정확 매칭",
    }


def _route_after_zero_shot(state: NewTransactionState) -> str:
    return "persist" if state.get("decision") == "confirm" else "judge"


def _judge_node(state: NewTransactionState) -> dict:
    tool = RealSearchTool()  # Gemini Google Search grounding (docs 3.5)
    try:
        result = run_merchant_judgment_loop(
            Transaction(merchant=state["merchant"], amount=state["amount"]), tool
        )
    except Exception as exc:  # noqa: BLE001 — LLM 장애로 거래를 통째로 잃으면 안 됨
        return {
            "judgment_failed": True,
            "reason": f"{state['merchant']} / {state['amount']}원 — 가맹점 판단 실패: {exc}",
        }
    return {
        "category": result.category,
        "decision": result.decision,
        "confidence": result.confidence,
        "reason": result.reason,
    }


def _route_after_judge(state: NewTransactionState) -> str:
    return "review" if state.get("judgment_failed") else "persist"


def _persist_node(state: NewTransactionState) -> dict:
    transaction_id = db.insert_transaction(
        merchant=state["merchant"],
        amount=state["amount"],
        category=state.get("category"),
        decision=state["decision"],
        confidence=state.get("confidence"),
        reason=state.get("reason", ""),
    )
    if state["decision"] == "confirm" and state.get("category"):
        db.upsert_merchant_category(state["merchant"], state["category"])
    elif state["decision"] == "escalate":
        try:
            notify_review_needed(transaction_id, state["merchant"], state["amount"])
        except Exception as exc:  # noqa: BLE001 — 알림 실패로 거래 저장 자체가 실패하면 안 됨
            print(f"  [알림 발송 실패] {exc}")
    return {}


def _review_node(state: NewTransactionState) -> dict:
    db.insert_review(reason="llm_unavailable", raw_text=state.get("reason"))
    return {}


def _build_new_transaction_graph():
    graph = StateGraph(NewTransactionState)
    graph.add_node("zero_shot", _zero_shot_match_node)
    graph.add_node("judge", _judge_node)
    graph.add_node("persist", _persist_node)
    graph.add_node("review", _review_node)
    graph.set_entry_point("zero_shot")
    graph.add_conditional_edges(
        "zero_shot", _route_after_zero_shot, {"persist": "persist", "judge": "judge"}
    )
    graph.add_conditional_edges(
        "judge", _route_after_judge, {"persist": "persist", "review": "review"}
    )
    graph.add_edge("persist", END)
    graph.add_edge("review", END)
    return graph.compile()


_new_transaction_graph = _build_new_transaction_graph()


def process_new_transaction(merchant: str, amount: int) -> None:
    """0차 정확 매칭 → 가맹점 판단 에이전트 루프 (3.4.1)."""
    _new_transaction_graph.invoke(
        {
            "merchant": merchant,
            "amount": amount,
            "category": None,
            "decision": None,
            "confidence": None,
            "reason": "",
            "judgment_failed": False,
        }
    )
