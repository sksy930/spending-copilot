"""escalate 건 사람 개입 알림 — ntfy.sh 푸시 (docs/project-overview.md 3.6).

가맹점 판단 루프가 escalate로 끝난 거래를 아이폰 푸시 알림으로 알린다. 알림의
"확인하기" 액션을 탭하면 app/main.py의 GET /review/{id} 페이지가 열려 사람이
직접 카테고리를 고를 수 있다.

NTFY_TOPIC 또는 PUBLIC_BASE_URL이 설정 안 되어 있으면 조용히 건너뛴다 — 알림은
부가 기능이라 거래 저장 자체를 막으면 안 된다 (3.4.1과 같은 원칙).
"""

import json
import os
import urllib.request

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL")


def notify_review_needed(transaction_id: int, merchant: str, amount: int) -> None:
    if not NTFY_TOPIC or not PUBLIC_BASE_URL:
        return

    review_url = f"{PUBLIC_BASE_URL}/review/{transaction_id}"
    payload = {
        "topic": NTFY_TOPIC,
        "title": "카테고리 확인 필요",
        "message": f"{merchant} / {amount:,}원",
        "actions": [{"action": "view", "label": "확인하기", "url": review_url, "clear": True}],
    }
    req = urllib.request.Request(
        NTFY_SERVER,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5)
