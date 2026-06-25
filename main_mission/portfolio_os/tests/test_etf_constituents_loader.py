"""Track E — ETF 구성 적재 커넥터(etf_constituents_loader) 테스트.

검증(불변 안전):
  - 구성 적재 + 멱등(UNIQUE etf_ticker,constituent_ticker,as_of) · replace 로 유령행 0.
  - 빈/가짜 행 거부(식별자 없는 행 skip) · 비중/섹터/국가 정규화.
  - **구성 적재 → etf_analysis(analyze_etf/overlap) 자동 동작**(핵심 회귀).
  - 입력 0건이면 data_connected=False(가짜 구성 0).
  - 파일(.json/.csv) 적재 · Anthropic 미사용.
"""
from __future__ import annotations

import json
import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_etfload.sqlite3")
# WAL/SHM 사이드카까지 정리 — 미삭제 시 새 연결이 stale WAL 로 readonly/IO 오류(테스트 격리).
for _sfx in ("", "-wal", "-shm", "-journal"):
    if os.path.exists(_TMP + _sfx):
        os.remove(_TMP + _sfx)
os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import etf_constituents_loader as loader
from main_mission.portfolio_os import etf_analysis


def setup():
    os.environ["SQLITE_PATH"] = _TMP
    store_db.init()


def setup_function(_fn=None):
    os.environ["SQLITE_PATH"] = _TMP
    conn = store_db.connect()
    try:
        conn.execute("DELETE FROM etf_constituents")
        conn.commit()
    finally:
        conn.close()


_ROWS = [
    {"ticker": "NVDA", "name": "NVIDIA", "weight_pct": 8.1, "sector": "Semiconductors",
     "country": "US"},
    {"ticker": "TSM", "name": "TSMC", "weight_pct": 6.0, "sector": "Semiconductors",
     "country": "TW"},
    {"ticker": "AVGO", "name": "Broadcom", "weight_pct": 5.5, "sector": "Semiconductors",
     "country": "US"},
]


# ---- 1) 적재 + 멱등 + replace ----
def test_load_and_idempotent():
    out = loader.load_constituents("SOXX", _ROWS, as_of="2026-06-01")
    assert out["written"] == 3 and out["data_connected"] is True
    # replace=True 재적재 → 행 수 동일(유령행 0).
    loader.load_constituents("SOXX", _ROWS, as_of="2026-06-01")
    conn = store_db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM etf_constituents WHERE etf_ticker='SOXX' "
                         "AND as_of='2026-06-01'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 3


def test_empty_rows_not_connected():
    out = loader.load_constituents("EMPTY", [])
    assert out["written"] == 0 and out["data_connected"] is False


def test_skips_garbage_rows():
    rows = _ROWS + [{"weight_pct": 1.0}]   # 식별자 없는 행 → skip
    out = loader.load_constituents("SOXX", rows, as_of="2026-06-01")
    assert out["written"] == 3


def test_dedupes_within_as_of():
    rows = _ROWS + [{"ticker": "NVDA", "name": "NVIDIA dup", "weight_pct": 9.9}]
    out = loader.load_constituents("SOXX", rows, as_of="2026-06-01")
    assert out["written"] == 3   # NVDA 중복 첫 행만


def test_weight_string_normalization():
    rows = [{"ticker": "AAA", "weight": "12.5%"}, {"Symbol": "BBB", "Weight (%)": "3,000"}]
    out = loader.load_constituents("XX", rows, as_of="2026-06-01")
    assert out["written"] == 2
    conn = store_db.connect()
    try:
        w = conn.execute("SELECT weight_pct FROM etf_constituents WHERE constituent_ticker='AAA'").fetchone()["weight_pct"]
    finally:
        conn.close()
    assert w == 12.5


# ---- 2) 핵심 회귀: 적재 → etf_analysis 자동 동작 ----
def test_analyze_etf_active_after_load():
    # 적재 전: 미연동.
    assert etf_analysis.analyze_etf("SOXX")["data_connected"] is False
    loader.load_constituents("SOXX", _ROWS, as_of="2026-06-01")
    a = etf_analysis.analyze_etf("SOXX")
    assert a["data_connected"] is True
    assert a["constituent_count"] == 3
    # 섹터 노출 자동 집계.
    sectors = {s["sector"]: s["weight_pct"] for s in a["sector_exposure"]}
    assert sectors["Semiconductors"] == 19.6


def test_overlap_active_after_load():
    loader.load_constituents("SOXX", _ROWS, as_of="2026-06-01")
    loader.load_constituents("SMH", [
        {"ticker": "NVDA", "name": "NVIDIA", "weight_pct": 10.0},
        {"ticker": "TSM", "name": "TSMC", "weight_pct": 8.0},
        {"ticker": "ASML", "name": "ASML", "weight_pct": 5.0},
    ], as_of="2026-06-01")
    ov = etf_analysis.overlap("SOXX", "SMH")
    assert ov["data_connected"] is True
    shared = {s["ticker"] for s in ov["shared"]}
    assert shared == {"NVDA", "TSM"}   # 공통 종목 자동 검출


# ---- 3) 파일 적재 ----
def test_load_from_json_file():
    p = os.path.join(tempfile.gettempdir(), "soxx_holdings.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(_ROWS, f)
    out = loader.load_from_file("SOXX", p, as_of="2026-06-01")
    assert out["written"] == 3
    os.remove(p)


def test_load_from_csv_file():
    p = os.path.join(tempfile.gettempdir(), "soxx_holdings.csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write("ticker,name,weight_pct,sector,country\n")
        f.write("NVDA,NVIDIA,8.1,Semiconductors,US\n")
        f.write("TSM,TSMC,6.0,Semiconductors,TW\n")
    out = loader.load_from_file("SOXX", p, as_of="2026-06-01")
    assert out["written"] == 2
    os.remove(p)


def test_status_honest():
    st = loader.status()
    assert st["data_connected"] is False   # 빈 상태
    loader.load_constituents("SOXX", _ROWS, as_of="2026-06-01")
    st2 = loader.status()
    assert st2["data_connected"] is True and st2["etf_analysis_active"] is True


def test_no_anthropic_import():
    import pathlib
    src = pathlib.Path(loader.__file__).read_text(encoding="utf-8").lower()
    assert "import anthropic" not in src
    assert "anthropic_api_key" not in src
