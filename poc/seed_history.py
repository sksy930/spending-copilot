"""과거 실거래 내역을 신규 거래 그래프(0차 정확 매칭 → 가맹점 판단 루프)로 실제 분류해
transactions 테이블에 지정한 날짜로 백필한다. poc/batch_replay.py와 같은 파이프라인을
쓰지만, 그쪽은 통계만 내고 저장은 안 하는 반면 이 스크립트는 실제로 저장한다
(데모용 초기 데이터 세팅).

사용법:
    python poc/seed_history.py data/july_august_history.csv

CSV 형식 (헤더 포함, date/merchant/amount 세 컬럼):
    date,merchant,amount
    2026-07-01,배달의민족,33256
"""

import csv
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db  # noqa: E402
from app.merchant_judgment import RealSearchTool, Transaction, run_merchant_judgment_loop  # noqa: E402


def seed(csv_path: str) -> None:
    total = 0
    zero_tier_hits = 0

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date = row["date"].strip()
            merchant = row["merchant"].strip()
            amount = int(row["amount"].replace(",", "").strip())
            if not merchant:
                continue
            total += 1
            created_at = f"{date} 12:00:00+09:00"  # 시각 정보가 없어 정오(KST)로 고정

            category = db.lookup_category(merchant)
            if category is not None:
                zero_tier_hits += 1
                db.insert_transaction(
                    merchant=merchant,
                    amount=amount,
                    category=category,
                    decision="confirm",
                    confidence=1.0,
                    reason="0차 정확 매칭",
                    created_at=created_at,
                )
                print(f"[{date}][0차 히트] {merchant} / {amount}원 -> {category}")
                continue

            tool = RealSearchTool()
            try:
                result = run_merchant_judgment_loop(Transaction(merchant=merchant, amount=amount), tool)
            except Exception as exc:  # noqa: BLE001 — 한 건 실패로 시딩 전체를 죽이면 안 됨
                db.insert_review(
                    reason="llm_unavailable",
                    raw_text=f"{merchant} / {amount}원 ({date}) — 가맹점 판단 실패: {exc}",
                )
                print(f"[{date}][llm_unavailable] {merchant} / {amount}원 -> {exc}")
                continue

            db.insert_transaction(
                merchant=merchant,
                amount=amount,
                category=result.category,
                decision=result.decision,
                confidence=result.confidence,
                reason=result.reason,
                created_at=created_at,
            )
            if result.decision == "confirm" and result.category:
                db.upsert_merchant_category(merchant, result.category)
            print(f"[{date}][{result.decision}] {merchant} / {amount}원 -> {result.category}")

    print("\n=== 완료 ===")
    print(f"총 거래: {total}, 0차 정확 매칭 히트: {zero_tier_hits} ({zero_tier_hits / total:.1%})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("사용법: python poc/seed_history.py <csv경로>")
    seed(sys.argv[1])
