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
그 줄바꿈만 제거해 하나의 가맹점명으로 합친다. 날짜는 원문에 없으므로 파싱 대상이
아니다 — 웹훅 payload의 captured_at을 그대로 쓴다 (3.2 참고).
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

_AMOUNT_RE = re.compile(r"([\d,]+)\s*원\s*결제")
_MERCHANT_RE = re.compile(r"결제\s*\|\s*(.+?)\n?\s*잔액", re.S)
_MERCHANT_RE_NOTIFICATION_CENTER = re.compile(r"체크카드\s*\|\s*(.+?)\s*잔액", re.S)


@dataclass
class ParsedCapture:
    merchant: str
    amount: int


def parse_capture(raw_text: str) -> Optional[ParsedCapture]:
    amount_match = _AMOUNT_RE.search(raw_text)
    merchant_match = _MERCHANT_RE.search(raw_text) or _MERCHANT_RE_NOTIFICATION_CENTER.search(raw_text)
    if not amount_match or not merchant_match:
        return None

    amount = int(amount_match.group(1).replace(",", ""))
    merchant = merchant_match.group(1).replace("\n", "").strip()
    if not merchant:
        return None

    return ParsedCapture(merchant=merchant, amount=amount)


def raw_text_hash(raw_text: str) -> str:
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
