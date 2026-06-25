"""Risk gate 회귀 테스트 — 안전 핵심. 한도 위반이 반드시 hard-block 되는지 검증.

실행: python -m main_mission.portfolio_os.tests.test_risk_gate
(pytest 없이도 돌도록 assert + __main__ 러너)
"""
from decimal import Decimal

from main_mission.portfolio_os.risk.gate import (
    PostTradeWeights,
    RiskLimits,
    check_trades,
)

LIMITS = RiskLimits()


def _w(**kw):
    base = dict(cash_pct=Decimal(30), single_name_max_pct=Decimal(10),
               short_total_pct=Decimal(5), leverage_total_pct=Decimal(5),
               largest_order_pct=Decimal(3), order_count=5)
    base.update(kw)
    return PostTradeWeights(**base)


def test_clean_passes():
    assert check_trades(_w(), LIMITS).passed is True


def test_cash_below_min_blocks():
    r = check_trades(_w(cash_pct=Decimal(5)), LIMITS)
    assert r.passed is False
    assert any(v.limit == "cash_min_pct" for v in r.violations)


def test_single_name_over_blocks():
    r = check_trades(_w(single_name_max_pct=Decimal(25)), LIMITS)
    assert r.passed is False
    assert any(v.limit == "single_name_max_pct" for v in r.violations)


def test_short_over_blocks():
    r = check_trades(_w(short_total_pct=Decimal(15)), LIMITS)
    assert r.passed is False
    assert any(v.limit == "short_total_max_pct" for v in r.violations)


def test_leverage_over_blocks():
    r = check_trades(_w(leverage_total_pct=Decimal(20)), LIMITS)
    assert r.passed is False


def test_big_order_blocks():
    r = check_trades(_w(largest_order_pct=Decimal(8)), LIMITS)
    assert r.passed is False


def test_multiple_violations_collected():
    r = check_trades(_w(cash_pct=Decimal(2), single_name_max_pct=Decimal(40)), LIMITS)
    assert r.passed is False
    assert len(r.violations) >= 2


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"  PASS  {t.__name__}")
    print(f"\n{passed}/{len(tests)} risk-gate tests passed - hard-block verified")
