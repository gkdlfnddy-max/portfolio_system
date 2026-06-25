"""집행 파이프라인 E2E 회귀(mock) — 확정안→비중배분→지정가→주문→차단→회고.

실 운영(postgres)·실 돈 무관: conftest 의 격리 sqlite + MockAdapter.
영구 안전망(CLAUDE.md §11.5): 시장가 매수 차단·지정가만·자동주문 0·리스크 게이트·단일종목 한도.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import weight_allocator as wa
from main_mission.portfolio_os import lesson_runs as lr
from main_mission.portfolio_os.broker import order_service as svc
from main_mission.portfolio_os.broker.mock_adapter import MockAdapter
from main_mission.portfolio_os.broker.port import Account, Instrument, OrderRequest

_CONFIRMED = [
    {"kind": "cash", "ref": None, "weight_pct": 40.0},
    {"kind": "hedge", "ref": "반도체 인버스", "weight_pct": 1.8},
    {"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": 40.7},
    {"kind": "tilt", "ref": "로봇", "weight_pct": 8.8},
    {"kind": "tilt", "ref": "반도체", "weight_pct": 8.8},
]


def _seed(account_index=1):
    conn = store_db.connect()
    try:
        conn.execute("INSERT OR IGNORE INTO accounts(account_index, alias, mode, broker) "
                     "VALUES(?,?,?,?)", (account_index, "exec-test", "mock", "kis"))
        conn.execute(
            "INSERT INTO allocation_selections(account_index, variant, allocation, status, "
            "selected_by, selected_at) VALUES(?,?,?,?,?,?)",
            (account_index, "conservative", json.dumps(_CONFIRMED), "active", "user",
             datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()


def test_confirmed_buckets_is_truth():
    _seed()
    cb = wa.confirmed_buckets(1)
    assert cb["ok"] and cb["variant"] == "conservative"
    assert cb["defensive"]["cash_pct"] == 40.0
    keys = {b["key"] for b in cb["buckets"]}
    assert {"global_core", "robotics", "semiconductor", "semiconductor_inverse"} <= keys


def test_single_etf_over_single_name_cap_is_blocked():
    """anchor 40.7% 를 단일 ETF 에 넣으면 단일종목 한도 초과 → 리스크 게이트 차단(정직)."""
    _seed()
    alloc = wa.allocate(1, {"global_core": ["VOO"], "robotics": ["BOTZ"],
                            "semiconductor": ["SOXX"], "semiconductor_inverse": ["SOXS"]})
    assert alloc["ok"] and alloc["blocked"] is True
    assert any(w["level"] == "block" for w in alloc["over_limit_warnings"])


def test_split_anchor_clears_block_and_drafts_100():
    _seed()
    alloc = wa.allocate(1, {"global_core": ["VOO", "VTI", "VT"], "robotics": ["BOTZ"],
                            "semiconductor": ["SOXX"], "semiconductor_inverse": ["SOXS"]})
    assert alloc["ok"] and alloc["blocked"] is False
    assert alloc["total_is_100"] is True
    assert alloc["auto_order_created"] is False and alloc["db_write"] is False
    # 각 종목 단일종목 한도 이내
    sng = alloc["limits"].get("single_name_max_pct", 20.0)
    for h in alloc["holdings"]:
        if h.get("ticker"):
            assert h["weight_pct"] <= sng + 1e-6, h


def test_limit_orders_submit_via_mock():
    _seed()
    broker = MockAdapter()
    acc = Account(id=1, mode=broker.mode)
    picks = [("VOO", 744800), ("VTI", 372400), ("BOTZ", 41160), ("SOXS", 8820)]
    oks = []
    for tk, limit in picks:
        inst = Instrument(tk, "NYSE", "USD", "etf")
        req = OrderRequest(client_order_id=f"exec-{tk}", instrument=inst, side="buy",
                           qty=Decimal(2), order_type="limit", limit_price=Decimal(limit))
        r = svc.submit_order(broker, acc, req, available_cash_krw=9_900_000, risk_passed=True)
        oks.append(r)
    assert all(r["ok"] and r["status"] == "submitted" for r in oks), oks
    assert all(r.get("broker_order_id") for r in oks)
    subs = {o["ticker"] for o in svc.list_orders(status="submitted")}
    assert {"VOO", "VTI", "BOTZ", "SOXS"} <= subs


def test_market_buy_is_blocked():
    _seed()
    broker = MockAdapter()
    acc = Account(id=1, mode=broker.mode)
    inst = Instrument("VOO", "NYSE", "USD", "etf")
    req = OrderRequest(client_order_id="exec-mkt", instrument=inst, side="buy",
                       qty=Decimal(1), order_type="market", limit_price=None)
    r = svc.submit_order(broker, acc, req, available_cash_krw=9_900_000, risk_passed=True)
    assert r["ok"] is False and r["status"] == "aborted"
    assert "시장가" in (r.get("reason") or "")


def test_reflection_recorded_after_submit():
    _seed()
    res = lr.record_lesson("etf", "VOO", account_index=1,
                           decision_context="확정안 집행 지정가 진입(mock)",
                           suggested_action="limit_buy_knee", user_action="accepted",
                           lesson_text="mock 검증")
    assert res["ok"] and res["hit_or_miss"] == "pending"
    runs = lr.recent_runs("etf", "VOO")
    assert any(x.get("user_action") == "accepted" for x in runs)
