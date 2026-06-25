"""CEO audit 보완 검증 — live 하드락 / sum100 / net·gross·hedge_ratio 노출.

키 없이 MockAdapter + 임시 SQLite 로 전 경로 검증.
  - live 하드락: 제출 시점에도 KIS_LIVE_CONFIRM 재확인 (mock 통과, live+무확인 차단)
  - sum100: allocation._variant 합계 항상 100 / decision 의 목표비중 합 검증 violation
  - 노출: net = 롱 − 숏, gross = 롱 + 숏, hedge_ratio = 숏/롱 (방어 제외, 포트폴리오 합계)
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal

# 임시 DB 경로를 .env 보다 먼저 주입 (load_dotenv override=False).
_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_exposure.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os.broker import order_service as svc
from main_mission.portfolio_os.broker.mock_adapter import MockAdapter
from main_mission.portfolio_os.broker.port import Account, Instrument, OrderRequest
from main_mission.portfolio_os import allocation as alloc_mod
from main_mission.portfolio_os import decision as decision_mod

_INST = Instrument("005930", "KRX", "KRW", "stock")


def _req(cid: str, qty=10, price=70000, side="buy"):
    return OrderRequest(client_order_id=cid, instrument=_INST, side=side,
                        qty=Decimal(qty), order_type="limit", limit_price=Decimal(price))


_PLACE_CALLED = {"n": 0}


class _LiveBroker:
    """live 모드 브로커 스텁. place_order 가 호출되면 카운터 증가(하드락 통과 증거)."""
    mode = "live"
    is_healthy = True

    def place_order(self, account, req):
        _PLACE_CALLED["n"] += 1
        raise RuntimeError("stub: 전송 안 함")  # in_doubt 로 귀결 (전송 자체는 시도됨)


def setup():
    store_db.init()


# ---------- 1) live 하드락 (Top위험#1) ----------
def test_live_hardlock_blocks_without_confirm():
    os.environ.pop("KIS_LIVE_CONFIRM", None)
    acc = Account(id=1, mode="live")
    raised = False
    try:
        svc.submit_order(_LiveBroker(), acc, _req("live-1"), available_cash_krw=10_000_000)
    except RuntimeError as e:
        raised = True
        assert "KIS_LIVE_CONFIRM" in str(e), e
    assert raised, "live + KIS_LIVE_CONFIRM 없음인데 차단되지 않음"


def test_mock_path_unaffected():
    os.environ.pop("KIS_LIVE_CONFIRM", None)  # mock/paper 는 확인 불필요
    acc = Account(id=1, mode="paper")
    b = MockAdapter()  # mode == "paper"
    r = svc.submit_order(b, acc, _req("mock-1"), available_cash_krw=10_000_000)
    assert r["ok"] and r["status"] == "submitted", r


def test_live_hardlock_does_not_call_place_when_unconfirmed():
    os.environ.pop("KIS_LIVE_CONFIRM", None)
    _PLACE_CALLED["n"] = 0
    acc = Account(id=1, mode="live")
    try:
        svc.submit_order(_LiveBroker(), acc, _req("live-x"), available_cash_krw=10_000_000)
    except RuntimeError:
        pass
    assert _PLACE_CALLED["n"] == 0, "하드락 전에 place_order 가 호출되면 안 됨"


def test_live_hardlock_passes_with_confirm():
    os.environ["KIS_LIVE_CONFIRM"] = "I_UNDERSTAND"
    _PLACE_CALLED["n"] = 0
    try:
        acc = Account(id=1, mode="live")
        # 확인이 있으면 하드락 통과 → place_order 까지 도달(스텁 RuntimeError → in_doubt).
        r = svc.submit_order(_LiveBroker(), acc, _req("live-2"), available_cash_krw=10_000_000)
        assert _PLACE_CALLED["n"] == 1, "확인 있으면 place_order 까지 가야 함"
        assert r["status"] == "in_doubt", r
    finally:
        os.environ.pop("KIS_LIVE_CONFIRM", None)


# ---------- 2) sum100 (위험#5) ----------
def test_variant_sum_always_100():
    rows = alloc_mod._variant("base", 30.0, ["로봇", "바이오"], ["반도체"],
                              sector_max=30.0, inverse_max=10.0, bond_pct=25.0,
                              region_targets={"미국": 60, "한국": 40}, duration="단기")
    assert round(sum(r["weight_pct"] for r in rows), 1) == 100.0, rows


def test_variant_sum_100_no_themes():
    rows = alloc_mod._variant("aggressive", 10.0, [], [], sector_max=30.0, inverse_max=10.0)
    assert round(sum(r["weight_pct"] for r in rows), 1) == 100.0, rows


def test_variant_sum_100_sector_capped():
    # 섹터 상한으로 깎여 under-invest 되는 케이스도 100 으로 흡수돼야 함
    rows = alloc_mod._variant("aggressive", 5.0, ["a", "b", "c"], [], sector_max=1.0, inverse_max=10.0)
    assert round(sum(r["weight_pct"] for r in rows), 1) == 100.0, rows


# ---------- 3) net/gross/hedge_ratio (F#6) — decision.compute 경유 ----------
def _seed_decision(account_index: int, alloc_rows: list[dict], total=10_000_000.0, cash_krw=3_000_000.0):
    now = datetime.now(timezone.utc).isoformat()
    conn = store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, "
            "source, captured_at) VALUES(?,?,?,?,?,?)",
            (account_index, cash_krw, total, 0, "manual_sync", now))
        conn.execute(
            "INSERT INTO allocation_selections(account_index, proposal_id, variant, allocation, "
            "account_snapshot_id, precheck_status, status, selected_at) VALUES(?,?,?,?,?,?,?,?)",
            (account_index, "p-exp", "base", json.dumps(alloc_rows, ensure_ascii=False),
             cur.lastrowid, "pass", "active", now))
        conn.commit()
    finally:
        conn.close()


def test_exposure_net_gross_hedge_ratio():
    # 예: 롱 57 (anchor 40 + tilt 17) + 인버스 4 + 방어 39(cash 30 + bond 9) = 100
    alloc_rows = [
        {"kind": "cash", "ref": None, "weight_pct": 30.0},
        {"kind": "bond", "ref": "국채", "weight_pct": 9.0},
        {"kind": "hedge", "ref": "반도체 인버스", "weight_pct": 4.0},
        {"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": 40.0},
        {"kind": "tilt", "ref": "로봇", "weight_pct": 17.0},
    ]
    _seed_decision(11, alloc_rows)
    res = decision_mod.compute(11)
    assert res.get("ok"), res
    assert res["alloc_sum_pct"] == 100.0, res
    # 롱 = 40 + 17 = 57, 숏 = 4
    assert res["gross_exposure_pct"] == 61.0, res     # 57 + 4
    assert res["net_exposure_pct"] == 53.0, res       # 57 - 4
    assert res["hedge_ratio_pct"] == 7.0, res         # 4/57 ≈ 7.0%
    # 방어(현금+국채)는 노출에서 제외 (gross 가 100 미만이어야)
    assert res["gross_exposure_pct"] < 100.0, res


def test_alloc_sum_violation_when_off():
    # 합계 95 (≠100) → violation 기록되고 risk.passed False
    alloc_rows = [
        {"kind": "cash", "ref": None, "weight_pct": 40.0},
        {"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": 55.0},
    ]
    _seed_decision(12, alloc_rows)
    res = decision_mod.compute(12)
    assert res.get("ok"), res
    assert res["alloc_sum_pct"] == 95.0, res
    codes = [v["limit"] for v in res["risk"]["violations"]]
    assert "alloc_sum_100" in codes, res["risk"]["violations"]
    assert res["risk"]["passed"] is False, res


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} EXPOSURE TESTS PASSED")
