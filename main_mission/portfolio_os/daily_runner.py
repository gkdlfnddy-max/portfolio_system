"""Daily Review 일괄 실행기 (수동→루틴) — 전 계좌 대상 generate_review 호출.

본질 (불변):
  - **주문 자동 실행 0.** generate_review 는 예약성 후보(plan)까지만 만든다 — 실제 주문은
    사람 승인 + PIN + live lock 이후 단계. 이 runner 는 review 행 생성만 트리거한다.
  - **멱등**: 계좌×일 1행(daily_portfolio_reviews UNIQUE) — 같은 날 여러 번 돌려도 1행 유지.
  - **관망 정상**: 스냅샷/선택안 없으면 watch 로 정직 보고(실패 아님).

cron 실제 설치는 사용자 몫(docs/portfolio/daily_review_ops.md 참고).
  python -m main_mission.portfolio_os.daily_runner
  python -m main_mission.portfolio_os.daily_runner --account 1   # 단일 계좌만
"""
from __future__ import annotations

import argparse
import json
import sys

from .store import db as store_db
from . import daily_review as dr


def list_accounts() -> list[int]:
    """accounts 테이블의 모든 account_index (없으면 빈 목록)."""
    conn = store_db.connect()
    try:
        rows = conn.execute("SELECT account_index FROM accounts ORDER BY account_index").fetchall()
        return [int(r["account_index"]) for r in rows]
    finally:
        conn.close()


def run_all(account_indexes: list[int] | None = None, review_date: str | None = None) -> dict:
    """모든(또는 지정) 계좌에 대해 generate_review 호출 — 멱등(계좌×일 1행).

    주문 자동 실행 없음: generate_review 가 만드는 것은 예약성 후보(plan)까지.
    반환: 계좌별 결과 요약 + 집계. 한 계좌 실패가 다른 계좌를 막지 않는다(격리)."""
    idxs = account_indexes if account_indexes is not None else list_accounts()
    results: list[dict] = []
    for idx in idxs:
        try:
            r = dr.generate_review(idx, review_date)
            results.append({
                "account_index": idx,
                "ok": bool(r.get("ok")),
                "review_id": r.get("review_id"),
                "action_decision": r.get("action_decision"),
                "no_trade_reason": r.get("no_trade_reason"),
                "has_orders": bool(r.get("has_orders")),
                "scheduled_order_plan_id": r.get("scheduled_order_plan_id"),
                "carry_over_count": ((r.get("carry_over") or {}).get("carry_count")),
            })
        except Exception as e:  # noqa: BLE001 — 계좌 격리(한 계좌 오류로 전체 중단 금지)
            results.append({"account_index": idx, "ok": False, "error": f"{e}"})
    return {
        "ok": True,
        "accounts": len(idxs),
        "generated": sum(1 for r in results if r.get("ok")),
        "with_order_candidates": sum(1 for r in results if r.get("has_orders")),
        "orders_executed": 0,  # 불변: 자동 주문 실행 0 (예약성 후보까지만)
        "results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="전 계좌 Daily Review 일괄 생성(멱등, 주문 자동실행 0)")
    ap.add_argument("--account", type=int, default=None, help="단일 계좌만(생략 시 전 계좌)")
    ap.add_argument("--date", type=str, default=None, help="review_date(YYYY-MM-DD, 생략 시 오늘)")
    args = ap.parse_args()
    try:
        idxs = [args.account] if args.account is not None else None
        out = run_all(idxs, args.date)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
