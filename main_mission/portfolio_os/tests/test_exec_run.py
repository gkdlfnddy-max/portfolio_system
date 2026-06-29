"""exec_run — 분할 집행 실행기(웹 집행 배선)의 안전 게이트 + mock E2E.

회귀 고정: 무승인 거부 · mode 명시 · live 이중확인 · plan 0건 거부 · mock 정상 제출.
실 돈 무관(MockAdapter + 격리 sqlite). Anthropic API 미사용.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import exec_run

# exec_plan 테스트와 동일한 확정안/픽스(가격 충분 → 회차 생성).
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
_CASH = 9_900_000


def _seed(acc=1):
    conn = store_db.connect()
    try:
        conn.execute("INSERT OR IGNORE INTO accounts(account_index, alias, mode, broker) VALUES(?,?,?,?)",
                     (acc, "exec-run-test", "mock", "kis"))
        conn.execute("INSERT INTO allocation_selections(account_index, variant, allocation, status, "
                     "selected_by, selected_at) VALUES(?,?,?,?,?,?)",
                     (acc, "conservative", json.dumps(_CONFIRMED), "active", "user",
                      datetime.now(timezone.utc).isoformat()))
        # 가격(일봉) seed — exec_run 은 price_history.load_history 로 현재가를 읽는다.
        for tk, px in _PRICES.items():
            conn.execute("INSERT INTO price_history(instrument_code, trade_date, open, high, low, close, "
                         "volume, source, captured_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (tk, "2026-06-26", px, px, px, px, 1000, "test",
                          datetime.now(timezone.utc).isoformat()))
        # 예수금 스냅샷(자동 cash 조회 경로).
        conn.execute("INSERT INTO account_snapshots(account_index, cash_krw, captured_at) VALUES(?,?,?)",
                     (acc, _CASH, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()


def setup_function():
    store_db.init()


def test_refuses_without_approval():
    _seed()
    out = exec_run.run(1, _PICKS, mode="mock", approve=False)
    assert out["ok"] is False and out["stage"] == "approval" and out["submitted"] == 0


def test_refuses_invalid_mode():
    _seed()
    out = exec_run.run(1, _PICKS, mode="LIVE", approve=True)  # 대문자/오타 → 거부
    assert out["ok"] is False and out["stage"] == "mode"


def test_live_requires_double_confirm():
    _seed()
    out = exec_run.run(1, _PICKS, mode="live", approve=True)  # 이중확인 문구 없음
    assert out["ok"] is False and out["stage"] == "live_confirm" and out["submitted"] == 0


def test_mock_executes_all_rounds():
    _seed()
    out = exec_run.run(1, _PICKS, mode="mock", approve=True, rounds=3, cash=_CASH)
    assert out["ok"] is True and out["stage"] == "done" and out["mode"] == "mock"
    assert out["submitted"] >= 1
    assert out["submitted"] == out["plan_steps"]      # 모든 회차 step 예약 제출
    assert out.get("auto_order_created") is False      # 자동주문 아님(승인 1회→일괄)


def _orders_count():
    conn = store_db.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        conn.close()


def test_idempotent_resubmit_no_duplicate_ledger(tmp_token="tok1"):
    _seed()
    first = exec_run.run(1, _PICKS, mode="mock", approve=True, cash=_CASH, token=tmp_token)
    assert first["ok"] and first["submitted"] >= 1
    n1 = _orders_count()
    # 같은 token=같은 client_order_id → 재집행해도 원장에 **중복 주문이 생기지 않는다**(idempotency).
    exec_run.run(1, _PICKS, mode="mock", approve=True, cash=_CASH, token=tmp_token)
    n2 = _orders_count()
    assert n2 == n1  # 원장 행 수 불변 = 중복 미생성


def test_cli_refuses_without_approve(capsys):
    _seed()
    rc = exec_run.main(["--account", "1", "--picks", json.dumps(_PICKS), "--mode", "mock"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is False and out["stage"] == "approval"
