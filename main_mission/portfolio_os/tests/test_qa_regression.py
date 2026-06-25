"""Agent 6 개선 1/2 — E2E 회귀 + 계좌 격리/보안 회귀(통합 안전망).

개선 1 (E2E): 확정안 저장 → 후보평가 → 6축 → prehook 최우선 truth → **승인 전 미반영·자동주문 0**.
개선 2 (격리): 계좌별 selected_allocation / asset_memory 가 다른 계좌로 새지 않음.
"""
from __future__ import annotations

import json
import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_qa_regression.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import memory_prehook as ph
from main_mission.portfolio_os import asset_memory as am
from main_mission.portfolio_os import security_selection as ss
from main_mission.portfolio_os.decline import composite as composite_mod


def setup():
    store_db.init()


def _confirm_allocation(account_index: int, variant: str, rows: list[dict]):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO allocation_selections(account_index, variant, allocation, status, "
            "selected_by, selected_at) VALUES(?,?,?,?,?,?)",
            (account_index, variant, json.dumps(rows), "active", "user", "2026-06-25T00:00:00+00:00"))
        conn.commit()
    finally:
        conn.close()


# ── 개선 1: E2E — 확정안 → 후보 → 6축 → 승인 전 미반영·자동주문 0 ──
def test_e2e_no_apply_no_auto_order():
    setup()
    _confirm_allocation(9, "base", [{"ticker": "SPY", "weight_pct": 60}])

    # prehook: 확정안이 최우선 truth 로 로드
    ctx = ph.prehook_context(9, "stock", "005930")
    assert ctx["selected_allocation"] is not None
    assert ctx["applied"] is False and ctx["advisory_only"] is True

    # 후보 평가: 전부 승인 필요·자동주문/적용 0·가짜 비중 0
    cl = ss.classify_bucket(9, "semiconductor")
    assert cl["ok"]
    for c in cl["normalized"]:
        assert c["approval_required"] is True
        assert c["auto_order_created"] is False and c["auto_applied"] is False
        assert c["suggested_weight"] is None

    # 6축 종합: 자동주문 0
    comp = composite_mod.composite({})
    assert comp["auto_order_created"] is False


def test_no_orders_table_written_during_analysis():
    setup()
    _confirm_allocation(9, "base", [{"ticker": "SPY", "weight_pct": 60}])
    conn = store_db.connect()
    try:
        before = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    finally:
        conn.close()
    ph.prehook_context(9, "stock", "005930")
    ss.classify_bucket(9, "semiconductor")
    conn = store_db.connect()
    try:
        after = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    finally:
        conn.close()
    assert after == before, "분석 단계에서 주문이 생성되면 안 됨(승인 전 미반영)"


# ── 개선 2: 계좌 격리 — 확정안/메모리가 다른 계좌로 새지 않음 ──
def test_selected_allocation_account_isolation():
    setup()
    _confirm_allocation(9, "base", [{"ticker": "SPY", "weight_pct": 60}])
    _confirm_allocation(10, "aggressive", [{"ticker": "QQQ", "weight_pct": 90}])
    c9 = ph.prehook_context(9, "stock", "005930")["selected_allocation"]
    c10 = ph.prehook_context(10, "stock", "005930")["selected_allocation"]
    assert c9["variant"] == "base" and c10["variant"] == "aggressive"
    assert c9["allocation_rows"][0]["ticker"] == "SPY"
    assert c10["allocation_rows"][0]["ticker"] == "QQQ"


def test_account_asset_memory_isolation():
    setup()
    am.record("stock", "005930", "user_view", account_index=9, title="계좌9 견해", body="보수적")
    am.record("stock", "005930", "user_view", account_index=10, title="계좌10 견해", body="공격적")
    m9 = am.search(scope_type="stock", scope_key="005930", account_index=9)
    m10 = am.search(scope_type="stock", scope_key="005930", account_index=10)
    assert all("계좌10" not in (x.get("title") or "") for x in m9)
    assert all("계좌9" not in (x.get("title") or "") for x in m10)


def test_market_only_context_has_no_account_leak():
    setup()
    _confirm_allocation(9, "base", [{"ticker": "SPY", "weight_pct": 60}])
    ctx = ph.prehook_context(None, "stock", "005930")  # 시장 공통(계좌 없음)
    assert ctx["selected_allocation"] is None
    assert ctx["user_views"] == [] and ctx["asset_memory_user"] == []


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"ALL {len(fns)} QA-REGRESSION TESTS PASSED")
