"""자연어 QA — 집계형 질문 경로 (docs/project-overview.md 3.4.3).

text-to-SQL 변환 → transactions 테이블 SELECT 조회 → LLM이 답변 문장 생성.
가맹점 불명확 질문(Chroma RAG) 경로는 이번 스코프에서 제외한다 (3.3 참고,
RAG는 미매칭 거래량이 늘어나면 재검토).
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timezone

import litellm
from litellm.exceptions import RateLimitError

from app.db import DB_PATH

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini/gemini-flash-latest")

SQL_SYSTEM_PROMPT_TEMPLATE = """너는 개인 소비 SQLite 데이터베이스에 대해 읽기 전용 SQL을 생성하는 도우미다.

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

규칙:
- transactions 테이블만 사용하는 SELECT 문 하나만 작성한다. INSERT/UPDATE/DELETE/DROP/PRAGMA/ATTACH 등은 절대 금지.
- "이번 주"는 created_at >= datetime('now', '-7 days')로, "오늘"은 date(created_at) = date('now')로,
  "이번 달"은 strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')로 계산한다.
- 합계/평균/개수 질문이면 SUM/AVG/COUNT를 쓴다. category가 NULL인 행(아직 미확정)은
  특별한 언급이 없으면 집계에서 제외한다 (decision='confirm' AND category IS NOT NULL).
- 아래 JSON 스키마 하나로만 응답한다 (다른 설명 문장 없이):
{{"sql": "SELECT ..."}}
"""

ANSWER_SYSTEM_PROMPT = """너는 사용자의 개인 소비 데이터에 대한 질문에 답하는 비서다.
주어진 SQL 실행 결과만 근거로 삼아 한국어로 한두 문장, 간결하게 답한다.
금액은 천 단위 콤마를 넣어 "12,345원"처럼 표기한다.
결과가 비어있으면 억지로 추측하지 말고 "해당 조건에 맞는 거래를 못 찾았어요"처럼 답한다.
"""

_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|pragma|create|replace|grant|vacuum)\b",
    re.IGNORECASE,
)


class QueryError(Exception):
    pass


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            first_line, rest = text.split("\n", 1)
            text = rest if first_line.strip().lower() in ("json", "") else text
    return text.strip()


def _generate_sql(question: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    system_prompt = SQL_SYSTEM_PROMPT_TEMPLATE.format(today=today)
    try:
        response = litellm.completion(
            model=GEMINI_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
        )
    except RateLimitError as exc:
        raise QueryError("지금 Gemini 무료 할당량이 다 찼어요. 잠시 후(또는 내일) 다시 시도해주세요.") from exc
    raw = _strip_code_fence(response.choices[0].message.content)
    try:
        data = json.loads(raw)
        return data["sql"].strip()
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise QueryError("질문을 SQL로 변환하지 못했어요.") from exc


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


def _generate_answer(question: str, sql: str, rows: list[dict]) -> str:
    try:
        response = litellm.completion(
            model=GEMINI_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"질문: {question}\nSQL: {sql}\n결과: {json.dumps(rows, ensure_ascii=False)}",
                },
            ],
        )
    except RateLimitError as exc:
        raise QueryError("지금 Gemini 무료 할당량이 다 찼어요. 잠시 후(또는 내일) 다시 시도해주세요.") from exc
    return response.choices[0].message.content.strip()


def answer_question(question: str) -> dict:
    sql = _generate_sql(question)
    if not _is_safe_select(sql):
        raise QueryError(f"안전하지 않은 쿼리라 실행할 수 없어요: {sql}")
    try:
        rows = _run_sql(sql)
    except sqlite3.Error as exc:
        raise QueryError(f"쿼리 실행에 실패했어요: {exc}") from exc
    answer = _generate_answer(question, sql, rows)
    return {"answer": answer, "sql": sql, "rows": rows}
