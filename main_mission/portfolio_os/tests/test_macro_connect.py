"""Track B — 거시/시장 데이터 연결(macro_connect) 테스트.

검증(불변 안전):
  - 키 없을 때 **안전 실패**(MacroConfigError) — 가짜 데이터/성공 0.
  - macro_indicators 멱등 적재(PK indicator,obs_date) — 재실행 중복 없음.
  - freshness/stale: obs_date decay → 오래된 지표 stale 판정·snapshot 분리.
  - 데이터 없으면 macro_snapshot.data_available=False, macro_to_portfolio.connected=False(정직).
  - 거시→포트폴리오 매핑: 금리↑→현금/단기채↑·위험↓, 달러강세→미국ETF↑, 역전→헤지, VIX→헤지.
  - stale 지표는 매핑 신호에서 제외(가짜 신호 금지).
  - decline/axes/macro: 신선 지표 실 점수 / stale 제외 / 미연동 data_available=False.
  - perspective_variants·portfolio_impact 에 거시 반영(데이터 없으면 '거시 미연동' 정직).
  - 자동주문/policy 자동변경 0 · Anthropic API 미사용 · HTTP fetcher 는 monkeypatch(네트워크 0).
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_macro.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_PATH"] = _TMP

import pytest

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import macro_connect as mc
from main_mission.portfolio_os.decline.axes import macro as macro_axis
from main_mission.portfolio_os.decline import context as ctx_mod


def setup():
    os.environ["SQLITE_PATH"] = _TMP
    store_db.init()


def setup_function(_fn=None):
    # 파일 삭제 대신 테이블만 비운다(연결 누수→readonly/IO 오류 회피, test_decline_axes 패턴).
    os.environ["SQLITE_PATH"] = _TMP
    conn = store_db.connect()
    try:
        conn.execute("DELETE FROM macro_indicators")
        conn.commit()
    finally:
        conn.close()


def _iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


# ============================================================
# 1) 키 없으면 안전 실패 (가짜 성공 금지)
# ============================================================
def test_fred_key_missing_raises(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(mc, "_load_env", lambda: None)
    with pytest.raises(mc.MacroConfigError):
        mc.fred_api_key()


def test_ecos_key_missing_raises(monkeypatch):
    monkeypatch.delenv("ECOS_API_KEY", raising=False)
    monkeypatch.setattr(mc, "_load_env", lambda: None)
    with pytest.raises(mc.MacroConfigError):
        mc.ecos_api_key()


def test_load_all_not_connected_when_no_keys(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("ECOS_API_KEY", raising=False)
    monkeypatch.setattr(mc, "_load_env", lambda: None)
    out = mc.load_all()
    assert out["any_loaded"] is False
    assert len(out["not_connected"]) == 2          # fred + ecos 둘 다 미연동
    assert out["fred"] is None and out["ecos"] is None


# ============================================================
# 2) 멱등 적재 + freshness/stale
# ============================================================
def test_upsert_idempotent():
    assert mc.upsert_indicator("policy_rate", _iso(2), 3.5, "test") is True
    assert mc.upsert_indicator("policy_rate", _iso(2), 3.5, "test") is True  # 재실행 OK
    conn = store_db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM macro_indicators "
                         "WHERE indicator='policy_rate'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1                                  # PK 충돌 → 1행만(중복 없음)


def test_upsert_skips_bad_value():
    assert mc.upsert_indicator("policy_rate", "garbage", 3.5, "test") is False
    assert mc.upsert_indicator("policy_rate", _iso(1), None, "test") is False


def test_freshness_stale_by_obs_date():
    fresh = mc.freshness(_iso(1), indicator="yield_10y")    # 일간 임계 작음
    stale = mc.freshness(_iso(400), indicator="policy_rate")
    assert fresh["stale"] is False and fresh["age_days"] == 1
    assert stale["stale"] is True and stale["age_days"] == 400
    # obs_date 없으면 stale=True(가짜 신선 금지)
    assert mc.freshness(None, indicator="vix")["stale"] is True


def test_ecos_obs_date_formats():
    assert mc._iso_obs_date("20250115") == "2025-01-15"   # D
    assert mc._iso_obs_date("202501") == "2025-01-01"     # M
    assert mc._iso_obs_date("2025") == "2025-01-01"       # A
    assert mc._iso_obs_date("2025-01-15") == "2025-01-15"  # FRED


# ============================================================
# 3) snapshot — 데이터 없음/있음
# ============================================================
def test_snapshot_empty_not_connected():
    snap = mc.macro_snapshot()
    assert snap["data_available"] is False
    assert snap["indicators"] == {}
    assert "미연동" in snap["note"]


def test_snapshot_fresh_and_stale_split():
    mc.upsert_indicator("policy_rate", _iso(5), 3.5, "test")     # fresh
    mc.upsert_indicator("yield_10y", _iso(2), 3.2, "test")       # fresh
    mc.upsert_indicator("cpi_yoy", _iso(400), 5.0, "test")       # stale(오래됨)
    snap = mc.macro_snapshot()
    assert snap["data_available"] is True
    assert snap["indicators"]["policy_rate"]["stale"] is False
    assert snap["indicators"]["cpi_yoy"]["stale"] is True
    assert snap["fresh_count"] == 2 and snap["stale_count"] == 1


# ============================================================
# 4) 거시 → 포트폴리오 매핑 (판단 신호)
# ============================================================
def test_map_not_connected_when_empty():
    out = mc.macro_to_portfolio()
    assert out["connected"] is False
    assert out["signals"] == []
    assert "미연동" in out["note"]


def test_map_high_rate_defensive():
    mc.upsert_indicator("policy_rate", _iso(5), 3.5, "test")
    out = mc.macro_to_portfolio()
    assert out["connected"] is True
    assert out["tilts"]["cash_band"] >= 1.0
    assert out["tilts"]["risk_assets"] <= -1.0
    assert out["tilts"]["short_bond"] >= 1.0
    assert any("기준금리" in s["detail"] for s in out["signals"])


def test_map_curve_inversion_hedge():
    mc.upsert_indicator("yield_10y", _iso(1), 3.0, "test")
    mc.upsert_indicator("yield_2y", _iso(1), 3.5, "test")   # 역전(10y<2y)
    out = mc.macro_to_portfolio()
    assert out["tilts"]["hedge"] >= 1.0
    assert any("역전" in s["detail"] for s in out["signals"])


def test_map_usd_strong_us_etf():
    mc.upsert_indicator("fx_usdkrw", _iso(1), 1400.0, "test")
    out = mc.macro_to_portfolio()
    assert out["tilts"]["us_etf"] >= 1.0
    assert out["tilts"]["usd_exposure"] >= 1.0
    assert any("달러 강세" in s["detail"] for s in out["signals"])


def test_map_vix_fear_hedge():
    mc.upsert_indicator("vix", _iso(1), 30.0, "test")
    out = mc.macro_to_portfolio()
    assert out["tilts"]["hedge"] >= 1.0
    assert any("VIX" in s["detail"] for s in out["signals"])


def test_map_excludes_stale_indicator():
    # stale 한 고금리 지표는 신호에서 제외돼야 함(가짜 신호 금지).
    mc.upsert_indicator("policy_rate", _iso(400), 5.0, "test")  # stale
    out = mc.macro_to_portfolio()
    assert out["connected"] is True
    assert all("기준금리" not in s["detail"] for s in out["signals"])
    assert out["tilts"]["cash_band"] == 0.0


def test_map_no_auto_apply():
    mc.upsert_indicator("policy_rate", _iso(5), 3.5, "test")
    out = mc.macro_to_portfolio()
    assert out["auto_applied"] is False
    assert out["auto_order_created"] is False
    assert out["requires_user_approval"] is True


# ============================================================
# 5) decline/axes/macro — 실데이터 / stale / 미연동
# ============================================================
def test_axis_macro_no_data_available():
    res = macro_axis.score({"macro_indicators": None})
    assert res["data_available"] is False
    assert res["risk_0_100"] == 0.0


def test_axis_macro_real_score_from_context():
    # context.build_context 가 macro_indicators 를 DB 에서 신선 지표로만 채운다.
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), 0.75, "test")  # 인상기
    conn = store_db.connect()
    try:
        ctx = ctx_mod.build_context("TEST", history=[], conn=conn)
    finally:
        conn.close()
    res = macro_axis.score(ctx)
    assert res["data_available"] is True
    assert res["risk_0_100"] > 0.0
    assert any(s["name"] == "rate_hiking" and s["fired"] for s in res["signals"])


def test_context_drops_stale_macro():
    mc.upsert_indicator("policy_rate", _iso(400), 5.0, "test")   # stale → 제외
    conn = store_db.connect()
    try:
        ctx = ctx_mod.build_context("TEST", history=[], conn=conn)
    finally:
        conn.close()
    # stale 한 단일 지표만 있으면 macro context 가 비어 data_available=False(정직).
    assert not ctx["macro_indicators"]


# ============================================================
# 6) FRED/ECOS fetcher 파싱 (monkeypatch — 네트워크 0)
# ============================================================
def test_fetch_fred_parses_and_skips_missing(monkeypatch):
    payload = {"observations": [
        {"date": "2025-06-01", "value": "5.25"},
        {"date": "2025-05-01", "value": "."},      # 결측 → 제외(가짜 0 금지)
        {"date": "2025-04-01", "value": "5.00"}]}
    monkeypatch.setattr(mc, "_http_get_json", lambda url, timeout=15.0: payload)
    obs = mc.fetch_fred_series("DGS10", api_key="x")
    assert obs == [("2025-06-01", 5.25), ("2025-04-01", 5.0)]


def test_fetch_ecos_parses(monkeypatch):
    payload = {"StatisticSearch": {"row": [
        {"TIME": "20250601", "DATA_VALUE": "3.50"},
        {"TIME": "20250501", "DATA_VALUE": "-"}]}}   # 결측 → 제외
    monkeypatch.setattr(mc, "_http_get_json", lambda url, timeout=15.0: payload)
    obs = mc.fetch_ecos_series("policy_rate", api_key="x")
    assert obs == [("20250601", 3.5)]


def test_fetch_ecos_error_response_raises(monkeypatch):
    payload = {"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}}
    monkeypatch.setattr(mc, "_http_get_json", lambda url, timeout=15.0: payload)
    with pytest.raises(mc.MacroConfigError):
        mc.fetch_ecos_series("policy_rate", api_key="x")


def test_load_fred_idempotent_with_monkeypatch(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "x")
    monkeypatch.setattr(mc, "_load_env", lambda: None)
    monkeypatch.setattr(mc, "fetch_fred_series",
                        lambda sid, limit=12, api_key=None: [("2025-06-01", 1.0)])
    r1 = mc.load_fred()
    r2 = mc.load_fred()
    assert r1["total"] > 0 and r2["total"] > 0
    conn = store_db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM macro_indicators WHERE source='fred' "
                         "AND obs_date='2025-06-01'").fetchone()["c"]
    finally:
        conn.close()
    assert n == len(mc.FRED_SERIES)                # 멱등 — 재실행해도 series 수만큼만


# ============================================================
# 7) 통합: 매핑이 실제 endpoint 상수를 가리키는지(확인됨) + 안티-anthropic
# ============================================================
def test_endpoints_are_official():
    assert mc.FRED_BASE == "https://api.stlouisfed.org/fred/series/observations"
    assert mc.ECOS_BASE == "https://ecos.bok.or.kr/api/StatisticSearch"


def test_no_anthropic_import():
    import pathlib
    src = pathlib.Path(mc.__file__).read_text(encoding="utf-8").lower()
    assert "import anthropic" not in src
    assert "anthropic_api_key" not in src


# ============================================================
# 8) CPI 단위 — 지수값을 전년比(YoY)로 오표기 금지 (사용자 신뢰)
# ============================================================
def _seed_cpi_index(indicator, latest, year_ago, n=14):
    """월간 CPI '지수' 시계열 적재(최신=latest, 12개월 전=year_ago)."""
    obs = []
    for i in range(n):
        y, m = 2026, 6 - i
        while m <= 0:
            m += 12; y -= 1
        val = latest if i == 0 else (year_ago if i == 12 else latest)
        obs.append((f"{y:04d}-{m:02d}-01", float(val)))
    mc.upsert_series(indicator, obs, "test")


def test_cpi_index_not_rendered_as_yoy():
    # 지수값 119.9 가 '전년比 119.9%' 로 표기되면 안 된다.
    _seed_cpi_index("cpi_yoy", 119.9, 117.0)        # 한국 CPI 지수(YoY ≈ 2.5%)
    m = mc.macro_to_portfolio()
    for s in m.get("signals", []):
        assert "119.9" not in s["detail"], f"지수값이 YoY로 오표기됨: {s['detail']}"
        assert "전년比 119" not in s["detail"]


def test_cpi_yoy_computed_from_index():
    # 지수 124.0(최신) vs 119.0(12개월 전) → YoY ≈ 4.2%.
    _seed_cpi_index("cpi_index_us", 124.0, 119.0)
    yoy = mc.cpi_yoy_from_index("cpi_index_us")
    assert yoy is not None and 4.0 <= yoy <= 4.5, yoy


def test_cpi_yoy_none_when_insufficient_history():
    # 13개월 미만이면 미계산(None) — false YoY 금지.
    mc.upsert_series("cpi_index_us", [("2026-06-01", 124.0), ("2026-05-01", 123.5)], "test")
    assert mc.cpi_yoy_from_index("cpi_index_us") is None
