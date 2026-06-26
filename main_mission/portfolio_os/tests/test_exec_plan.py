"""분할 매수 집행 플래너/실행기(exec_plan) — mock E2E.

검증: 확정안+picks → 분할 회차 지정가 plan(주문X) → 승인 후 회차 집행(지정가) · 무승인 거부 · 시장가 없음.
격리 sqlite + MockAdapter (실 돈 무관).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import exec_plan
from main_mission.portfolio_os.broker.mock_adapter import MockAdapter
from main_mission.portfolio_os.broker.port import Account

_CONFIRMED = [
    {"kind": "cash", "ref": None, "weight_pct": 40.0},
    {"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": 40.7},
    {"kind": "tilt", "ref": "로봇", "weight_pct": 8.8},
    {"kind": "tilt", "ref": "반도체", "weight_pct": 8.8},
    {"kind": "hedge", "ref": "반도체 인버스", "weight_pct": 1.8},
]
_PICKS = {"global_core": ["VOO", "VTI", "VT"], "robotics": ["BOTZ"],
          "semiconductor": ["SOXX"], "semiconductor_inverse": ["SOXS"]}
_PRICES = {"VOO": 760000, "VTI": 380000, "VT": 170000,
           "BOTZ": 42000, "SOXX": 330000, "SOXS": 9000}
_MKT = {t: ("NYSE", "USD") for t in _PRICES}
_CASH = 9_900_000


def _seed(acc=1):
    conn = store_db.connect()
    try:
        conn.execute("INSERT OR IGNORE INTO accounts(account_index, alias, mode, broker) VALUES(?,?,?,?)",
                     (acc, "exec-plan-test", "mock", "kis"))
        conn.execute("INSERT INTO allocation_selections(account_index, variant, allocation, status, "
                     "selected_by, selected_at) VALUES(?,?,?,?,?,?)",
                     (acc, "conservative", json.dumps(_CONFIRMED), "active", "user",
                      datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()


def test_build_split_plan_proposes_rounds_no_order():
    _seed()
    plan = exec_plan.build_split_plan(1, _PICKS, prices=_PRICES, cash_krw=_CASH,
                                      rounds=3, markets=_MKT)
    assert plan["ok"] and plan["rounds"] == 3
    assert plan["auto_order_created"] is False and plan["requires_user_approval"] is True
    assert plan["step_count"] >= 1
    # 모든 step 은 지정가 매수, 회차/한도 정보 포함
    for s in plan["steps"]:
        assert s["order_type"] == "limit" and s["side"] == "buy"
        assert 1 <= s["round_no"] <= 3 and s["total_rounds"] == 3
        assert s["limit_price"] > 0 and s["qty"] >= 1
        assert s["on_unfilled"] == "no_chase"   # 미체결이면 매수 안 함(추격·시장가 없음)
        # 1주문 회차 비중은 one_order_cap 이내
        assert s["cycle_pct"] <= plan["one_order_cap_pct"] + 1e-6
    # 저점 사다리: 같은 종목은 회차가 깊을수록 지정가가 낮아진다(VOO 3회차).
    voo = sorted([s for s in plan["steps"] if s["ticker"] == "VOO"], key=lambda s: s["round_no"])
    if len(voo) >= 2:
        assert voo[0]["limit_price"] > voo[-1]["limit_price"]
        assert voo[0]["drop_pct"] < voo[-1]["drop_pct"]


def test_no_price_is_skipped_not_faked():
    _seed()
    prices = dict(_PRICES); prices.pop("SOXS")  # 가격 미연동
    plan = exec_plan.build_split_plan(1, _PICKS, prices=prices, cash_krw=_CASH, markets=_MKT)
    assert all(s["ticker"] != "SOXS" for s in plan["steps"])
    assert any(sk.get("ticker") == "SOXS" for sk in plan["skipped"])


def test_execute_round_requires_approval():
    _seed()
    plan = exec_plan.build_split_plan(1, _PICKS, prices=_PRICES, cash_krw=_CASH, markets=_MKT)
    broker = MockAdapter()
    acc = Account(id=1, mode=broker.mode)
    refused = exec_plan.execute_round(plan, 1, broker, acc, approved=False,
                                      available_cash_krw=_CASH)
    assert refused["ok"] is False and refused["submitted"] == 0
    assert "승인" in refused["reason"]


def test_execute_round_submits_limit_orders_when_approved():
    _seed()
    plan = exec_plan.build_split_plan(1, _PICKS, prices=_PRICES, cash_krw=_CASH, markets=_MKT)
    broker = MockAdapter()
    acc = Account(id=1, mode=broker.mode)
    out = exec_plan.execute_round(plan, 1, broker, acc, approved=True, available_cash_krw=_CASH)
    assert out["ok"] and out["submitted"] >= 1 and out["auto_order_created"] is False
    assert all(r["status"] == "submitted" for r in out["results"] if r["ok"])
    subs = {o["ticker"] for o in svc_list()}
    assert subs, "지정가 주문이 원장에 기록돼야 함"


def svc_list():
    from main_mission.portfolio_os.broker import order_service as svc
    return svc.list_orders(status="submitted")


def test_blocked_when_single_etf_over_cap():
    _seed()
    plan = exec_plan.build_split_plan(1, {"global_core": ["VOO"]}, prices=_PRICES,
                                      cash_krw=_CASH, markets=_MKT)
    assert plan["ok"] is False and plan.get("blocked") is True
