"""공용 LLM 호출 헬퍼 — Groq를 기본으로 쓰고, 실패하면 Gemini로 한 번 더 시도한다.

Groq가 모델을 통보 없이 단종시켜(`llama-3.3-70b-versatile`) 가맹점 판단/QA/브리핑이
한꺼번에 죽었던 사고가 이 프로젝트에서 실제로 있었다 — 그 사고에 대한 안전장치다.
Rate limit(429)은 기존처럼 같은 provider로 몇 번 재시도하고, 그래도 안 되거나 다른
종류의 장애(모델 단종, 네트워크 등)면 즉시 Gemini로 넘어간다. Gemini 쪽도 일시적으로
503(과부하)이 날 수 있다는 걸 실제로 확인해서, 짧게 한 번 더 재시도하게 해뒀다.
"""

import os
import time

import litellm
from litellm.exceptions import RateLimitError

FALLBACK_MODEL = os.environ.get("GEMINI_MODEL", "gemini/gemini-flash-latest")
PRIMARY_RETRY_ATTEMPTS = 3
PRIMARY_BACKOFF_SECONDS = 20
FALLBACK_RETRY_ATTEMPTS = 2
FALLBACK_BACKOFF_SECONDS = 5


def complete_with_fallback(primary_model: str, fallback_model: str = None, **kwargs):
    """primary_model로 시도하고, 실패하면 fallback_model(기본 Gemini)로 넘어간다.

    호출부 대부분은 Groq가 기본이라 fallback_model을 안 주면 FALLBACK_MODEL(Gemini)로
    떨어진다. 가맹점 판단처럼 반대로 Gemini가 기본이고 Groq가 대체여야 하는 곳은
    fallback_model=GROQ_MODEL을 넘긴다 (app/merchant_judgment.py).

    둘 다 실패하면 가장 마지막에 발생한 예외를 그대로 던진다 — 호출하는 쪽의 기존
    except 처리(RateLimitError를 review_queue로 보내는 등)가 그대로 먹힌다.
    """
    fallback_model = fallback_model or FALLBACK_MODEL
    last_exc: Exception = RuntimeError("no attempt made")
    for attempt in range(1, PRIMARY_RETRY_ATTEMPTS + 1):
        try:
            return litellm.completion(model=primary_model, **kwargs)
        except RateLimitError as exc:
            last_exc = exc
            if attempt < PRIMARY_RETRY_ATTEMPTS:
                print(f"  [{primary_model} rate limit] {PRIMARY_BACKOFF_SECONDS}초 대기 후 재시도 ({attempt}/{PRIMARY_RETRY_ATTEMPTS})")
                time.sleep(PRIMARY_BACKOFF_SECONDS)
        except Exception as exc:  # noqa: BLE001 — rate limit이 아닌 장애는 바로 폴백으로
            last_exc = exc
            break

    print(f"  [{primary_model} 실패: {last_exc}] {fallback_model}로 대체 시도")
    for attempt in range(1, FALLBACK_RETRY_ATTEMPTS + 1):
        try:
            return litellm.completion(model=fallback_model, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < FALLBACK_RETRY_ATTEMPTS:
                print(f"  [{fallback_model} 실패, {FALLBACK_BACKOFF_SECONDS}초 후 재시도]")
                time.sleep(FALLBACK_BACKOFF_SECONDS)

    raise last_exc
