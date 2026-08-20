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

_AMOUNT_RE = re.compile(r"([\d,]+)\s*원\s*(결제|출금)")
_MERCHANT_RE = re.compile(r"결제\s*\|\s*(.+?)\n?\s*잔액", re.S)
_MERCHANT_RE_NOTIFICATION_CENTER = re.compile(r"체크카드\s*\|\s*(.+?)\s*잔액", re.S)
_WITHDRAWAL_DEST_RE = re.compile(r"통장\s*→\s*(.+?)(?:\n|$)")


@dataclass
class ParsedCapture:
    merchant: str
    amount: int
    force_escalate: bool = False


def parse_captures(raw_text: str) -> list[ParsedCapture]:
    amount_matches = list(_AMOUNT_RE.finditer(raw_text))
    results: list[ParsedCapture] = []

    for i, amount_match in enumerate(amount_matches):
        block_start = amount_match.start()
        block_end = amount_matches[i + 1].start() if i + 1 < len(amount_matches) else len(raw_text)
        block = raw_text[block_start:block_end]
        amount = int(amount_match.group(1).replace(",", ""))
        kind = amount_match.group(2)

        if kind == "출금":
            dest_match = _WITHDRAWAL_DEST_RE.search(block)
            if not dest_match:
                continue
            destination = dest_match.group(1).strip()
            if not destination:
                continue
            results.append(ParsedCapture(merchant=destination, amount=amount, force_escalate=True))
            continue

        merchant_match = _MERCHANT_RE.search(block) or _MERCHANT_RE_NOTIFICATION_CENTER.search(block)
        if not merchant_match:
            continue

        merchant = merchant_match.group(1).replace("\n", "").strip()
        if not merchant:
            continue

        results.append(ParsedCapture(merchant=merchant, amount=amount))

    return results


def raw_text_hash(raw_text: str) -> str:
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
