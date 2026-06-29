"""분할 매수 집행 플래너/실행기 — '웹에서 포트폴리오 구성' 요청의 실집행 엔진.

현금에서 확정안(selected_allocation) 목표까지 **한 번에 X, 분할(여러 회차) 지정가 예측진입**으로
포트폴리오를 완성한다(계좌 보유 0 → 목표 비중까지 단계 매수).

흐름: 확정안(truth) + 종목선택(picks) → weight_allocator(목표 비중·한도 검증)
      → 분할 회차 지정가 plan(build_split_plan) → **CEO 승인** → 회차 집행(execute_round)

불변 원칙(코드로 강제):
  - **지정가만**(시장가 매수 영구 금지) — submit_order 가 시장가 매수 차단(§16).
  - **사람 승인 기본값** — execute_round 는 approved=True 가 아니면 집행 거부(무승인 자동매매 금지).
  - 회차/세션 한도: 1주문 ≤ one_order_cap, 회차 수 = rebalance rounds.
  - 자동 주문/자동 적용 0 — build_split_plan 은 제안만(주문 X), 집행은 명시적 승인 1건씩.
  - 가짜 숫자 금지: 가격 없으면 해당 종목 스킵(정직).
"""
from __future__ import annotations

import math
from decimal import Decimal

from . import weight_allocator as wa
from .broker import order_service as svc
from .broker.port import Account, Instrument, OrderRequest

ROUNDS_DEFAULT = 3
KNEE_PCT_DEFAULT = 2.0       # 무릎: 현재가 -2% 지정가(예측진입; 실제는 일/주 흐름 판단)


def _sell_rules(rules: dict | None) -> dict:
    """매도 규칙 envelope — 규칙(목표/손절/보수전환)은 사전승인 대상, 재량 시그널은 제안→승인."""
    r = rules or {}
    return {
        "target_pct": r.get("target_pct"),          # 목표가 도달 시 익절(사전승인 규칙)
        "stop_pct": r.get("stop_pct"),              # 손절(사전승인 규칙)
        "conservative_switch": r.get("conservative_switch", True),  # 보수전환 레벨(사전승인)
        "discretionary": "propose_then_approve",    # 그 외 시그널 매도는 제안→승인(자동 X)
    }


