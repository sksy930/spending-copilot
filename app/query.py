"""자연어 QA — 집계형 질문 경로, 에이전트 루프 (docs/project-overview.md 3.4.3).

가맹점 판단 루프(app/merchant_judgment.py)와 같은 패턴: 매 스텝 LLM이 "지금 결과로
답할 수 있는가, 쿼리를 더 돌려야 하는가"를 스스로 결정한다. "지난달보다 늘었어?"처럼
쿼리 하나로 안 풀리는 질문에서 다음 조회를 스스로 판단해 이어가는 게 핵심이다.
가맹점 불명확 질문(Chroma RAG) 경로는 이번 스코프에서 제외한다 (3.3 참고,
RAG는 미매칭 거래량이 늘어나면 재검토).
"""

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import litellm
from litellm.exceptions import RateLimitError

from app.db import DB_PATH

GROQ_MODEL = os.environ.get("GROQ_MODEL", "groq/llama-3.3-70b-versatile")
MAX_QUERY_STEPS = 3
RATE_LIMIT_RETRY_ATTEMPTS = 3
RATE_LIMIT_BACKOFF_SECONDS = 20

QUERY_AGENT_SYSTEM_PROMPT = """너는 개인 소비 SQLite 데이터베이스에 대해 질문에 답하는 에이전트다.

테이블: transactions
- id INTEGER
- merchant TEXT (가맹점명)
- amount INTEGER (결제 금액, 원)
- category TEXT (카테고리, 아직 확정 안 된 경우 NULL)
- decision TEXT ('confirm' 확정 | 'escalate' 리뷰 필요)
- confidence REAL
- reason TEXT
- created_at TEXT (SQLite datetime('now') 형식 UTC, 'YYYY-MM-DD HH:MM:SS')

오늘 날짜(UTC): {today}

매 스텝마다 아래 JSON 스키마로만 응답한다 (다른 설명 문장 없이 JSON 객체 하나만 출력):
{{
  "action": "query" | "answer",
  "sql": "string (action=query일 때만, SELECT 문 하나)",
  "answer": "string (action=answer일 때만, 최종 답변 문장)",
  "reason": "판단 근거"
}}

규칙:
- 지금까지 실행한 쿼리 결과로 질문에 충분히 답할 수 있으면 action=answer.
- 비교/추세 질문(예: "지난달보다 늘었어?")처럼 한 번의 쿼리로 부족하면 action=query로
  추가 쿼리를 요청한다 — 필요한 쿼리를 한 스텝에 하나씩, 스스로 판단해서 순서대로 요청한다.
- SQL은 transactions 테이블만 쓰는 SELECT 문 하나만 작성한다. INSERT/UPDATE/DELETE/DROP/PRAGMA/ATTACH 등은 절대 금지.
- "이번 주"는 created_at >= datetime('now', '-7 days')로, "지난주"는 -14일~-7일 범위로,
  "이번 달"은 strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')로, "지난달"은 그 전달로 계산한다.
- category가 NULL인 행(아직 미확정)은 특별한 언급이 없으면 집계에서 제외한다 (decision='confirm' AND category IS NOT NULL).
- action=answer일 때 답변은 한국어로 한두 문장, 간결하게. 금액은 천 단위 콤마를 넣어 "12,345원"처럼 표기.
- 조회해도 조건에 맞는 데이터가 없으면 억지로 추측하지 말고 그렇다고 답한다.
"""

_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|pragma|create|replace|grant|vacuum)\b",
    re.IGNORECASE,
)


class QueryError(Exception):
    pass


@dataclass
class QueryStep:
    action: str
    sql: Optional[str] = None
    answer: Optional[str] = None
    reason: str = ""


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            first_line, rest = text.split("\n", 1)
            text = rest if first_line.strip().lower() in ("json", "") else text
    return text.strip()


def _parse_step(raw_text: str) -> QueryStep:
    data = json.loads(_strip_code_fence(raw_text))
    return QueryStep(
        action=data["action"],
        sql=data.get("sql"),
        answer=data.get("answer"),
        reason=data.get("reason", ""),
    )


def _call_query_agent(messages: list[dict]) -> QueryStep:
    """Groq 무료 티어의 요청 제한(429)에 걸리면 잠깐 대기 후 재시도한다."""
    for attempt in range(1, RATE_LIMIT_RETRY_ATTEMPTS + 1):
        try:
            response = litellm.completion(model=GROQ_MODEL, temperature=0, messages=messages)
            return _parse_step(response.choices[0].message.content)
        except RateLimitError:
            if attempt == RATE_LIMIT_RETRY_ATTEMPTS:
                raise QueryError("지금 LLM 무료 할당량이 다 찼어요. 잠시 후 다시 시도해주세요.")
            print(f"  [rate limit] {RATE_LIMIT_BACKOFF_SECONDS}초 대기 후 재시도 ({attempt}/{RATE_LIMIT_RETRY_ATTEMPTS})")
            time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise QueryError("질문을 처리하지 못했어요.") from exc
    raise RuntimeError("unreachable")


def _is_safe_select(sql: str) -> bool:
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        return False
    if not stripped.lower().startswith("select"):
        return False
    if _FORBIDDEN_RE.search(stripped):
        return False
    return True


def _run_sql(sql: str) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql).fetchmany(200)
        return [dict(row) for row in rows]
    finally:
        conn.close()


def answer_question(question: str) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    messages = [
        {"role": "system", "content": QUERY_AGENT_SYSTEM_PROMPT.format(today=today)},
        {"role": "user", "content": question},
    ]
    executed_sql: list[str] = []

    for step in range(1, MAX_QUERY_STEPS + 1):
        decision = _call_query_agent(messages)
        messages.append({"role": "assistant", "content": json.dumps(decision.__dict__, ensure_ascii=False)})

        if decision.action == "answer":
            return {"answer": decision.answer, "sql": executed_sql, "steps": step}

        sql = (decision.sql or "").strip()
        if not _is_safe_select(sql):
            raise QueryError(f"안전하지 않은 쿼리라 실행할 수 없어요: {sql}")
        try:
            rows = _run_sql(sql)
        except sqlite3.Error as exc:
            raise QueryError(f"쿼리 실행에 실패했어요: {exc}") from exc
        executed_sql.append(sql)
        messages.append({"role": "user", "content": f"쿼리 결과: {json.dumps(rows, ensure_ascii=False)}"})

    raise QueryError(f"질문이 복잡해서 {MAX_QUERY_STEPS}번 조회해도 답을 못 정했어요. 더 구체적으로 물어봐 주세요.")
