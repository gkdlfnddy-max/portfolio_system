"""국채 ETF 실 지표(가격/거래량) 연동 + 후보 비교 강화.

검증 축:
  - KR 5종 KIS 일봉 가격/거래량 **실적재**(fake fetcher 주입) → etf_profile 가격 채워짐.
  - 미국 3종·보수율·듀레이션·수익률 **미연동 → unknown 정직**(가짜 0).
  - 비교표: 역할/장점/리스크 + 거시 적합성 + 계좌 목적 적합성 + 추천강도 + 데이터품질 + 대안 + 제외.
  - 장기채 변동성 경고 동봉. 자동주문/policy 0.
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_govbondetf.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from datetime import date, timedelta

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import govbond_etf, price_history, profile as profile_mod


def setup():
    store_db.init()


def _mk_account(idx: int):
    conn = store_db.connect()
    try:
        conn.execute("INSERT OR IGNORE INTO accounts(account_index, mode, alias) VALUES(?,?,?)",
                     (idx, "mock", f"acct{idx}"))
        conn.commit()
    finally:
        conn.close()


_UNIVERSE_CODES = ["153130", "114260", "471230", "439870", "451530",
                   "SHY", "IEF", "TLT"]


def _clear_prices():
    """price_history 의 universe 코드 행 제거 — 테스트 순서 독립성(공유 DB 격리)."""
    store_db.init()
    conn = store_db.connect()
    try:
        for code in _UNIVERSE_CODES:
            conn.execute("DELETE FROM price_history WHERE instrument_code=?", (code,))
        conn.commit()
    finally:
        conn.close()


class _FakeFetcher:
    """KR 일봉 fetcher 대역 — fetch_and_store 가 price_history 에 실 upsert(read-only)."""

    def __init__(self, n: int = 40):
        self.n = n
        self.calls: list[str] = []

    def fetch_and_store(self, code, *, count=200):
        self.calls.append(code)
        d0 = date.today() - timedelta(days=self.n - 1)
        bars = []
        for i in range(self.n):
            c = 100.0 + 0.1 * i + (0.5 if i % 2 else -0.5)  # 약한 변동
            bars.append({"trade_date": (d0 + timedelta(days=i)).isoformat(),
                         "open": c, "high": c * 1.005, "low": c * 0.995,
                         "close": round(c, 2), "volume": 5000.0 + i})
        return price_history.upsert_bars(code, bars, source="kis_daily")


# ---- universe / profile 정직성 ---------------------------------------------
def test_universe_government_only_8_candidates():
    assert len(govbond_etf._UNIVERSE) == 8
    tickers = {e["ticker"] for e in govbond_etf._UNIVERSE}
    assert {"SHY", "IEF", "TLT"} <= tickers
    assert {"153130", "114260", "471230", "439870", "451530"} <= tickers


def test_us_etf_unknown_no_fake_metrics():
    p = govbond_etf.etf_profile("TLT")
    assert p["region"] == "미국"
    assert p["price"] == "unknown" and p["volume"] == "unknown"
    assert p["recent_volatility"] == "unknown"
    assert p["data_available"] is False
    # 정성 사실은 있어야 함(추적지수/환노출)
    assert p["tracking_index"] and p["hedged_or_unhedged"]


def test_metrics_always_unknown_for_expense_duration_yield():
    for tk in ("SHY", "153130", "439870"):
        p = govbond_etf.etf_profile(tk)
        assert p["expense_ratio"] == "unknown", p
        assert p["duration_years"] == "unknown", p
        assert p["yield"] == "unknown", p


def test_kr_etf_before_fetch_is_unknown():
    # 적재 전이면 KR 도 가격 unknown(가짜 0)
    _clear_prices()
    p = govbond_etf.etf_profile("114260")
    assert p["price"] == "unknown" and p["data_available"] is False


# ---- 실 지표 연동 (KR 가격/거래량) -----------------------------------------
def test_fetch_metrics_loads_kr_prices_real():
    _clear_prices()
    fake = _FakeFetcher()
    res = govbond_etf.fetch_metrics(900, fetcher=fake)
    assert res["ok"] and res["kr_fetched_codes"] == 5, res
    assert res["auto_order_created"] is False
    # 미국 3종은 적재 skip(정직)
    assert len(res["us_skipped"]) == 3
    assert {s["ticker"] for s in res["us_skipped"]} == {"SHY", "IEF", "TLT"}
    # fetch 후 KR profile 에 실 가격/거래량/변동성 채워짐
    p = govbond_etf.etf_profile("153130")
    assert isinstance(p["price"], (int, float)) and p["price"] > 0, p
    assert isinstance(p["volume"], (int, float)), p
    assert isinstance(p["recent_volatility"], (int, float)), p
    assert p["data_available"] is True and p["last_verified_at"]
    assert p["confidence"] == "medium"


def test_recent_volatility_none_when_insufficient():
    assert govbond_etf._recent_volatility([100.0]) is None
    assert govbond_etf._recent_volatility([]) is None
    v = govbond_etf._recent_volatility([100, 101, 99, 102, 100])
    assert isinstance(v, float)


# ---- rate_regime (거시 미연동 정직 / 실데이터) ------------------------------
def test_rate_regime_unknown_when_macro_not_connected(monkeypatch):
    import main_mission.portfolio_os.macro_connect as mc
    monkeypatch.setattr(mc, "macro_to_portfolio", lambda *a, **k: {"connected": False})
    r = govbond_etf._rate_regime()
    assert r["regime"] == "unknown" and r["connected"] is False


def test_rate_regime_elevated_on_high_rate(monkeypatch):
    import main_mission.portfolio_os.macro_connect as mc
    monkeypatch.setattr(mc, "macro_to_portfolio", lambda *a, **k: {
        "connected": True, "lean": "defensive",
        "signals": [{"name": "high_rate_policy_rate"}],
        "tilts": {"bond_duration": -1.0}})
    r = govbond_etf._rate_regime()
    assert r["regime"] == "elevated" and r["connected"] is True


# ---- 비교표 ----------------------------------------------------------------
def _patch_elevated(monkeypatch):
    import main_mission.portfolio_os.macro_connect as mc
    monkeypatch.setattr(mc, "macro_to_portfolio", lambda *a, **k: {
        "connected": True, "lean": "defensive",
        "signals": [{"name": "high_rate_policy_rate_us"}],
        "tilts": {"bond_duration": -1.0}})


def test_compare_has_all_columns(monkeypatch):
    _patch_elevated(monkeypatch)
    _mk_account(901)
    profile_mod.save(901, {"bond_target_pct": 40, "bond_duration_pref": "short"})
    out = govbond_etf.compare_govbond_candidates(901)
    assert out["ok"] and out["candidates"], out
    for c in out["candidates"]:
        for col in ("role", "pros", "risks", "macro_fit", "purpose_fit",
                    "recommendation_strength", "data_quality", "alternatives",
                    "classification", "tracking_index", "hedged_or_unhedged"):
            assert col in c, (col, c)
        assert "label" in c["macro_fit"] and "label" in c["purpose_fit"]
    assert out["auto_order_created"] is False and out["policy_changed"] is False


def test_compare_emits_normalized_candidate_evaluations(monkeypatch):
    _patch_elevated(monkeypatch)
    _mk_account(921)
    out = govbond_etf.compare_govbond_candidates(921)
    norm = out["normalized"]
    assert len(norm) == len(out["candidates"]) + len(out["excluded"])
    for c in norm:
        assert c["candidate_type"] == "treasury"
        assert c["bucket"] == "treasury"
        # 안전 불변식
        assert c["approval_required"] is True
        assert c["auto_order_created"] is False and c["auto_applied"] is False
        # 비교 단계 — 가짜 비중 금지
        assert c["suggested_weight"] is None and c["max_weight"] is None
    # 후보(미제외)는 편입 사유, 제외분은 제외 사유
    included = [c for c in norm if c["reason_to_exclude"] == ""]
    assert included and all(c["reason_to_include"] for c in included)


def test_compare_macro_fit_short_adequate_in_elevated(monkeypatch):
    _patch_elevated(monkeypatch)
    _mk_account(902)
    profile_mod.save(902, {"bond_target_pct": 40})
    out = govbond_etf.compare_govbond_candidates(902)
    short = [c for c in out["candidates"] if c["duration_bucket"] == "short"]
    long_ = [c for c in out["candidates"] if c["duration_bucket"] == "long"]
    assert all(c["macro_fit"]["label"] == "적합" for c in short), short
    # 고금리 국면 장기채는 '주의'
    assert all(c["macro_fit"]["label"] == "주의" for c in long_), long_


def test_compare_long_bond_warning_present(monkeypatch):
    _patch_elevated(monkeypatch)
    _mk_account(903)
    out = govbond_etf.compare_govbond_candidates(903)
    assert "장기국채" in out["long_bond_volatility_warning"]
    longs = [c for c in out["candidates"] if c["duration_bucket"] == "long"]
    assert longs and all(any("변동성" in r for r in c["risks"]) for c in longs)


def test_compare_not_auto_confirm_product_note(monkeypatch):
    _patch_elevated(monkeypatch)
    _mk_account(904)
    out = govbond_etf.compare_govbond_candidates(904)
    assert "확정" in out["decision_note"]
    assert "운용 수단" in out["product_note"]


def test_compare_filters_and_exclusions(monkeypatch):
    _patch_elevated(monkeypatch)
    _mk_account(905)
    out = govbond_etf.compare_govbond_candidates(905, duration_pref="short", region="한국")
    for c in out["candidates"]:
        assert c["duration_bucket"] == "short" and c["region"] == "한국", c
    # 제외된 것들은 사유와 함께 정직 기록
    assert out["excluded"] and all("reason" in x for x in out["excluded"])


def test_compare_data_quality_unknown_for_us(monkeypatch):
    _patch_elevated(monkeypatch)
    _mk_account(906)
    out = govbond_etf.compare_govbond_candidates(906, region="미국")
    for c in out["candidates"]:
        dq = c["data_quality"]
        assert dq["price"] == "unknown" and dq["data_available"] is False, dq
        assert dq["expense_ratio"] == "unknown" and dq["yield"] == "unknown", dq


def test_compare_account_purpose_reflected(monkeypatch):
    _patch_elevated(monkeypatch)
    _mk_account(907)
    profile_mod.save(907, {"bond_target_pct": 40, "bond_duration_pref": "long"})
    out = govbond_etf.compare_govbond_candidates(907)
    longs = [c for c in out["candidates"] if c["duration_bucket"] == "long"]
    # 계좌가 long 선호 → purpose_fit 이 '적합' (단, 방어성향이면 주의로 바뀜)
    assert longs and any(c["purpose_fit"]["label"] in ("적합", "주의") for c in longs)
    assert out["account_purpose"]["duration_pref"] == "long"


def test_no_order_or_policy_keys_in_output(monkeypatch):
    _patch_elevated(monkeypatch)
    _mk_account(908)
    out = govbond_etf.compare_govbond_candidates(908)
    for forbidden in ("client_order_id", "order", "orders", "approval", "policy_written"):
        assert forbidden not in out, forbidden


if __name__ == "__main__":
    setup()
    import inspect
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    for f in fns:
        if "monkeypatch" in inspect.signature(f).parameters:
            continue  # monkeypatch 필요한 건 pytest 로만
        f()
        print(f"  PASS {f.__name__}")
    print("manual subset PASSED (monkeypatch tests via pytest)")