def build_split_plan(account_index: int, picks: dict, *, prices: dict, cash_krw: float,
                     rounds: int = ROUNDS_DEFAULT, period_days: int = 14,
                     knee_pct: float = KNEE_PCT_DEFAULT, weighting: str = "equal",
                     markets: dict | None = None, sell_rules: dict | None = None,
                     plan_token: str | None = None, fx_rates: dict | None = None,
                     equity_option: str = "none") -> dict:
    """확정안 + picks → **기간·횟수만으로** 분할 저점 지정가 전략 plan(제안만, 주문 X).

    CEO 모델: 기간(period_days)+분할횟수(rounds)만 정하면 나머지(저점 사다리 가격/수량/스케줄/
    매도규칙)는 시스템이 수립. prices:{ticker:현재가}. markets:{ticker:(market,currency)}.
    fx_rates:{통화:원화환율} — 미국주식(USD)은 KRW 예산을 통화로 환산해 수량 계산(없으면 1.0=무환산).
    반환: {ok, rounds, period_days, steps[](schedule_day 포함), sell_rules, total_target_krw,
           blocked, requires_user_approval=True, auto_order_created=False}
    """
    alloc = wa.allocate(account_index, picks, weighting=weighting, equity_option=equity_option)
    if not alloc.get("ok"):
        return {"ok": False, "error": alloc.get("error", "allocate 실패"),
                "requires_user_approval": True, "auto_order_created": False}
    if alloc.get("blocked"):
        return {"ok": False, "error": "리스크 게이트 차단 — 한도 초과(분산 필요)",
                "over_limit_warnings": alloc.get("over_limit_warnings", []),
                "blocked": True, "requires_user_approval": True, "auto_order_created": False}

    rounds = max(1, int(rounds))
    period_days = max(1, int(period_days))
    # plan_token: 같은 plan 재승인은 idempotent(중복 미제출), 다른 사이클은 새 주문ID(stale 차단 회피).
    tok = f"-{plan_token}" if plan_token else ""
    one_order_cap = float((alloc.get("limits") or {}).get("one_order_cap_pct", 5.0))
    markets = markets or {}
    steps: list[dict] = []
    total_target = 0.0
    skipped: list[dict] = []

    for h in alloc["holdings"]:
        tk = h.get("ticker")
        if h.get("kind") == "cash" or not tk:
            continue
        price = prices.get(tk)
        if not price or price <= 0:
            skipped.append({"ticker": tk, "reason": "현재가 미연동 — 분할 계획 제외(가짜 숫자 금지)"})
            continue
        weight = float(h["weight_pct"])
        total_krw = cash_krw * weight / 100.0
        total_target += total_krw
        _m = markets.get(tk, ("KRX", "KRW"))
        mkt, ccy = _m[0], _m[1]
        exch = _m[2] if len(_m) > 2 else ""   # KIS 해외 거래소코드(NASD/NYSE/AMEX) — 미국 주문용
        is_foreign = ccy != "KRW"
        # 통화 환산: KRW=1.0(불변). fx_rates 제공됐는데 해당 통화 없으면 스킵(가짜 환산 금지).
        if is_foreign and fx_rates is not None and ccy not in fx_rates:
            skipped.append({"ticker": tk,
                            "reason": f"{ccy} 환율 미연동 — KRW 환산 불가(집행 보류, 환율 적재 필요)"})
            continue
        fx = float((fx_rates or {}).get(ccy, 1.0)) or 1.0
        # ── 버림(floor) 기준 + 1주 보장 + 드롭 ──
        #   종목 전체 예산(weight×cash)으로 살 수 있는 **정수 주식**만 매수(소수주 없음).
        #   시드(예산)가 1주보다 작으면 못 사므로 드롭(정직). 1주문 한도(one_order_cap)도 준수.
        one_order_cap_ccy = (cash_krw * one_order_cap / 100.0) / fx      # 1주문 한도(종목 통화)
        name_budget_ccy = total_krw / fx                                 # 종목 전체 예산(통화)
        ref_limit = (round(price * (1 - knee_pct / 100.0), 2) if is_foreign
                     else round(price * (1 - knee_pct / 100.0)))         # 1회차(최고가) 기준 호가
        unit = ccy
        if ref_limit <= 0:
            continue
        if ref_limit > one_order_cap_ccy:
            skipped.append({"ticker": tk,
                            "reason": f"1주({ref_limit:,} {unit})가 1주문 한도({one_order_cap}%="
                                      f"{one_order_cap_ccy:,.0f} {unit}) 초과 — 분산/자본 조정 필요"})
            continue
        shares_total = math.floor(name_budget_ccy / ref_limit)           # 버림 — 살 수 있는 정수 주식
        if shares_total < 1:
            budget_str = f"{name_budget_ccy:,.2f}{unit}" if is_foreign else f"{name_budget_ccy:,.0f}원"
            skipped.append({"ticker": tk,
                            "reason": f"예산 {budget_str} < 1주({ref_limit:,} {unit}) — 시드 부족(버림→0, 드롭)"})
            continue
        eff_rounds = min(rounds, shares_total)         # 살 수 있는 주식 수만큼만 회차 분할(회차당 ≥1주 보장)
        base_q, rem = divmod(shares_total, eff_rounds)  # 균등 분배 + 나머지는 앞 회차에(버림)
        for r in range(1, eff_rounds + 1):
            # 저점 사다리: 회차가 깊어질수록 더 낮은 지정가. 해외는 소수 2자리 호가.
            raw_limit = price * (1 - knee_pct * r / 100.0)
            limit = round(raw_limit, 2) if is_foreign else round(raw_limit)
            q = base_q + (1 if r <= rem else 0)
            max_q = math.floor(one_order_cap_ccy / limit) if limit > 0 else 0   # 회차 1주문 한도 재확인
            q = min(q, max_q)
            if q < 1:
                continue
            cycle_krw = q * limit * fx
            steps.append({
                "ticker": tk, "market": mkt, "currency": ccy, "side": "buy",
                "order_type": "limit", "limit_price": limit, "qty": q,
                "round_no": r, "total_rounds": eff_rounds,
                "schedule_day": round((r - 1) * period_days / eff_rounds),  # 기간 내 회차 분산(일)
                "drop_pct": round(knee_pct * r, 1),          # 현재가 대비 하락률(저점 깊이)
                "weight_pct": round(weight, 2),
                "cycle_pct": round(cycle_krw / cash_krw * 100.0, 2),
                "cycle_krw": round(cycle_krw), "fx_rate": (round(fx, 2) if is_foreign else None),
                "bucket": h.get("bucket"), "shares_total": shares_total, "exchange": exch,
                "on_unfilled": "no_chase",                    # 미체결이면 매수 안 함(추격·시장가 없음)
                "client_order_id": f"exec-{account_index}-{tk}-r{r}{tok}",
            })

    return {
        "ok": True, "account_index": account_index, "rounds": rounds,
        "period_days": period_days, "knee_pct": knee_pct, "one_order_cap_pct": one_order_cap,
        "steps": steps, "step_count": len(steps),
        "skipped": skipped,
        "sell_rules": _sell_rules(sell_rules),
        "total_target_krw": round(total_target),
        "blocked": False,
        "requires_user_approval": True,
        "auto_order_created": False,
        "note": ("기간·횟수만으로 수립한 분할 저점 지정가 전략 — 주문 아님(제안). "
                 "전략 1회 승인 후 execute_plan 으로 예약 지정가 일괄 집행(걸리면 체결·미체결=미매수). "
                 "시장가 매수 금지·자동주문 0·확정안 한도 내·매도는 규칙 사전승인+시그널 제안."),
    }


