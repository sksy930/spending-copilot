"""가맹점 판단 에이전트 루프의 최소 재현 스크립트.

실제 LLM/웹 검색 대신 스크립트로 정해진 응답을 반환하는 목(mock)을 사용해
docs/project-overview.md 3.4.1의 트레이스를 그대로 재현한다.
"""

from dataclasses import dataclass
from typing import Optional

MAX_RETRIES = 2
SIMILARITY_THRESHOLD = 0.75


@dataclass
class Transaction:
    merchant: str
    amount: int
    chroma_similarity: float  # 0~1, mocked 값


@dataclass
class Decision:
    decision: str  # "confirm" | "search" | "escalate"
    category: Optional[str] = None
    search_query: Optional[str] = None
    confidence: Optional[float] = None
    reason: str = ""


class MockLLM:
    """호출 순서대로 미리 정해둔 Decision을 반환하는 스크립트 LLM."""

    def __init__(self, scripted_decisions: list[Decision]):
        self._script = list(scripted_decisions)
        self.call_count = 0

    def decide(self) -> Decision:
        self.call_count += 1
        if not self._script:
            raise RuntimeError("스크립트에 정의되지 않은 LLM 호출")
        return self._script.pop(0)


class MockSearchTool:
    """search_merchant 툴의 mock 구현. 쿼리 -> 스니펫 매핑."""

    def __init__(self, canned: dict[str, str]):
        self._canned = canned
        self.call_log: list[str] = []

    def search_merchant(self, query: str) -> str:
        self.call_log.append(query)
        return self._canned.get(query, "검색 결과 없음")


def run_merchant_judgment_loop(
    transaction: Transaction,
    llm: MockLLM,
    tool: MockSearchTool,
) -> Decision:
    """3.4.1 가맹점 판단 루프. 에이전트 루프이므로 매 스텝 LLM이 종료/재검색을 스스로 결정한다."""
    print(f"[입력] {transaction.merchant} / {transaction.amount}원 / Chroma 유사도 {transaction.chroma_similarity}")

    if transaction.chroma_similarity >= SIMILARITY_THRESHOLD:
        print("  -> 유사도 충분, 루프 진입 없이 즉시 확정")
        return Decision(decision="confirm", category="(유사 거래 기반 확정)", confidence=transaction.chroma_similarity)

    for _ in range(MAX_RETRIES):
        decision = llm.decide()
        print(f"  [LLM 호출 {llm.call_count}] {decision}")

        if decision.decision == "confirm":
            print(f"  -> 확정: {decision.category} (confidence={decision.confidence})")
            return decision

        if decision.decision == "escalate":
            print("  -> 리뷰 큐로 이동 (LLM이 직접 escalate 선택)")
            return decision

        # decision == "search"
        snippet = tool.search_merchant(decision.search_query)
        print(f"  [검색] '{decision.search_query}' -> {snippet}")

    print(f"  -> 재시도 한도({MAX_RETRIES}) 초과, 강제 escalate")
    return Decision(decision="escalate", reason=f"재시도 {MAX_RETRIES}회 초과")


def example_confirm_after_search() -> None:
    """docs 3.4.1의 예시: 검색 1회 후 확정되는 정상 경로."""
    print("=== 예시 1: 검색 후 확정 ===")
    transaction = Transaction(merchant="쿠팡이츠*엔제리너스", amount=8900, chroma_similarity=0.42)
    llm = MockLLM([
        Decision(decision="search", search_query="쿠팡이츠 엔제리너스 업종", reason="배달앱 결제형이라 실제 업종 불명확"),
        Decision(decision="confirm", category="카페", confidence=0.88, reason="검색 결과로 커피전문점 확인"),
    ])
    tool = MockSearchTool({"쿠팡이츠 엔제리너스 업종": "엔제리너스 - 커피전문점 프랜차이즈"})

    result = run_merchant_judgment_loop(transaction, llm, tool)
    assert result.decision == "confirm" and result.category == "카페"
    print(f"[결과] {result}\n")


def example_escalate_after_retry_limit() -> None:
    """재시도 한도를 넘겨도 확신이 서지 않아 리뷰 큐로 넘어가는 경로 (안전장치 검증)."""
    print("=== 예시 2: 재시도 한도 초과 -> 리뷰 큐 ===")
    transaction = Transaction(merchant="(주)미상거래12345", amount=15000, chroma_similarity=0.10)
    llm = MockLLM([
        Decision(decision="search", search_query="(주)미상거래12345", reason="가맹점명이 사업자번호 형태라 단서 부족"),
        Decision(decision="search", search_query="미상거래 업종 조회", reason="1차 검색 결과로도 특정 불가"),
    ])
    tool = MockSearchTool({
        "(주)미상거래12345": "검색 결과 없음",
        "미상거래 업종 조회": "관련성 낮은 결과만 존재",
    })

    result = run_merchant_judgment_loop(transaction, llm, tool)
    assert result.decision == "escalate"
    print(f"[결과] {result}\n")


if __name__ == "__main__":
    example_confirm_after_search()
    example_escalate_after_retry_limit()
