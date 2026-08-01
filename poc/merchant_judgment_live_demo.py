"""가맹점 판단 에이전트 루프의 실LLM 데모 (docs/project-overview.md 3.4.1).

merchant_judgment_poc.py의 MockLLM 대신 LiteLLM으로 실제 Gemini를 호출해
같은 트레이스가 스크립트가 아닌 실제 판단으로 재현되는지 보여준다.
search_merchant 웹 검색 도구는 이번 데모에서도 mock을 유지한다
(3.5 "웹 검색 도구: TBD" — 실제 검색 API 연동은 이후 주차 과제).
"""

import json
import os
from dataclasses import dataclass
from typing import Optional

import litellm
from dotenv import load_dotenv

load_dotenv()

MAX_RETRIES = 2
SIMILARITY_THRESHOLD = 0.75
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini/gemini-flash-latest")

SYSTEM_PROMPT = """너는 체크카드 거래의 가맹점 카테고리를 판단하는 에이전트다.
매 스텝마다 아래 JSON 스키마로만 응답한다 (다른 설명 문장 없이 JSON 객체 하나만 출력):

{
  "decision": "confirm" | "search" | "escalate",
  "category": "string (decision=confirm일 때만)",
  "search_query": "string (decision=search일 때만)",
  "confidence": 0.0에서 1.0 사이 숫자,
  "reason": "판단 근거"
}

규칙:
- 가맹점명만으로 업종을 확신할 수 있으면 decision=confirm.
- 배달앱 대행 결제(예: "쿠팡이츠*상호명")처럼 실제 업종이 표면 이름과 다를 수 있으면 decision=search로 웹 검색을 요청한다.
- 검색 결과를 받았고 그걸로 업종이 특정되면 decision=confirm.
- 검색해도 특정이 안 되면 decision=escalate.
"""


@dataclass
class Transaction:
    merchant: str
    amount: int
    chroma_similarity: float  # 0~1, mocked 값 (past_transactions 컬렉션 유사도)


@dataclass
class Decision:
    decision: str
    category: Optional[str] = None
    search_query: Optional[str] = None
    confidence: Optional[float] = None
    reason: str = ""


class MockSearchTool:
    """search_merchant 툴의 mock 구현. 쿼리에 포함된 키워드로 스니펫을 매칭한다.

    실제 LLM이 생성하는 검색어 문구는 스크립트로 고정할 수 없으므로,
    (기존 poc의 dict 완전일치 대신) 키워드 포함 여부로 매칭한다.
    """

    def __init__(self, canned: dict[str, str]):
        self._canned = canned  # keyword -> snippet
        self.call_log: list[str] = []

    def search_merchant(self, query: str) -> str:
        self.call_log.append(query)
        for keyword, snippet in self._canned.items():
            if keyword in query:
                return snippet
        return "검색 결과 없음"


def _parse_decision(raw_text: str) -> Decision:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            first_line, rest = text.split("\n", 1)
            text = rest if first_line.strip().lower() in ("json", "") else text
    data = json.loads(text)
    return Decision(
        decision=data["decision"],
        category=data.get("category"),
        search_query=data.get("search_query"),
        confidence=data.get("confidence"),
        reason=data.get("reason", ""),
    )


def call_llm(messages: list[dict]) -> Decision:
    response = litellm.completion(model=GEMINI_MODEL, messages=messages, temperature=0)
    raw = response.choices[0].message.content
    return _parse_decision(raw)


def run_merchant_judgment_loop(transaction: Transaction, tool: MockSearchTool) -> Decision:
    print(f"[입력] {transaction.merchant} / {transaction.amount}원 / past_transactions 유사도 {transaction.chroma_similarity}")

    if transaction.chroma_similarity >= SIMILARITY_THRESHOLD:
        print("  -> 유사도 충분, 루프 진입 없이 즉시 확정")
        return Decision(decision="confirm", category="(유사 거래 기반 확정)", confidence=transaction.chroma_similarity)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"가맹점명: {transaction.merchant}\n"
                f"금액: {transaction.amount}원\n"
                f"past_transactions 유사도: {transaction.chroma_similarity} (임계치 {SIMILARITY_THRESHOLD} 미달)"
            ),
        },
    ]

    for step in range(1, MAX_RETRIES + 1):
        decision = call_llm(messages)
        print(f"  [LLM 호출 {step}] {decision}")
        messages.append({"role": "assistant", "content": json.dumps(decision.__dict__, ensure_ascii=False)})

        if decision.decision == "confirm":
            print(f"  -> 확정: {decision.category} (confidence={decision.confidence})")
            return decision
        if decision.decision == "escalate":
            print("  -> 리뷰 큐로 이동 (LLM이 직접 escalate 선택)")
            return decision

        snippet = tool.search_merchant(decision.search_query or "")
        print(f"  [검색] '{decision.search_query}' -> {snippet}")
        messages.append({"role": "user", "content": f"검색 결과: {snippet}"})

    print(f"  -> 재시도 한도({MAX_RETRIES}) 초과, 강제 escalate")
    return Decision(decision="escalate", reason=f"재시도 {MAX_RETRIES}회 초과")


def example_delivery_app_merchant() -> None:
    """배달앱 대행 결제형 가맹점 — 검색 1회 후 확정되는 경로 (실제 Gemini 호출)."""
    print("=== 예시 1: 배달앱 결제형 가맹점 ===")
    transaction = Transaction(merchant="쿠팡이츠*엔제리너스", amount=8900, chroma_similarity=0.42)
    tool = MockSearchTool({"엔제리너스": "엔제리너스 - 커피전문점 프랜차이즈"})
    result = run_merchant_judgment_loop(transaction, tool)
    print(f"[결과] {result}\n")


def example_ambiguous_merchant() -> None:
    """사업자번호형 가맹점명 — 검색해도 특정 안 되어 리뷰 큐로 넘어가는 경로 (실제 Gemini 호출)."""
    print("=== 예시 2: 사업자번호형 가맹점명 ===")
    transaction = Transaction(merchant="(주)미상거래12345", amount=15000, chroma_similarity=0.10)
    tool = MockSearchTool({})
    result = run_merchant_judgment_loop(transaction, tool)
    print(f"[결과] {result}\n")


if __name__ == "__main__":
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit(".env에 GEMINI_API_KEY를 설정하세요 (.env.example 참고)")
    example_delivery_app_merchant()
    example_ambiguous_merchant()
