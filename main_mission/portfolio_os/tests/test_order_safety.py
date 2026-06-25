"""주문 안전 서비스 테스트 — idempotency / 모드가드 / 리스크 / 매수여력 / in_doubt / 감사 / 비밀값차단.

키 없이 MockAdapter + 임시 SQLite 로 전 경로 검증.
"""
from __future__ import annotations

import os
import tempfile
from decimal import Decimal

# 임시 DB 경로를 .env 보다 먼저 주입 (load_dotenv override=False).
_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_orders.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os.broker import order_service as svc
from main_mission.portfolio_os.broker.mock_adapter import MockAdapter
from main_mission.portfolio_os.broker.port import Account, Instrument, OrderRequest, OrderAck
from main_mission.portfolio_os.audit import logger as audit
from main_mission.portfolio_os.audit.logger import AuditError

_INST = Instrument("005930", "KRX", "KRW", "stock")


def _req(cid: str, qty=10, price=70000, side="buy"):
    return OrderRequest(client_order_id=cid, instrument=_INST, side=side,
                        qty=Decimal(qty), order_type="limit", limit_price=Decimal(price))


def _mreq(cid: str, side="sell", qty=10):
    return OrderRequest(client_order_id=cid, instrument=_INST, side=side,
                        qty=Decimal(qty), order_type="market", limit_price=None)


class _RaisingBroker:
    mode = "paper"
    is_healthy = True
    def place_order(self, account, req):
        raise RuntimeError("socket timeout")


def setup():
    store_db.init()


def test_normal_submit():
    b = MockAdapter()
    acc = Account(id=1, mode="paper")
    r = svc.submit_order(b, acc, _req("ok-1"), available_cash_krw=10_000_000)
    assert r["ok"] and r["status"] == "submitted", r
    assert r["broker_order_id"], r
    rows = svc.list_orders(status="submitted")
    assert any(o["client_order_id"] == "ok-1" for o in rows), rows


def test_idempotent_same_payload():
    b = MockAdapter()
    acc = Account(id=1, mode="paper")
    svc.submit_order(b, acc, _req("dup-1"), available_cash_krw=10_000_000)
    r2 = svc.submit_order(b, acc, _req("dup-1"), available_cash_krw=10_000_000)
    assert r2["ok"] and r2.get("duplicate") is True, r2  # 재전송 안 함


def test_dup_id_different_payload_rejected():
    b = MockAdapter()
    acc = Account(id=1, mode="paper")
    svc.submit_order(b, acc, _req("diff-1", qty=10), available_cash_krw=10_000_000)
    r2 = svc.submit_order(b, acc, _req("diff-1", qty=999), available_cash_krw=10_000_000)
    assert not r2["ok"] and "different payload" in r2["reason"], r2


def test_mode_mismatch_aborted():
    b = MockAdapter()  # paper
    acc = Account(id=1, mode="live")  # mismatch
    r = svc.submit_order(b, acc, _req("mm-1"), available_cash_krw=10_000_000)
    assert not r["ok"] and r["status"] == "aborted" and "mode mismatch" in r["reason"], r


def test_risk_block_aborted():
    b = MockAdapter()
    acc = Account(id=1, mode="paper")
    r = svc.submit_order(b, acc, _req("rk-1"), risk_passed=False, available_cash_krw=10_000_000)
    assert not r["ok"] and r["status"] == "aborted" and "risk" in r["reason"], r


def test_insufficient_buying_power():
    b = MockAdapter()
    acc = Account(id=1, mode="paper")
    r = svc.submit_order(b, acc, _req("bp-1", qty=10, price=70000), available_cash_krw=100_000)
    assert not r["ok"] and "insufficient_buying_power" in r["reason"], r


def test_market_buy_blocked():
    b = MockAdapter()
    acc = Account(id=1, mode="paper")
    r = svc.submit_order(b, acc, _mreq("mb-1", side="buy"))
    assert not r["ok"] and r["status"] == "aborted" and "시장가 매수" in r["reason"], r


def test_market_sell_blocked_without_urgent():
    b = MockAdapter()
    acc = Account(id=1, mode="paper")
    r = svc.submit_order(b, acc, _mreq("ms-1", side="sell"))
    assert not r["ok"] and "시장가 매도" in r["reason"], r


def test_urgent_market_sell_allowed():
    b = MockAdapter()
    acc = Account(id=1, mode="paper")
    r = svc.submit_order(b, acc, _mreq("us-1", side="sell"), urgent_sell=True)
    assert r["ok"] and r["status"] == "submitted", r


def test_in_doubt_on_exception():
    acc = Account(id=1, mode="paper")
    r = svc.submit_order(_RaisingBroker(), acc, _req("id-1"), available_cash_krw=10_000_000)
    assert not r["ok"] and r["status"] == "in_doubt", r
    rows = svc.list_orders(status="in_doubt")
    assert any(o["client_order_id"] == "id-1" for o in rows), rows


def test_audit_blocks_secret():
    try:
        audit.record("leak_test", payload={"app_key": "PS123", "x": 1})
        assert False, "비밀값이 감사로그에 기록됨 (차단 실패)"
    except AuditError:
        pass


def test_audit_records_order():
    # self-contained: 주문을 직접 한 건 제출해 order_submit 감사로그를 만든 뒤 검증 — 앞 테스트 의존 제거.
    svc.submit_order(MockAdapter(), Account(id=1, mode="paper"),
                     _req("audit-1"), available_cash_krw=10_000_000)
    conn = store_db.connect()
    try:
        n = conn.execute("SELECT count(*) FROM audit_logs WHERE action='order_submit'").fetchone()[0]
        assert n >= 1, f"order_submit 감사로그 없음 ({n})"
    finally:
        conn.close()


if __name__ == "__main__":
    setup()
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]  # 정의 순서
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} ORDER-SAFETY TESTS PASSED")