def fx_rates_for_markets(markets: dict | None) -> dict | None:
    """markets 에 외화(KRW 외) 통화가 있으면 환율 dict 반환, 없으면 None(환산 불필요·legacy).

    환율 조회 실패 시 빈 dict — build_split_plan 이 해당 통화 종목을 정직하게 스킵(가짜 환산 금지).
    """
    ccys = {c for (_, c) in (markets or {}).values() if c and c != "KRW"}
    if not ccys:
        return None  # 외화 없음 → 환산 비활성(KRW only)
    from . import price_history
    rates: dict = {}
    if "USD" in ccys:
        r = price_history.fetch_fx_usdkrw()
        if r:
            rates["USD"] = r
    return rates  # USD 환율 조회 실패 시 {} → 해당 종목 스킵


def execute_plan(plan: dict, broker, account: Account, *, approved: bool,
                 available_cash_krw: float, conn=None) -> dict:
    """**전략 1회 승인 → 예약 지정가 일괄 집행**(CEO 확정 모델).

    approved=True(전략 전체 승인)면 plan 의 모든 회차 step 을 지정가 예약주문으로 제출한다.
    걸리면 체결, 미체결이면 매수 안 함(추격·시장가 없음). live 는 submit_order 내부 게이트(§15).
    approved 아니면 거부(무승인 자동매매 금지).
    """
    if not approved:
        return {"ok": False, "reason": "전략 승인 필요 — 무승인 집행 거부(자동매매 금지)", "submitted": 0}
    rounds = sorted({s["round_no"] for s in plan.get("steps", [])})
    results = []
    for r in rounds:
        out = execute_round(plan, r, broker, account, approved=True,
                            available_cash_krw=available_cash_krw, conn=conn)
        results.append(out)
    submitted = sum(o.get("submitted", 0) for o in results)
    return {"ok": submitted > 0, "rounds_executed": len(rounds), "submitted": submitted,
            "by_round": results, "auto_order_created": False,
            "note": "예약 지정가 일괄 제출 — 미체결분은 매수되지 않음(추격 없음)."}


