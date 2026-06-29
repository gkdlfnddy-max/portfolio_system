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
        # 버림+1주보장: total_rounds 는 살 수 있는 주식 수만큼(≤ 요청 rounds). qty 는 항상 ≥1주.
        assert 1 <= s["round_no"] <= s["total_rounds"] <= 3
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


def test_period_and_sell_rules_envelope():
    _seed()
    plan = exec_plan.build_split_plan(1, _PICKS, prices=_PRICES, cash_krw=_CASH,
                                      rounds=3, period_days=21, markets=_MKT,
                                      sell_rules={"target_pct": 20, "stop_pct": 8})
    assert plan["period_days"] == 21
    sr = plan["sell_rules"]
    assert sr["target_pct"] == 20 and sr["stop_pct"] == 8
    assert sr["conservative_switch"] is True
    assert sr["discretionary"] == "propose_then_approve"   # 재량 매도는 제안→승인
    # 회차가 기간 내에 분산(schedule_day 증가) — 다회차를 가진 종목으로 확인
    from collections import Counter
    cnt = Counter(s["ticker"] for s in plan["steps"])
    multi = next(t for t, n in cnt.items() if n >= 2)
    seq = sorted([s for s in plan["steps"] if s["ticker"] == multi], key=lambda s: s["round_no"])
    days = [s["schedule_day"] for s in seq]
    assert days == sorted(days) and days[0] == 0 and days[-1] < 21


def test_plan_token_changes_order_ids_across_cycles():
    """다른 사이클(토큰)은 다른 client_order_id → stale idempotency 로 미래 재진입이 막히지 않음."""
    _seed()
    p1 = exec_plan.build_split_plan(1, _PICKS, prices=_PRICES, cash_krw=_CASH, markets=_MKT, plan_token="20260626")
    p2 = exec_plan.build_split_plan(1, _PICKS, prices=_PRICES, cash_krw=_CASH, markets=_MKT, plan_token="20260703")
    ids1 = {s["client_order_id"] for s in p1["steps"]}
    ids2 = {s["client_order_id"] for s in p2["steps"]}
    assert ids1 and ids2 and ids1.isdisjoint(ids2)
    # 같은 토큰은 동일 ID(같은 plan 재승인은 idempotent)
    p1b = exec_plan.build_split_plan(1, _PICKS, prices=_PRICES, cash_krw=_CASH, markets=_MKT, plan_token="20260626")
    assert {s["client_order_id"] for s in p1b["steps"]} == ids1


def test_execute_plan_one_approval_all_rounds():
    """전략 1회 승인 → 예약 지정가 전량 집행(CEO 확정 모델)."""
    _seed()
    plan = exec_plan.build_split_plan(1, _PICKS, prices=_PRICES, cash_krw=_CASH,
                                      rounds=3, markets=_MKT)
    broker = MockAdapter()
    acc = Account(id=1, mode=broker.mode)
    refused = exec_plan.execute_plan(plan, broker, acc, approved=False, available_cash_krw=_CASH)
    assert refused["ok"] is False and refused["submitted"] == 0
    out = exec_plan.execute_plan(plan, broker, acc, approved=True, available_cash_krw=_CASH)
    assert out["ok"] and out["rounds_executed"] == 3
    assert out["submitted"] == plan["step_count"]   # 모든 회차 step 예약 제출
    assert out["auto_order_created"] is False


# 실제 시드 티커(instrument_master 검증 통과) + 통제 가격으로 floor/drop 검증.
_MKT_KR = {t: ("KRX", "KRW") for t in ["000990", "042700", "005930", "000660"]}


def test_floor_share_guarantee_and_drop():
    """버림(floor)+1주 보장+드롭 — 시드 작으면 못 사고, 살 수 있으면 정수 주식 ≥1주."""
    from main_mission.portfolio_os import instrument_master as im
    _seed()
    im.seed()  # 개별주 carve 는 instrument_master(DB) 로 검증 — 실종목만.
    picks = {"individual": ["000990", "042700", "005930", "000660"]}
    prices = {"000990": 10000, "042700": 120000, "005930": 354000, "000660": 2592000}
    plan = exec_plan.build_split_plan(1, picks, prices=prices, cash_krw=9_900_000,
                                      rounds=3, markets=_MKT_KR, equity_option="10")
    assert plan["ok"]
    by_t: dict = {}
    for s in plan["steps"]:
        assert s["qty"] >= 1                       # 1주 보장(소수주 없음)
        assert 1 <= s["round_no"] <= s["total_rounds"] <= 3
        by_t[s["ticker"]] = by_t.get(s["ticker"], 0) + s["qty"]
    assert by_t.get("000990", 0) >= 1              # 싼 종목(1만)은 2% 예산(198k)으로 여러 주
    # carve 2%=198k. 005930(35만) 1주 못 삼 → 드롭(시드 부족). 000660(259만) 1주문 한도 초과 → 드롭.
    drops = {sk["ticker"]: sk["reason"] for sk in plan.get("skipped", []) if "ticker" in sk}
    assert "005930" in drops and "시드 부족" in drops["005930"]
    assert "000660" in drops and "한도" in drops["000660"]


def test_qty_times_price_within_one_order_cap():
    """모든 회차의 주문금액(qty×지정가)은 1주문 한도(one_order_cap) 이내."""
    from main_mission.portfolio_os import instrument_master as im
    _seed()
    im.seed()
    plan = exec_plan.build_split_plan(1, {"individual": ["000990"]}, prices={"000990": 10000},
                                      cash_krw=9_900_000, rounds=2, markets=_MKT_KR, equity_option="10")
    cap_krw = 9_900_000 * plan["one_order_cap_pct"] / 100.0
    for s in plan["steps"]:
        assert s["qty"] * s["limit_price"] <= cap_krw + 1
