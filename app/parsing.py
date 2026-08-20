"""캡처 원문 텍스트 파싱 (docs/project-overview.md 3.2 정규화 로직).

토스뱅크 체크카드 결제 알림을 대상으로 하며, 실기기 캡처 경로에 따라 OCR 텍스트
형식이 둘로 갈린다.

배너(화면 상단에 뜬 순간 캡처) 형식:

    토스뱅크 체크카드
    3,200원 결제 | 이디야커피
    (대구수성알파시티점)
    잔액 705,872원

알림 센터(스와이프해서 캡처) 형식 — "결제" 뒤에 "|"가 바로 안 오고, 가맹점명이
"체크카드 |"와 "잔액" 사이에 온다:

    3,800원 결제
    토스뱅크 체크카드 | KFC 수원성균관대점 잔액 159,253원 (토스뱅크 알림)

가맹점명이 화면 폭 때문에 줄바꿈되어도(배너 예시의 "이디야커피\n(대구수성알파시티점)")
그 줄바꿈만 제거해 하나의 가맹점명으로 합친다.

알림 센터 캡처는 결제 블록마다 시각("오후 12:06")이 붙어있고, 화면 맨 위 잠금화면
헤더에 날짜("20일")가 있다 — 한 스크린샷에 여러 결제가 쌓여있으면 그 시각이 서로
다르므로(예: 12:06, 12:04, 11:49), 캡처 시각(webhook payload의 captured_at, 즉
Back Tap을 누른 순간) 하나로 전부 퉁치면 안 된다. 헤더의 날짜 + 블록별 시각을
조합해 결제 건마다 실제 결제 시각을 복원한다. 시각을 못 찾으면(배너 형식은 애초에
시각이 안 찍힘) captured_at으로 폴백한다.

한 스크린샷에 결제 알림이 여러 건 쌓여있는 경우(예: 알림 센터를 며칠치 밀렸다가
한 번에 캡처)도 처리한다 — 금액 패턴("N원 결제")이 나온 위치마다 결제 한 건의
시작으로 보고, 다음 금액이 나오기 전까지를 그 건의 블록으로 잘라 그 안에서만
가맹점명을 찾는다. 이렇게 블록을 나누지 않으면 정규식이 텍스트 전체에서 처음
매칭되는 것 하나만 찾아서, 두 번째 이후 결제가 조용히 유실된다.

체크카드 결제 말고 계좌 출금(네이버페이/카카오페이 충전 등)도 실제 소비이지만
형식이 완전히 다르고 가맹점명 자체가 없다:

    10,000원 출금
    내 토스뱅크 통장 → 네이버페이충전

가맹점 정보가 없어 LLM이 판단할 방법이 없으므로, 이 형식은 가맹점 판단 루프를
아예 안 태우고 곧장 escalate시켜 사람이 카테고리를 정하게 한다
(app/new_transaction_graph.py의 force_escalate 참고).
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_AMOUNT_RE = re.compile(r"([\d,]+)\s*원\s*(결제|출금)")
_MERCHANT_RE = re.compile(r"결제\s*\|\s*(.+?)\n?\s*잔액", re.S)
_MERCHANT_RE_NOTIFICATION_CENTER = re.compile(r"체크카드\s*\|\s*(.+?)\s*잔액", re.S)
_WITHDRAWAL_DEST_RE = re.compile(r"통장\s*(?:→|>+)\s*(.+?)(?:\n|$)")
_TIME_RE = re.compile(r"(어제\s*)?(오전|오후)\s*(\d{1,2}):(\d{2})")
_DAY_OF_MONTH_RE = re.compile(r"(\d{1,2})일")


@dataclass
class ParsedCapture:
    merchant: str
    amount: int
    occurred_at: datetime
    force_escalate: bool = False


def _resolve_occurred_at(raw_text: str, block: str, header_end: int, captured_at: datetime) -> datetime:
    time_match = _TIME_RE.search(block)
    if not time_match:
        return captured_at

    yesterday_marker, ampm, hour_str, minute_str = time_match.groups()
    hour = int(hour_str) % 12
    if ampm == "오후":
        hour += 12
    minute = int(minute_str)

    day_match = _DAY_OF_MONTH_RE.search(raw_text[:header_end])
    day = int(day_match.group(1)) if day_match else captured_at.day

    try:
        resolved = captured_at.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        # 그 달에 없는 날짜(예: 헤더가 다음 달 1일인데 day=31로 잘못 읽힘) — 캡처 시각으로 폴백
        return captured_at

    if yesterday_marker:
        # 알림 센터를 자정 넘겨서 캡처하면 헤더 날짜는 오늘이지만, 그 전날 쌓인 건은
        # 시각 앞에 "어제"가 붙어서 나온다 — 헤더 날짜 그대로 쓰면 하루 밀려서 기록된다.
        resolved -= timedelta(days=1)

    return resolved

def parse_captures(raw_text: str, captured_at: datetime) -> list[ParsedCapture]:
    amount_matches = list(_AMOUNT_RE.finditer(raw_text))
    results: list[ParsedCapture] = []
    header_end = amount_matches[0].start() if amount_matches else 0

    for i, amount_match in enumerate(amount_matches):
        block_start = amount_match.start()
        block_end = amount_matches[i + 1].start() if i + 1 < len(amount_matches) else len(raw_text)
        block = raw_text[block_start:block_end]
        amount = int(amount_match.group(1).replace(",", ""))
        kind = amount_match.group(2)
        occurred_at = _resolve_occurred_at(raw_text, block, header_end, captured_at)

        if kind == "출금":
            dest_match = _WITHDRAWAL_DEST_RE.search(block)
            if not dest_match:
                continue
            destination = dest_match.group(1).strip()
            if not destination:
                continue
            results.append(
                ParsedCapture(merchant=destination, amount=amount, occurred_at=occurred_at, force_escalate=True)
            )
            continue

        merchant_match = _MERCHANT_RE.search(block) or _MERCHANT_RE_NOTIFICATION_CENTER.search(block)
        if not merchant_match:
            continue

        merchant = merchant_match.group(1).replace("\n", "").strip()
        if not merchant:
            continue

        results.append(ParsedCapture(merchant=merchant, amount=amount, occurred_at=occurred_at))

    return results


def count_payment_blocks(raw_text: str) -> int:
    """"N원 결제/출금" 패턴이 몇 개 있는지 — parse_captures가 그중 일부만 못 잡았을 때
    (원문은 남기고 나머지는 정상 처리하는) 부분 실패를 감지하는 용도 (app/main.py)."""
    return len(list(_AMOUNT_RE.finditer(raw_text)))


def raw_text_hash(raw_text: str) -> str:
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
