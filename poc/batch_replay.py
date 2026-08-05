"""과거 거래 내역을 신규 거래 그래프(0차 정확 매칭 → 가맹점 판단 루프)로 일괄 재생하고
통계를 낸다. docs 3.4.1 설계 근거("소수 가맹점 반복 → 0차 히트율이 시간이 지날수록
올라간다")를 실제 데이터로 검증하기 위한 스크립트.

사용법:
    python poc/batch_replay.py data/real_transactions.csv

CSV 형식 (헤더 포함, merchant/amount 두 컬럼):
    merchant,amount
    이디야커피(대구수성알파시티점),3200

주의: 0차 정확 매칭 확정 결과는 실제 파이프라인과 동일하게 data/spending.db의
merchant_category_map에 반영된다 — CSV의 앞쪽 행이 뒤쪽 행의 0차 히트 여부에
영향을 준다 (운영 기간이 늘수록 히트율이 오른다는 가설을 그대로 재현하는 것).
transactions 테이블에는 쓰지 않으므로 조회 화면(UI)은 오염되지 않는다.
"""

import csv
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db  # noqa: E402
from app.merchant_judgment import RealSearchTool, Transaction, run_merchant_judgment_loop  # noqa: E402


def replay(csv_path: str) -> None:
    total = 0
    zero_tier_hits = 0
    decision_counts = {"confirm (0차)": 0, "confirm (에이전트 루프)": 0, "escalate": 0}
    llm_calls_on_miss: list[int] = []

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            merchant = row["merchant"].strip()
            amount = int(row["amount"].replace(",", "").strip())
            if not merchant:
                continue
            total += 1

            category = db.lookup_category(merchant)
            if category is not None:
                zero_tier_hits += 1
                decision_counts["confirm (0차)"] += 1
                print(f"[0차 히트] {merchant} / {amount}원 -> {category}")
                continue

            tool = RealSearchTool()
            result = run_merchant_judgment_loop(Transaction(merchant=merchant, amount=amount), tool)
            llm_calls_on_miss.append(len(tool.call_log) + 1)

            if result.decision == "confirm":
                decision_counts["confirm (에이전트 루프)"] += 1
                if result.category:
                    db.upsert_merchant_category(merchant, result.category)
            else:
                decision_counts["escalate"] += 1

            print(
                f"[{result.decision}] {merchant} / {amount}원 -> "
                f"{result.category} (confidence={result.confidence}, reason={result.reason})"
            )

    print("\n=== 요약 ===")
    print(f"총 거래: {total}")
    if total == 0:
        return
    print(f"0차 정확 매칭 히트: {zero_tier_hits} ({zero_tier_hits / total:.1%})")
    for label, count in decision_counts.items():
        print(f"{label}: {count} ({count / total:.1%})")
    misses = total - zero_tier_hits
    if misses:
        avg_calls = sum(llm_calls_on_miss) / misses
        print(f"0차 미스 건당 평균 LLM 호출 횟수: {avg_calls:.2f}회")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("사용법: python poc/batch_replay.py <csv경로>")
    replay(sys.argv[1])
