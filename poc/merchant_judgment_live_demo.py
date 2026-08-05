"""가맹점 판단 에이전트 루프의 실LLM 데모 (docs/project-overview.md 3.4.1).

merchant_judgment_poc.py의 MockLLM 대신 LiteLLM으로 실제 Gemini를 호출해
같은 트레이스가 스크립트가 아닌 실제 판단으로 재현되는지 보여준다.
search_merchant 웹 검색 도구는 이번 데모에서도 mock을 유지한다
(3.5 "웹 검색 도구: TBD" — 실제 검색 API 연동은 이후 주차 과제).

루프 본체는 app/merchant_judgment.py에 있다 — 웹훅 파이프라인(app/main.py)과
공유하는 로직이라 여기서는 예시 트랜잭션만 정의하고 결과를 SQLite에 저장한다.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import insert_transaction  # noqa: E402
from app.merchant_judgment import (  # noqa: E402
    Decision,
    MockSearchTool,
    Transaction,
    run_merchant_judgment_loop,
)


def _persist(transaction: Transaction, result: Decision) -> None:
    insert_transaction(
        merchant=transaction.merchant,
        amount=transaction.amount,
        category=result.category,
        decision=result.decision,
        confidence=result.confidence,
        reason=result.reason,
    )


def example_delivery_app_merchant() -> None:
    """배달앱 대행 결제형 가맹점 — 검색 1회 후 확정되는 경로 (실제 Gemini 호출)."""
    print("=== 예시 1: 배달앱 결제형 가맹점 ===")
    transaction = Transaction(merchant="쿠팡이츠*엔제리너스", amount=8900)
    tool = MockSearchTool({"엔제리너스": "엔제리너스 - 커피전문점 프랜차이즈"})
    result = run_merchant_judgment_loop(transaction, tool)
    print(f"[결과] {result}\n")
    _persist(transaction, result)


def example_ambiguous_merchant() -> None:
    """사업자번호형 가맹점명 — 검색해도 특정 안 되어 리뷰 큐로 넘어가는 경로 (실제 Gemini 호출)."""
    print("=== 예시 2: 사업자번호형 가맹점명 ===")
    transaction = Transaction(merchant="(주)미상거래12345", amount=15000)
    tool = MockSearchTool({})
    result = run_merchant_judgment_loop(transaction, tool)
    print(f"[결과] {result}\n")
    _persist(transaction, result)


if __name__ == "__main__":
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit(".env에 GEMINI_API_KEY를 설정하세요 (.env.example 참고)")
    example_delivery_app_merchant()
    example_ambiguous_merchant()
