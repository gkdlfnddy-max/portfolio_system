"""Track B — Portfolio OS 핵심 실행 경로가 Growth Middleware(run_task)를 강제 통과하는지 증명.

검증:
  ① decision_compute / daily_portfolio_review / order_submit / broker_sync 가 run_task 경유(task 행 생성).
  ② prehook gate=block 시 본 함수 미실행(부작용 없음).
  ③ posthook 저장 실패 시 DONE 차단(예외 전파).
  ④ 실패는 task_failure_patterns 저장.
  ⑤ live lock 보존(미들웨어 밖 hard-fail) — KIS_LIVE_CONFIRM 없으면 예외 전파.

키 없이 임시 SQLite + MockAdapter 로 전 경로 검증(Anthropic API 미사용).
"""
from __future__ import annotations

import os
import tempfile
from decimal import Decimal

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_core_runtask.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import allocation as alloc_mod
from main_mission.portfolio_os import selection as sel_mod
from main_mission.portfolio_os import decision as decision_mod
from main_mission.portfolio_os import daily_review as dr_mod
from main_mission.portfolio_os.broker import order_service as svc
from main_mission.portfolio_os.broker import sync_job
from main_mission.portfolio_os.broker.mock_adapter import MockAdapter
from main_mission.portfolio_os.broker.port import Account, Instrument, OrderRequest
from main_mission.portfolio_os.growth import posthooks
from main_mission.portfolio_os.broker import account_status


def setup():
    store_db.init()


def _iso_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _conn():
    return store_db.connect()


def _profile(idx):
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
            "interests_text, updated_at) VALUES(?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(account_index) DO NOTHING",
            (idx, "neutral", 10.0, 30.0, "반도체, 2차전지"),
        )
        # captured_at 은 tz-aware ISO(=실제 sync 포맷). decision 본문이 fromisoformat 으로 신선도 판정.
        conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, "
            "source, is_stale, captured_at) VALUES(?,?,?,?,?,0,?)",
            (idx, 9000000, 10000000, 0, "test", _iso_now()),
        )
        conn.commit()
    finally:
        conn.close()


def _ready_account(idx):
    """profile + snapshot + selected allocation 까지 갖춘 계좌(decision 통과 가능)."""
    _profile(idx)
    out = alloc_mod.generate(idx)
    sel_mod.select(idx, out["proposal_id"], "base")