# ---------------------------------------------------------------------------
# CLI — 웹 '분할 진입' 자동 생성용 (기간·횟수만 받아 저점 사다리 draft 반환, 주문 X)
# ---------------------------------------------------------------------------
def main() -> int:
    import argparse
    import json
    import sys

    from . import price_history
    try:
        from .security_selection import _TICKER_META
    except Exception:  # noqa: BLE001
        _TICKER_META = {}

    ap = argparse.ArgumentParser(description="분할 저점 매수 plan 자동 생성(draft, 주문 없음)")
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--picks", required=True, help='JSON {"bucket":["TICKER",...]}')
    ap.add_argument("--rounds", type=int, default=ROUNDS_DEFAULT)
    ap.add_argument("--period", type=int, default=14)
    ap.add_argument("--cash", type=float, default=None,
                    help="예수금. 미지정 시 최신 account_snapshots.cash_krw 사용(횟수만 입력 지원)")
    ap.add_argument("--knee", type=float, default=KNEE_PCT_DEFAULT)
    ap.add_argument("--token", default=None,
                    help="plan 토큰(주문ID 구분자). 미지정 시 오늘 날짜(YYYYMMDD) — 사이클 간 stale 차단")
    ap.add_argument("--equity-option", default="none", choices=("none", "5", "10"),
                    help="개별주 carve(picks['individual'] 에 위험자산 5%/10% 균등)")
    a = ap.parse_args()

    try:
        picks = json.loads(a.picks)
    except ValueError as e:
        sys.stdout.write(json.dumps({"ok": False, "error": f"picks JSON 파싱 실패: {e}"}))
        return 0

    cash = a.cash
    if cash is None:  # 예수금 자동 조회(최신 스냅샷) — 사용자는 횟수만 입력
        from .store import db as _db
        conn = _db.connect()
        try:
            row = conn.execute(
                "SELECT cash_krw FROM account_snapshots WHERE account_index=? "
                "ORDER BY datetime(captured_at) DESC, id DESC LIMIT 1", (a.account,)).fetchone()
            cash = float(row["cash_krw"]) if row and row["cash_krw"] is not None else 0.0
        finally:
            conn.close()
    if not cash or cash <= 0:
        sys.stdout.write(json.dumps({"ok": False, "error": "예수금 없음 — 계좌 동기화(sync) 후 재시도"}))
        return 0
    tickers = [t for arr in (picks or {}).values() if isinstance(arr, list) for t in arr]
    prices: dict = {}
    markets: dict = {}
    for t in tickers:
        bars = price_history.load_history(t)
        if bars:
            try:
                prices[t] = float(bars[-1]["close"])
            except (TypeError, ValueError, KeyError):
                pass
        m = _TICKER_META.get(t, {})
        mk = m.get("market")
        if mk:
            ccy = "KRW" if mk in ("KRX", "KOSPI", "KOSDAQ") else "USD"
            markets[t] = (mk, ccy, m.get("kis_exchange", ""))

    from datetime import datetime, timezone
    token = a.token or datetime.now(timezone.utc).strftime("%Y%m%d")
    plan = build_split_plan(a.account, picks, prices=prices, cash_krw=cash,
                            rounds=a.rounds, period_days=a.period, knee_pct=a.knee,
                            markets=markets, plan_token=token, fx_rates=fx_rates_for_markets(markets),
                            equity_option=a.equity_option)
    sys.stdout.write(json.dumps(plan, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def execute_round(plan: dict, round_no: int, broker, account: Account, *,
                  approved: bool, available_cash_krw: float, conn=None) -> dict:
    """plan 의 특정 회차를 **승인 후** 집행 — 회차 step 들을 지정가 주문으로 제출.

    approved=True 가 아니면 집행 거부(무승인 자동매매 금지). live 는 submit_order 내부 게이트(§15).
    """
    if not approved:
        return {"ok": False, "reason": "사용자 승인 필요 — 무승인 집행 거부(자동매매 금지)",
                "submitted": 0}
    steps = [s for s in plan.get("steps", []) if s["round_no"] == round_no]
    if not steps:
        return {"ok": False, "reason": f"{round_no}회차 step 없음", "submitted": 0}
    results = []
    for s in steps:
        inst = Instrument(s["ticker"], s["market"], s["currency"], "etf",
                          exchange=s.get("exchange", ""))   # 미국 주문 거래소코드(OVRS_EXCG_CD)
        req = OrderRequest(client_order_id=s["client_order_id"], instrument=inst, side="buy",
                           qty=Decimal(s["qty"]), order_type="limit",
                           limit_price=Decimal(s["limit_price"]))
        r = svc.submit_order(broker, account, req, available_cash_krw=available_cash_krw,
                             risk_passed=True, conn=conn)
        results.append({"ticker": s["ticker"], "ok": r["ok"], "status": r["status"],
                        "broker_order_id": r.get("broker_order_id"), "reason": r.get("reason")})
    ok_n = sum(1 for x in results if x["ok"])
    return {"ok": ok_n > 0, "round_no": round_no, "submitted": ok_n,
            "total": len(results), "results": results, "auto_order_created": False}
