"""ETF 분석 테스트 (Track E) — 구성·상위비중·섹터/국가 노출·보유 ETF 간 겹침·
개별종목→ETF 영향. 데이터 없으면 정직하게 '미연동'(가짜 0). Anthropic 미사용.

임시 SQLite 핀 + 직접 etf_constituents 적재(수동 입력 경로).
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_etf_analysis.sqlite3")

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import etf_analysis as ea


def setup():
    os.environ["SQLITE_PATH"] = _TMP
    # WAL/SHM 사이드카까지 정리 — 미삭제 시 새 연결이 stale WAL 로 readonly 오류 발생(테스트 격리).
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = _TMP + suffix
        if os.path.exists(p):
            os.remove(p)
    store_db.init()


def _add_constituent(conn, etf, ticker, name, w, sector=None, country=None, as_of="2026-06-20"):
    conn.execute(
        "INSERT INTO etf_constituents(etf_ticker, constituent_ticker, constituent_name, "
        "weight_pct, sector, country, as_of, source) VALUES(?,?,?,?,?,?,?,?)",
        (etf, ticker, name, w, sector, country, as_of, "manual"))


def _seed_two_overlapping_etfs(conn):
    # 반도체 ETF: 삼성/하이닉스/엔비디아
    _add_constituent(conn, "SEMI", "005930", "삼성전자", 25.0, "IT", "KR")
    _add_constituent(conn, "SEMI", "000660", "SK하이닉스", 20.0, "IT", "KR")
    _add_constituent(conn, "SEMI", "NVDA", "NVIDIA", 15.0, "IT", "US")
    # AI ETF: 엔비디아/삼성/MS  → 엔비디아·삼성이 양쪽 겹침
    _add_constituent(conn, "AI", "NVDA", "NVIDIA", 30.0, "IT", "US")
    _add_constituent(conn, "AI", "005930", "삼성전자", 10.0, "IT", "KR")
    _add_constituent(conn, "AI", "MSFT", "Microsoft", 12.0, "IT", "US")
    conn.commit()


# --------------------------------------------------------------------------- single ETF
def test_analyze_etf_returns_exposure_and_top():
    setup()
    conn = store_db.connect()
    try:
        _seed_two_overlapping_etfs(conn)
    finally:
        conn.close()
    out = ea.analyze_etf("SEMI")
    assert out["data_connected"] is True
    assert out["constituent_count"] == 3
    assert out["top_holdings"][0]["ticker"] == "005930"  # 비중순
    secs = {s["sector"]: s["weight_pct"] for s in out["sector_exposure"]}
    assert secs["IT"] == 60.0
    countries = {c["country"]: c["weight_pct"] for c in out["country_exposure"]}
    assert countries["KR"] == 45.0 and countries["US"] == 15.0


def test_analyze_etf_honest_when_no_data():
    setup()
    out = ea.analyze_etf("NOPE")
    assert out["data_connected"] is False
    assert out["constituent_count"] == 0
    assert "미연동" in out["note"]


# --------------------------------------------------------------------------- overlap
def test_overlap_detects_shared_holdings():
    setup()
    conn = store_db.connect()
    try:
        _seed_two_overlapping_etfs(conn)
    finally:
        conn.close()
    out = ea.overlap("SEMI", "AI")
    assert out["data_connected"] is True
    shared = {s["ticker"] for s in out["shared"]}
    assert shared == {"NVDA", "005930"}
    # min_overlap: NVDA min(15,30)=15, 삼성 min(25,10)=10 → 합 25
    assert out["overlap_weight_pct"] == 25.0
    assert out["concentration_flag"] is True  # 20%+


def test_overlap_honest_when_one_side_missing():
    setup()
    conn = store_db.connect()
    try:
        _seed_two_overlapping_etfs(conn)
    finally:
        conn.close()
    out = ea.overlap("SEMI", "NOPE")
    assert out["data_connected"] is False
    assert out["shared_count"] == 0


# --------------------------------------------------------------------------- account
def _seed_account_with_etfs(conn, account_index=1):
    cur = conn.execute(
        "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, source) "
        "VALUES(?,?,?,?,?)", (account_index, 1000.0, 5000.0, 2, "manual_sync"))
    sid = cur.lastrowid
    conn.execute("INSERT INTO holdings(snapshot_id, account_index, ticker, name, qty, market_value) "
                 "VALUES(?,?,?,?,?,?)", (sid, account_index, "SEMI", "반도체ETF", 10, 2000.0))
    conn.execute(
        "INSERT INTO universe_instruments(account_index, ticker, market, name, asset_class, is_active) "
        "VALUES(?,?,?,?,?,1)", (account_index, "AI", "KRX", "AI ETF", "etf"))
    conn.commit()


def test_analyze_account_etfs_finds_overlap():
    setup()
    conn = store_db.connect()
    try:
        _seed_two_overlapping_etfs(conn)
        _seed_account_with_etfs(conn, 1)
    finally:
        conn.close()
    out = ea.analyze_account_etfs(1)
    assert out["data_connected"] is True
    assert set(out["etfs"]) == {"SEMI", "AI"}
    assert out["overlaps"]
    assert out["concentration_flags"]      # 겹침 25% → 집중 표기
    assert out["auto_order_created"] is False  # 주문 절대 금지


def test_analyze_account_etfs_honest_when_no_data():
    setup()
    conn = store_db.connect()
    try:
        _seed_account_with_etfs(conn, 1)   # etf_constituents 없음
    finally:
        conn.close()
    out = ea.analyze_account_etfs(1)
    assert out["data_connected"] is False
    assert "미연동" in out["note"]


# --------------------------------------------------------------------------- stock → ETF impact
def test_stock_impact_on_etfs():
    setup()
    conn = store_db.connect()
    try:
        _seed_two_overlapping_etfs(conn)
    finally:
        conn.close()
    out = ea.stock_impact_on_etfs("NVDA", ["SEMI", "AI"])
    assert out["data_connected"] is True
    assert out["etf_count_holding"] == 2          # 양쪽 ETF 에 포함
    assert out["sum_weight_in_etfs_pct"] == 45.0  # 15 + 30
    assert len(out["in_etfs"]) == 2


def test_stock_impact_partial_membership():
    setup()
    conn = store_db.connect()
    try:
        _seed_two_overlapping_etfs(conn)
    finally:
        conn.close()
    out = ea.stock_impact_on_etfs("MSFT", ["SEMI", "AI"])
    assert out["etf_count_holding"] == 1          # AI 에만
    assert out["sum_weight_in_etfs_pct"] == 12.0


def test_stock_impact_honest_when_no_data():
    setup()
    out = ea.stock_impact_on_etfs("NVDA", ["NOPE1", "NOPE2"])
    assert out["data_connected"] is False
    assert out["sum_weight_in_etfs_pct"] == 0.0
    assert "미연동" in out["note"]


# --------------------------------------------------------------------------- 후보↔보유 중복노출
def test_candidate_overlap_combined_indirect_exposure():
    setup()
    conn = store_db.connect()
    try:
        # 후보 CAND: NVDA 10%, AMD 5%, MSFT 8%
        _add_constituent(conn, "CAND", "NVDA", "NVIDIA", 10.0, "IT", "US")
        _add_constituent(conn, "CAND", "AMD", "AMD", 5.0, "IT", "US")
        _add_constituent(conn, "CAND", "MSFT", "Microsoft", 8.0, "IT", "US")
        # 보유 SEMI: NVDA 12%, AMD 6%  / 보유 AI: NVDA 20%
        _add_constituent(conn, "SEMI", "NVDA", "NVIDIA", 12.0, "IT", "US")
        _add_constituent(conn, "SEMI", "AMD", "AMD", 6.0, "IT", "US")
        _add_constituent(conn, "AI", "NVDA", "NVIDIA", 20.0, "IT", "US")
        conn.commit()
    finally:
        conn.close()
    out = ea.candidate_overlap_with_holdings("CAND", ["SEMI", "AI"])
    assert out["data_connected"] is True
    shared = {s["ticker"] for s in out["shared"]}
    assert shared == {"NVDA", "AMD"}  # MSFT 는 보유에 없음
    nvda = next(s for s in out["shared"] if s["ticker"] == "NVDA")
    # NVDA 합산 보유비중 = 12 + 20 = 32, 후보 비중 10
    assert nvda["weight_in_held_etfs_sum"] == 32.0
    assert set(nvda["held_in"]) == {"SEMI", "AI"}
    # 합산 간접노출 = 겹친 후보 구성종목의 후보 내 비중 합 = NVDA 10 + AMD 5 = 15
    assert out["combined_indirect_exposure_pct"] == 15.0
    assert out["concentration_flag"] is False  # <20


def test_candidate_overlap_concentration_flag_when_high():
    setup()
    conn = store_db.connect()
    try:
        _add_constituent(conn, "CAND", "NVDA", "NVIDIA", 15.0, "IT", "US")
        _add_constituent(conn, "CAND", "TSM", "TSMC", 12.0, "IT", "TW")
        _add_constituent(conn, "HELD", "NVDA", "NVIDIA", 10.0, "IT", "US")
        _add_constituent(conn, "HELD", "TSM", "TSMC", 8.0, "IT", "TW")
        conn.commit()
    finally:
        conn.close()
    out = ea.candidate_overlap_with_holdings("CAND", ["HELD"])
    # 후보 겹침분 = 15 + 12 = 27 → 20%+
    assert out["combined_indirect_exposure_pct"] == 27.0
    assert out["concentration_flag"] is True


def test_candidate_overlap_honest_when_no_data():
    setup()
    out = ea.candidate_overlap_with_holdings("NOPE", ["NOPE2"])
    assert out["data_connected"] is False
    assert out["combined_indirect_exposure_pct"] == 0.0
    assert out["concentration_flag"] is False
    assert "미연동" in out["note"]


def test_candidate_overlap_excludes_self():
    setup()
    conn = store_db.connect()
    try:
        _add_constituent(conn, "CAND", "NVDA", "NVIDIA", 15.0, "IT", "US")
        _add_constituent(conn, "HELD", "NVDA", "NVIDIA", 10.0, "IT", "US")
        conn.commit()
    finally:
        conn.close()
    # 후보가 보유 목록에 포함돼도 자기 자신과는 겹침 계산 안 함.
    out = ea.candidate_overlap_with_holdings("CAND", ["CAND", "HELD"])
    assert out["holding_etfs"] == ["HELD"]
    assert {s["ticker"] for s in out["shared"]} == {"NVDA"}


def test_stock_impact_on_account():
    setup()
    conn = store_db.connect()
    try:
        _seed_two_overlapping_etfs(conn)
        _seed_account_with_etfs(conn, 1)
    finally:
        conn.close()
    out = ea.stock_impact_on_account("NVDA", 1)
    assert out["account_index"] == 1
    assert set(out["account_etfs"]) == {"SEMI", "AI"}
    assert out["etf_count_holding"] == 2