def _tasks_for(task_type, account_index):
    c = _conn()
    try:
        rows = c.execute(
            "SELECT id, status, prehook FROM tasks WHERE task_type=? AND account_index=? ORDER BY id DESC",
            (task_type, account_index),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


# ---------------------------------------------------------------------------
# ① 핵심 경로가 run_task 경유 (task 행 생성)
# ---------------------------------------------------------------------------
def test_decision_compute_goes_through_runtask():
    _ready_account(101)
    res = decision_mod.compute(101)
    assert res.get("ok"), res
    rows = _tasks_for("decision_compute", 101)
    assert rows, "decision_compute task 행 미생성 — run_task 미경유"
    assert rows[0]["status"] == "done", rows[0]


def test_daily_review_goes_through_runtask():
    _ready_account(102)
    res = dr_mod.generate_review(102)
    assert res.get("ok"), res
    rows = _tasks_for("daily_portfolio_review", 102)
    assert rows, "daily_portfolio_review task 행 미생성 — run_task 미경유"
    assert rows[0]["status"] == "done", rows[0]
    # daily_review 내부에서 decision_compute 도 run_task 경유(중첩) 했어야 함.
    assert _tasks_for("decision_compute", 102), "내부 decision_compute task 미생성"


def test_order_submit_goes_through_runtask():
    b = MockAdapter()
    acc = Account(id=103, mode="paper")
    inst = Instrument("005930", "KRX", "KRW", "stock")
    req = OrderRequest(client_order_id="rt-order-1", instrument=inst, side="buy",
                       qty=Decimal(10), order_type="limit", limit_price=Decimal(70000))
    r = svc.submit_order(b, acc, req, available_cash_krw=10_000_000)
    assert r["ok"] and r["status"] == "submitted", r
    rows = _tasks_for("order_submit", 103)
    assert rows, "order_submit task 행 미생성 — run_task 미경유"
    assert rows[0]["status"] == "done", rows[0]


def test_broker_sync_goes_through_runtask():
    # fetch 를 결정론적 성공으로 고정(키/네트워크 비의존) → 본문 ok:True, run_task 경유(task done).
    orig = sync_job.fetch
    sync_job.fetch = lambda n: {"ok": True, "mode": "paper", "tokenOk": True,
                                "cashKrw": 9900000.0, "totalValueKrw": 9900000.0, "holdings": []}
    try:
        conn = _conn()
        try:
            res = sync_job.sync_balance(104, conn)
        finally:
            conn.close()
    finally:
        sync_job.fetch = orig
    assert res["account_index"] == 104 and res["ok"] is True, res
    rows = _tasks_for("broker_sync", 104)
    assert rows, "broker_sync task 행 미생성 — run_task 미경유"
    assert rows[0]["status"] == "done", rows[0]


# ---------------------------------------------------------------------------
# ② prehook block 시 본 함수 미실행(부작용 없음)
# ---------------------------------------------------------------------------
def test_decision_block_skips_body_no_side_effects():
    # selected allocation 없음 → prehook gate=block → 본문 미실행 → decisions 행 0.
    _profile(105)  # snapshot 있으나 selection 없음
    res = decision_mod.compute(105)
    assert res.get("ok") is False and res.get("blocked") is True, res
    assert res.get("block_code") == "no_selection", res
    c = _conn()
    try:
        n_dec = c.execute("SELECT COUNT(*) n FROM decisions WHERE account_index=105").fetchone()["n"]
        n_plan = c.execute("SELECT COUNT(*) n FROM rebalance_plans WHERE account_index=105").fetchone()["n"]
    finally:
        c.close()
    assert n_dec == 0, "block인데 decisions 행이 생성됨(부작용)"
    assert n_plan == 0, "block인데 rebalance_plans 행이 생성됨(부작용)"
    rows = _tasks_for("decision_compute", 105)
    assert rows and rows[0]["status"] == "blocked", rows


def test_order_submit_block_skips_body_when_no_account_id():
    # account.id 없음 → prehook account_id 게이트 hard-block → 본문 미실행(원장 미기록).
    b = MockAdapter()
    acc = Account(id=None, mode="paper")
    inst = Instrument("005930", "KRX", "KRW", "stock")
    req = OrderRequest(client_order_id="rt-noacct-1", instrument=inst, side="buy",
                       qty=Decimal(10), order_type="limit", limit_price=Decimal(70000))
    r = svc.submit_order(b, acc, req, available_cash_krw=10_000_000)
    assert r["ok"] is False and r["status"] == "aborted", r
    rows = svc.list_orders()
    assert not any(o["client_order_id"] == "rt-noacct-1" for o in rows), "block인데 주문 원장 기록됨(부작용)"


# ---------------------------------------------------------------------------
# ③ posthook 저장 실패 시 DONE 차단 (예외 전파 → run_task 가 실패로 보고)
# ---------------------------------------------------------------------------
def test_posthook_failure_blocks_done(monkeypatch=None):
    from main_mission.portfolio_os.growth import middleware as mw

    orig = posthooks.finalize
    calls = {"n": 0}

    def boom_finalize(task_id, *, status="done", **kw):
        calls["n"] += 1
        # blocked/failed 마감(첫 호출 등)은 통과시키고, 정상 done 마감만 실패시켜 DONE 차단을 증명.
        if status == "done":
            return {"ok": False}
        return orig(task_id, status=status, **kw)

    posthooks.finalize = boom_finalize
    mw.posthooks.finalize = boom_finalize
    try:
        out = mw.run_task("theme_advice", "theme-agent", lambda i, c: {"result": 1}, account_index=1)
        # posthook 저장 실패 → RuntimeError → run_task 가 흡수해 success=False 로 보고(또는 전파).
        assert out["success"] is False, out
    except RuntimeError as e:
        assert "posthook" in str(e), e
    finally:
        posthooks.finalize = orig
        mw.posthooks.finalize = orig


# ---------------------------------------------------------------------------
# ④ 실패는 task_failure_patterns 저장
# ---------------------------------------------------------------------------
def test_failure_recorded_in_task_failure_patterns():
    # fetch 가 실패(stage=token)를 반환하도록 강제 → 동기화 실패 → run_task 가 validation 실패로
    #   task_failure_patterns 기록 + task failed.
    orig = sync_job.fetch
    sync_job.fetch = lambda n: {"ok": False, "stage": "token", "error": "stub token fail", "tokenOk": False}
    try:
        conn = _conn()
        try:
            res = sync_job.sync_balance(106, conn)
        finally:
            conn.close()
        assert res["ok"] is False, res
    finally:
        sync_job.fetch = orig
    c = _conn()
    try:
        n = c.execute(
            "SELECT COUNT(*) n FROM task_failure_patterns WHERE task_type='broker_sync' AND account_index=106"
        ).fetchone()["n"]
        st = c.execute(
            "SELECT status FROM tasks WHERE task_type='broker_sync' AND account_index=106 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        c.close()
    assert n >= 1, "broker_sync 실패가 task_failure_patterns 에 미기록"
    assert st and st["status"] == "failed", st


# ---------------------------------------------------------------------------
# ⑤ live lock 보존 — 미들웨어 밖 hard-fail (KIS_LIVE_CONFIRM 없으면 예외 전파)
# ---------------------------------------------------------------------------
class _LiveBroker:
    mode = "live"
    is_healthy = True

    def place_order(self, account, req):
        raise RuntimeError("stub")


def test_live_lock_preserved_through_runtask():
    os.environ.pop("KIS_LIVE_CONFIRM", None)
    acc = Account(id=107, mode="live")
    inst = Instrument("005930", "KRX", "KRW", "stock")
    req = OrderRequest(client_order_id="rt-live-1", instrument=inst, side="buy",
                       qty=Decimal(10), order_type="limit", limit_price=Decimal(70000))
    raised = False
    try:
        svc.submit_order(_LiveBroker(), acc, req, available_cash_krw=10_000_000)
    except RuntimeError as e:
        raised = True
        assert "KIS_LIVE_CONFIRM" in str(e), e
    assert raised, "live + KIS_LIVE_CONFIRM 없음인데 run_task 가 예외를 흡수함(하드락 손상)"
    # 본문 미실행 → 원장에 주문 없음.
    rows = svc.list_orders()
    assert not any(o["client_order_id"] == "rt-live-1" for o in rows), "live 차단인데 원장 기록됨"


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} CORE-RUNTASK TESTS PASSED")
