"""가맹점 판단 에이전트 루프 (docs/project-overview.md 3.4.1, 3.5 LLM 라우팅).

0차 정확 매칭에서 걸러지지 않은 거래를 LLM이 스스로 웹 검색 여부를 결정하며
판단한다. poc/merchant_judgment_live_demo.py와 웹훅 파이프라인(app/main.py)이
이 모듈을 공유한다.

판단(confirm/search/escalate)은 도구 호출이 필요 없는 저지연 분류 작업이라 Groq를
쓰고, 실제 웹 검색만 Gemini의 Google Search grounding을 쓴다 (Groq엔 이 기능이
없음) — docs 3.5가 원래 의도했던 라우팅에 가깝다.
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Optional

import litellm
from litellm.exceptions import RateLimitError

MAX_RETRIES = 2
RATE_LIMIT_RETRY_ATTEMPTS = 3
RATE_LIMIT_BACKOFF_SECONDS = 20
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini/gemini-flash-latest")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "groq/llama-3.3-70b-versatile")

CATEGORIES = ("음식점", "카페", "베이커리", "편의점", "쇼핑", "의류", "식료품", "교육", "여가/오락", "의료", "미용", "교통", "기타")

SYSTEM_PROMPT = """너는 체크카드 거래의 가맹점 카테고리를 판단하는 에이전트다.
매 스텝마다 아래 JSON 스키마로만 응답한다 (다른 설명 문장 없이 JSON 객체 하나만 출력):

{
  "reason": "판단 근거를 먼저 한 문장으로 쓴다 (decision/category는 이 reason을 쓴 뒤에 정한다)",
  "decision": "confirm" | "search" | "escalate",
  "category": "다음 목록 중 하나, 반드시 위 reason과 같은 업종이어야 함 (decision=confirm일 때만): """ + ", ".join(CATEGORIES) + """",
  "search_query": "string (decision=search일 때만)",
  "confidence": 0.0에서 1.0 사이 숫자
}

규칙:
- 반드시 reason을 먼저 정하고, 그 reason에 맞춰 decision/category를 고른다 (거꾸로 category부터 정하고 reason을 끼워맞추지 않는다).
- 가맹점명만으로 업종을 확신할 수 있으면 decision=confirm.
- category는 반드시 위 목록 중 하나이며 reason의 내용과 모순되면 안 된다 (예: reason이 "커피숍"이라고 했으면 category는 "카페"여야 한다 — "음식점"처럼 다른 값을 쓰면 안 된다). 목록에 뚜렷이 맞는 게 없으면 "기타"를 쓴다 (새 카테고리 이름을 만들어내지 않는다).
- 배달앱 대행 결제(예: "쿠팡이츠*상호명")처럼 실제 업종이 표면 이름과 다를 수 있으면 decision=search로 웹 검색을 요청한다.
- 검색 결과를 받았고 그걸로 업종이 특정되면 decision=confirm.
- 검색해도 특정이 안 되면 decision=escalate.
"""


@dataclass
class Transaction:
    merchant: str
    amount: int


@dataclass
class Decision:
    decision: str
    category: Optional[str] = None
    search_query: Optional[str] = None
    confidence: Optional[float] = None
    reason: str = ""


class MockSearchTool:
    """search_merchant 툴의 mock 구현. 쿼리에 포함된 키워드로 스니펫을 매칭한다.

    poc/merchant_judgment_poc.py, poc/merchant_judgment_live_demo.py처럼 docs 3.4.1의
    정해진 트레이스를 재현성 있게 보여줘야 하는 데모용. 실 파이프라인(웹훅, 배치 재생)은
    RealSearchTool을 쓴다.
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


class RealSearchTool:
    """search_merchant 툴의 실 구현 — Gemini의 Google Search grounding으로 검색한다 (docs 3.5).

    grounding 호출이 실패하면(429는 상위로 전파, 그 외 스키마/네트워크 에러는) 루프가
    깨지지 않도록 "검색 결과 없음"으로 안전하게 폴백한다.
    """

    def __init__(self):
        self.call_log: list[str] = []

    def search_merchant(self, query: str) -> str:
        self.call_log.append(query)
        try:
            response = litellm.completion(
                model=GEMINI_MODEL,
                temperature=0,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"'{query}'가 어떤 업종/가게인지 웹에서 검색해서 한 문장으로 요약해줘. "
                            "확실하지 않으면 '모르겠음'이라고만 답해."
                        ),
                    }
                ],
                tools=[{"googleSearch": {}}],
            )
            return response.choices[0].message.content.strip()
        except RateLimitError:
            raise
        except Exception as exc:  # noqa: BLE001 — 검색 실패는 루프 전체를 깨면 안 됨
            print(f"  [실 웹 검색 실패, 빈 결과로 대체] {exc}")
            return "검색 결과 없음"


def _parse_decision(raw_text: str) -> Decision:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            first_line, rest = text.split("\n", 1)
            text = rest if first_line.strip().lower() in ("json", "") else text
    data = json.loads(text)
    category = data.get("category")
    if data["decision"] == "confirm" and category not in CATEGORIES:
        category = "기타"  # 모델이 목록 밖 카테고리를 만들어내면 캐시 오염을 막기 위해 강제 보정
    return Decision(
        decision=data["decision"],
        category=category,
        search_query=data.get("search_query"),
        confidence=data.get("confidence"),
        reason=data.get("reason", ""),
    )


def call_llm(messages: list[dict]) -> Decision:
    """판단 전용 — Groq. 무료 티어 요청 제한(429)에 걸리면 잠깐 대기 후 재시도한다."""
    for attempt in range(1, RATE_LIMIT_RETRY_ATTEMPTS + 1):
        try:
            response = litellm.completion(model=GROQ_MODEL, messages=messages, temperature=0)
            return _parse_decision(response.choices[0].message.content)
        except RateLimitError:
            if attempt == RATE_LIMIT_RETRY_ATTEMPTS:
                raise
            print(f"  [rate limit] {RATE_LIMIT_BACKOFF_SECONDS}초 대기 후 재시도 ({attempt}/{RATE_LIMIT_RETRY_ATTEMPTS})")
            time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
    raise RuntimeError("unreachable")


def run_merchant_judgment_loop(transaction: Transaction, tool: "MockSearchTool | RealSearchTool") -> Decision:
    print(f"[입력] {transaction.merchant} / {transaction.amount}원 (0차 정확 매칭 미스)")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"가맹점명: {transaction.merchant}\n"
                f"금액: {transaction.amount}원"
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
