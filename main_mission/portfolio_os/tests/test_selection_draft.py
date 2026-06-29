"""selection_draft — 세부 선정 위저드 draft 저장/복원(계좌별, draft only).

회귀 목적(고정): "초안 승인 했는데 저장이 안 됨" — 선정 상태가 휘발성이라 새로고침 시 소실.
근본 수정 = 백엔드 DB 영속. 본 테스트가 그 영속/복원/격리/멱등을 고정한다.
키 없이 임시 SQLite(conftest per-test 격리). Anthropic API 미사용.
"""
from __future__ import annotations

import base64
import json

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import selection_draft as sd


def setup_function():
    store_db.init()


def test_empty_load_returns_none():
    assert sd.load(1) is None  # 저장 전엔 draft 없음


def test_save_then_load_roundtrip_restores_state():
    sd.save(7, picks=[
        {"bucket": "semiconductor", "ticker": "005930", "name": "삼성전자", "asset_class": "stock"},
        {"bucket": "global_core", "ticker": "SPY"},
    ], equity_option="5", acknowledged=True, proposal_id="prop_1")
    d = sd.load(7)
    assert d is not None
    assert d["equity_option"] == "5"
    assert d["acknowledged"] is True
    assert d["acknowledged_at"]  # 승인 표시 시각 기록됨
    assert d["proposal_id"] == "prop_1"
    tickers = {p["ticker"] for p in d["picks"]}
    assert tickers == {"005930", "SPY"}


def test_picks_deduped_and_sanitized():
    # 같은 bucket:ticker 중복 → 1건. bucket/ticker 없는 항목 → 제외.
    sd.save(7, picks=[
        {"bucket": "semiconductor", "ticker": "005930", "name": "삼성"},
        {"bucket": "semiconductor", "ticker": "005930", "name": "중복"},
        {"bucket": "", "ticker": "X"},          # bucket 없음 → 제외
        {"ticker": "Y"},                          # bucket 없음 → 제외
        {"bucket": "global_core"},                # ticker 없음 → 제외
    ])
    d = sd.load(7)
    assert len(d["picks"]) == 1
    assert d["picks"][0]["ticker"] == "005930"


def test_upsert_overwrites_single_row_per_account():
    sd.save(7, picks=[{"bucket": "global_core", "ticker": "SPY"}], equity_option="10", acknowledged=True)
    sd.save(7, picks=[{"bucket": "robotics", "ticker": "BOTZ"}], equity_option="none", acknowledged=False)
    d = sd.load(7)
    assert [p["ticker"] for p in d["picks"]] == ["BOTZ"]
    assert d["equity_option"] == "none"
    assert d["acknowledged"] is False
    # 계좌당 1건만(이력 아님) — 직접 카운트로 확인.
    conn = store_db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM selection_drafts WHERE account_index=7").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_acknowledged_at_cleared_when_unacknowledged():
    sd.save(7, picks=[{"bucket": "global_core", "ticker": "SPY"}], acknowledged=True)
    assert sd.load(7)["acknowledged_at"] is not None
    sd.save(7, picks=[{"bucket": "global_core", "ticker": "SPY"}], acknowledged=False)
    assert sd.load(7)["acknowledged_at"] is None


def test_acknowledged_at_preserved_while_still_acknowledged():
    sd.save(7, picks=[{"bucket": "global_core", "ticker": "SPY"}], acknowledged=True)
    first = sd.load(7)["acknowledged_at"]
    sd.save(7, picks=[{"bucket": "robotics", "ticker": "BOTZ"}], acknowledged=True)  # 계속 승인
    assert sd.load(7)["acknowledged_at"] == first  # 최초 승인 시각 보존


def test_account_isolation_no_cross_leak():
    sd.save(1, picks=[{"bucket": "global_core", "ticker": "SPY"}])
    sd.save(2, picks=[{"bucket": "robotics", "ticker": "BOTZ"}])
    assert [p["ticker"] for p in sd.load(1)["picks"]] == ["SPY"]
    assert [p["ticker"] for p in sd.load(2)["picks"]] == ["BOTZ"]


def test_invalid_equity_option_falls_back_to_none():
    sd.save(7, picks=[], equity_option="99")  # 허용값 아님 → none
    assert sd.load(7)["equity_option"] == "none"


def test_cli_save_load_via_base64_payload(capsys):
    payload = {"picks": [{"bucket": "global_core", "ticker": "SPY"}],
               "equity_option": "5", "acknowledged": True}
    b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    rc = sd.main(["--account", "9", "--save", "--payload-b64", b64])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is True and out["acknowledged"] is True

    rc = sd.main(["--account", "9", "--load"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["draft"]["picks"][0]["ticker"] == "SPY"
