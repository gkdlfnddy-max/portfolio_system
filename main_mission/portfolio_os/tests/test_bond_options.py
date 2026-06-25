"""국채 비중 후보(A/B/C/D) **추천형 엔진** 테스트 — bond_recommendation.bond_options.

검증(불변 안전 + CEO 목적):
  - 후보 3~4안 제시: 각 후보 {label, govbond_ratio_pct(방어 대비), suggested_split,
    total_breakdown(전체환산), rationale, suited_when, rising_rate_risk, falling_rate_benefit,
    fx_risk, liquidity, account_fit, confidence, system_recommended}.
  - 전체환산 carve 정합: 순현금 + 국채 = 방어, 방어 + 위험 = 100, 단기+장기 = 국채.
  - 계좌 인지: 방어형(loss_reduction/low) → 보수적 후보, 성장형(aggressive_growth/high) → 국채 낮음.
  - 거시 인지: 인상기/고금리 → 단기 위주(장기 절제), 인하기대/하락 → 장기 비중↑.
  - system_recommended: regime 기준 1개 강조. unknown 이면 강조 없음(가짜 단정 0).
  - 장기국채 변동성 경고: 항상 존재 + 장기 split 있는 후보에 개별 경고.
  - 추천일 뿐: requires_user_approval=True · auto_applied=False · 주문/policy 흔적 0.
  - Anthropic API 미사용.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_bondoptions.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import (
    bond_recommendation as br,
    bond_bucket,
    macro_connect as mc,
    user_views,
    investor_objective,
    profile as profile_mod,
)


def setup():
    os.environ["SQLITE_PATH"] = _TMP
    store_db.init()


def setup_function(_fn=None):
    os.environ["SQLITE_PATH"] = _TMP
    conn = store_db.connect()
    try:
        conn.execute("DELETE FROM macro_indicators")
        conn.execute("DELETE FROM user_views")
        conn.commit()
    finally:
        conn.close()


def _iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _mk_account(idx: int):
    conn = store_db.connect()
    try:
        conn.execute("INSERT OR IGNORE INTO accounts(account_index, mode, alias) VALUES(?,?,?)",
                     (idx, "mock", f"acct{idx}"))
        conn.commit()
    finally:
        conn.close()


def _empty_snapshot() -> dict:
    return {"data_available": False, "indicators": {}, "as_of": _iso(0)}


# ============================================================
# 1) 기본 구조 — 후보 3~4안, 필수 필드, 추천일 뿐
# ============================================================
def test_options_structure_and_fields():
    _mk_account(901)
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), 0.5, "test")  # rising
    out = br.bond_options(901)
    assert out["ok"] is True, out
    assert out["applies_to_defensive"] is True, out
    opts = out["options"]
    assert 3 <= len(opts) <= 4, out
    required = {"label", "govbond_ratio_pct", "suggested_split", "total_breakdown",
                "rationale", "suited_when", "rising_rate_risk", "falling_rate_benefit",
                "fx_risk", "liquidity", "account_fit", "confidence", "system_recommended"}
    for o in opts:
        assert required <= set(o.keys()), (set(required) - set(o.keys()), o)
        assert 0.0 <= o["govbond_ratio_pct"] <= 100.0, o
    # 라벨 A/B/C...
    assert opts[0]["label"] == "A", opts


def test_recommendation_only_no_auto_apply():
    _mk_account(902)
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), 0.5, "test")
    out = br.bond_options(902)
    assert out["requires_user_approval"] is True, out
    assert out["auto_applied"] is False, out
    assert out["auto_order_created"] is False, out
    assert out["policy_written"] is False, out
    for forbidden in ("order", "orders", "client_order_id", "approval", "fill"):
        assert forbidden not in out, forbidden


# ============================================================
# 2) 전체환산 carve 정합 (방어총량 알 때)
# ============================================================
def test_total_breakdown_carve_consistent():
    _mk_account(903)
    profile_mod.save(903, {"bond_target_pct": 40, "bond_duration_pref": "short"})  # 방어=cash band
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), -0.5, "test")  # falling
    out = br.bond_options(903)
    for o in out["options"]:
        tb = o["total_breakdown"]
        assert tb is not None, o
        defn = tb["defensive_bucket_pct"]
        gov = tb["govbond_pct_of_total"]
        cash = tb["pure_cash_pct_of_total"]
        risk = tb["risk_asset_pct"]
        short = tb["short_govbond_pct_of_total"]
        long = tb["long_govbond_pct_of_total"]
        assert round(cash + gov, 1) == defn, tb
        assert round(defn + risk, 1) == 100.0, tb
        assert round(short + long, 1) == gov, tb


# ============================================================
# 3) 거시 인지 — 인상기는 단기 위주(장기 절제), 하락기는 장기↑
# ============================================================
def test_rising_keeps_duration_short():
    _mk_account(904)
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), 0.75, "test")  # rising
    out = br.bond_options(904)
    assert out["rate_regime"] == "rising", out
    for o in out["options"]:
        sp = o["suggested_split"]
        if sp is not None:  # 국채 0% 후보는 split None
            assert sp["long"] <= 20.0, (o["govbond_ratio_pct"], sp)


def test_falling_increases_long_duration():
    _mk_account(905)
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), -0.5, "test")  # falling
    out = br.bond_options(905)
    assert out["rate_regime"] == "falling", out
    # 하락기엔 비중 있는 후보들의 장기 split 이 인상기보다 큼(>=40).
    long_splits = [o["suggested_split"]["long"] for o in out["options"]
                   if o["suggested_split"] is not None]
    assert long_splits, out
    assert max(long_splits) >= 40.0, long_splits


# ============================================================
# 4) 계좌 인지 — 방어형 vs 성장형 후보 다름
# ============================================================
def test_defensive_account_vs_growth_account_differ():
    _mk_account(906)
    _mk_account(907)
    # 동일 거시(uncertain — 비편향), 목적만 다르게.
    mc.upsert_indicator("policy_rate", _iso(5), 2.0, "test")  # 낮은 금리, 변화 정보 없음 → 약함
    # uncertain 유도 위해 곡선 혼조 없이 level 만 → high 아닌 약신호 처리. 명확히 하기 위해 user view 사용 X.
    investor_objective.set_objective(906, {"investment_goal": "loss_reduction",
                                           "risk_tolerance": "low", "loss_aversion": 0.8})
    investor_objective.set_objective(907, {"investment_goal": "aggressive_growth",
                                           "risk_tolerance": "high"})
    d = br.bond_options(906)
    g = br.bond_options(907)
    assert d["objective"]["lean"] == "defensive", d["objective"]
    assert g["objective"]["lean"] == "growth", g["objective"]
    d_ratios = sorted(o["govbond_ratio_pct"] for o in d["options"])
    g_ratios = sorted(o["govbond_ratio_pct"] for o in g["options"])
    # 방어형 최대 후보 >= 성장형 최대 후보(방어형이 더 적극적으로 국채 제시).
    assert max(d_ratios) >= max(g_ratios), (d_ratios, g_ratios)
    # account_fit 텍스트가 목적별로 다름.
    assert d_ratios != g_ratios or any(
        do["account_fit"] != go["account_fit"]
        for do, go in zip(d["options"], g["options"])), (d_ratios, g_ratios)


def test_objective_unset_is_honest():
    _mk_account(908)
    out = br.bond_options(908, snapshot=_empty_snapshot())
    assert out["objective"]["is_set"] is False, out
    assert out["objective"]["lean"] == "neutral", out
    assert any("미설정" in o["account_fit"] for o in out["options"]), out


# ============================================================
# 5) system_recommended — regime 기준 1개 강조, unknown 이면 없음
# ============================================================
def test_system_recommended_exactly_one_when_regime_known():
    _mk_account(909)
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), 0.5, "test")  # rising
    out = br.bond_options(909)
    flagged = [o for o in out["options"] if o["system_recommended"]]
    assert len(flagged) == 1, flagged
    assert out["system_recommended_ratio_pct"] == flagged[0]["govbond_ratio_pct"], out


def test_no_system_recommended_when_unknown():
    _mk_account(910)
    out = br.bond_options(910, snapshot=_empty_snapshot())
    assert out["rate_regime"] == "unknown", out
    assert out["data_source"] == "none", out
    assert out["system_recommended_ratio_pct"] is None, out
    assert all(o["system_recommended"] is False for o in out["options"]), out
    # 후보 사다리는 여전히 제시(일반 기준).
    assert len(out["options"]) >= 3, out


# ============================================================
# 6) 장기국채 변동성 경고 (불변)
# ============================================================
def test_long_bond_volatility_warning_present():
    _mk_account(911)
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), -0.5, "test")  # falling → 장기↑
    out = br.bond_options(911)
    assert "안전자산" in out["long_bond_volatility_warning"], out
    # 장기 split 있는 후보엔 개별 경고도 존재.
    long_opts = [o for o in out["options"]
                 if o["suggested_split"] and o["suggested_split"]["long"] > 0]
    assert long_opts, out
    assert all(o["long_bond_volatility_warning"] for o in long_opts), long_opts


# ============================================================
# 7) 사용자 금리 견해 폴백 + confidence
# ============================================================
def test_user_view_drives_options():
    _mk_account(912)
    user_views.add(912, layer="mid", theme="금리", note="금리 인하 예상", stance="negative")
    out = br.bond_options(912, snapshot=_empty_snapshot())
    assert out["data_source"] == "user_view", out
    assert out["rate_regime"] == "cut_expected", out
    # user_view confidence(0.5) — 목적 미설정이므로 가산 없음.
    assert all(o["confidence"] == 0.5 for o in out["options"]), out


def test_confidence_higher_with_macro_and_objective():
    _mk_account(913)
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), 0.5, "test")  # macro
    investor_objective.set_objective(913, {"investment_goal": "growth", "risk_tolerance": "mid"})
    out = br.bond_options(913)
    # macro(0.75) + 목적 설정(+0.1) = 0.85.
    assert all(o["confidence"] >= 0.8 for o in out["options"]), out


# ============================================================
# 8) 한국 국채 ETF 실 티커 (bond_bucket)
# ============================================================
def test_korean_govbond_etf_real_tickers():
    cands = bond_bucket.govbond_etf_candidates(region="한국")
    assert cands, cands
    tickers = {c["ticker"] for c in cands}
    # 실 KRX 종목코드(6자리 숫자) — placeholder(KR_GOV_*) 제거 확인.
    assert "153130" in tickers, tickers   # KODEX 단기채권
    assert "439870" in tickers or "451530" in tickers, tickers  # 장기(KODEX/TIGER 30년)
    for c in cands:
        assert not c["ticker"].startswith("KR_GOV_"), c
        assert c["ticker"].isdigit() and len(c["ticker"]) == 6, c
        assert c["bond_type"] == "government", c
        assert c["data_connected"] is False, c  # 지표 미연동 정직


def test_us_govbond_etf_tickers_kept():
    cands = bond_bucket.govbond_etf_candidates(region="미국")
    tickers = {c["ticker"] for c in cands}
    assert {"SHY", "IEF", "TLT"} <= tickers, tickers


# ============================================================
# 9) Anthropic API 미사용
# ============================================================
def test_no_anthropic_import():
    import pathlib
    for mod in (br, bond_bucket):
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8").lower()
        assert "import anthropic" not in src, mod.__file__
        assert "anthropic_api_key" not in src, mod.__file__


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        setup_function()
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} BOND-OPTIONS TESTS PASSED")
