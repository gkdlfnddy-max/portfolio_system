"""금리 동향 기반 국채 비중·듀레이션 추천 엔진 테스트.

검증(불변 안전):
  - rate_regime 분류: 인상기→rising/short, 인하기대→cut_expected/long, 역전→경기둔화 신호,
    불확실→uncertain/ladder, 고금리→high/short.
  - 데이터 소스 정직: macro 연동→macro_connected, 미연동+견해→user_view, 둘 다 없음→none/unknown.
  - **금리 미연동·견해 없음** → rate_regime='unknown', 숫자 추천 없음(가짜 0).
  - 모든 비중 % 계산: 방어 대비 + 전체 환산(carve 정합 — bond_bucket 과 동일).
  - 추천일 뿐: requires_user_approval=True · auto_applied=False · 주문/policy 흔적 0.
  - applies_to_defensive=True (방어자산 내부 국채).
  - Anthropic API 미사용.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_bondreco.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import (
    bond_recommendation as br,
    macro_connect as mc,
    user_views,
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
# 1) 금리 미연동 + 견해 없음 → unknown (가짜 숫자 0)
# ============================================================
def test_unknown_when_no_macro_no_view():
    _mk_account(801)
    out = br.recommend(801, snapshot=_empty_snapshot())
    assert out["rate_regime"] == "unknown", out
    assert out["data_source"] == "none", out
    # 가짜 숫자 금지: 비중/듀레이션/split 모두 None, confidence 0.
    assert out["suggested_bond_ratio_pct"] is None, out
    assert out["suggested_duration"] is None, out
    assert out["suggested_split"] is None, out
    assert out["ladder"] is None, out
    assert out["confidence"] == 0.0, out
    # 일반 원칙만 — rationale 에 원칙 텍스트 존재.
    assert any("일반 원칙" in r for r in out["rationale"]), out
    assert "일반 원칙" in out["note"], out


# ============================================================
# 2) macro 연동 — 인상기/고금리/인하/역전/불확실
# ============================================================
def test_macro_rising_short():
    _mk_account(802)
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), 0.75, "test")  # 인상기
    out = br.recommend(802)
    assert out["rate_regime"] == "rising", out
    assert out["data_source"] == "macro_connected", out
    assert out["suggested_duration"] == "short", out
    assert out["suggested_split"]["short"] == 100.0, out
    assert out["ladder"] is False, out
    assert any("인상" in r for r in out["rationale"]), out


def test_macro_high_when_level_high_no_change():
    _mk_account(803)
    mc.upsert_indicator("policy_rate", _iso(5), 3.5, "test")  # 고금리, 변화 정보 없음
    out = br.recommend(803)
    assert out["rate_regime"] == "high", out
    assert out["suggested_duration"] == "short", out


def test_macro_falling_long():
    _mk_account(804)
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), -0.5, "test")  # 인하
    out = br.recommend(804)
    assert out["rate_regime"] == "falling", out
    assert out["suggested_duration"] == "long", out
    assert out["suggested_split"]["long"] >= out["suggested_split"]["short"], out


def test_macro_curve_inversion_signals_slowdown():
    _mk_account(805)
    mc.upsert_indicator("yield_10y", _iso(1), 3.0, "test")
    mc.upsert_indicator("yield_2y", _iso(1), 3.6, "test")  # 역전
    out = br.recommend(805)
    assert out["curve_inverted"] is True, out
    # 역전(경기둔화 선행) → 인하 기대 쪽 (장기 일부 분산)
    assert out["rate_regime"] in ("cut_expected", "uncertain"), out
    assert any("역전" in r for r in out["rationale"]), out


def test_macro_uncertain_ladder_when_hiking_but_inverted():
    _mk_account(806)
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), 0.25, "test")  # 인상 중
    mc.upsert_indicator("yield_10y", _iso(1), 3.0, "test")
    mc.upsert_indicator("yield_2y", _iso(1), 3.5, "test")  # 그러나 역전
    out = br.recommend(806)
    assert out["rate_regime"] == "uncertain", out
    assert out["ladder"] is True, out
    assert out["suggested_duration"] == "mixed", out
    assert out["suggested_split"] == {"short": 50.0, "long": 50.0}, out


def test_macro_excludes_stale_falls_back_to_view_or_unknown():
    _mk_account(807)
    mc.upsert_indicator("policy_rate", _iso(400), 5.0, "test")  # stale → 제외
    out = br.recommend(807)
    # stale 만 있으면 신선 금리 지표 없음 → 견해 없으니 unknown.
    assert out["rate_regime"] == "unknown", out
    assert out["data_source"] == "none", out


# ============================================================
# 3) 사용자 금리 견해 폴백 (macro 미연동)
# ============================================================
def test_user_view_rising(monkeypatch):
    _mk_account(808)
    user_views.add(808, layer="mid", theme="금리", note="금리 인상 계속될 듯", stance="positive")
    out = br.recommend(808, snapshot=_empty_snapshot())
    assert out["data_source"] == "user_view", out
    assert out["rate_regime"] == "rising", out
    assert out["suggested_duration"] == "short", out
    assert out["confidence"] == 0.5, out


def test_user_view_cut_expected():
    _mk_account(809)
    user_views.add(809, layer="mid", theme="채권", note="곧 금리 인하 사이클", stance="negative")
    out = br.recommend(809, snapshot=_empty_snapshot())
    assert out["data_source"] == "user_view", out
    assert out["rate_regime"] == "cut_expected", out
    assert out["suggested_duration"] in ("long", "mixed"), out


def test_user_view_uncertain_ladder():
    _mk_account(810)
    user_views.add(810, layer="mid", theme="금리", note="방향 불확실, 혼조")
    out = br.recommend(810, snapshot=_empty_snapshot())
    assert out["rate_regime"] == "uncertain", out
    assert out["ladder"] is True, out


def test_non_rate_view_ignored_unknown():
    # 금리와 무관한 견해는 regime 에 영향 주지 않음(가짜 신호 금지).
    _mk_account(811)
    user_views.add(811, layer="long", theme="반도체", note="장기 긍정", stance="positive")
    out = br.recommend(811, snapshot=_empty_snapshot())
    assert out["rate_regime"] == "unknown", out
    assert out["data_source"] == "none", out


def test_macro_overrides_user_view():
    # macro 연동되면 사용자 견해보다 실 지표 우선.
    _mk_account(812)
    user_views.add(812, layer="mid", theme="금리", note="금리 인하 예상", stance="negative")
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), 0.75, "test")  # 실제는 인상기
    out = br.recommend(812)
    assert out["data_source"] == "macro_connected", out
    assert out["rate_regime"] == "rising", out


# ============================================================
# 4) 모든 비중 % 계산 + carve 정합 (전체 환산)
# ============================================================
def test_total_breakdown_carve_consistent():
    _mk_account(813)
    profile_mod.save(813, {"bond_target_pct": 40, "bond_duration_pref": "short"})  # 방어 컨텍스트
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), 0.5, "test")  # rising
    out = br.recommend(813)
    tb = out["total_breakdown"]
    assert tb is not None, out
    # carve 불변식: 순현금 + 국채 = 방어, 방어 + 위험 = 100, 단기+장기 = 국채
    defn = tb["defensive_bucket_pct"]
    gov = tb["suggested_govbond_pct_of_total"]
    cash = tb["suggested_pure_cash_pct_of_total"]
    risk = tb["risk_asset_pct"]
    assert round(cash + gov, 1) == defn, tb
    assert round(defn + risk, 1) == 100.0, tb
    assert round(tb["suggested_short_govbond_pct_of_total"]
                 + tb["suggested_long_govbond_pct_of_total"], 1) == gov, tb


def test_bond_ratio_is_defensive_relative():
    _mk_account(814)
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), -0.5, "test")  # falling
    out = br.recommend(814)
    # 방어 대비 % (0~100 범위), 전체% 아님.
    assert 0.0 <= out["suggested_bond_ratio_pct"] <= 100.0, out
    assert out["applies_to_defensive"] is True, out


def test_comparison_direction():
    _mk_account(815)
    # 현 프로필 국채비율 낮게(0), rising → 제안 30 → increase 또는 hold.
    profile_mod.save(815, {"bond_target_pct": 0, "bond_duration_pref": "short"})
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), 0.5, "test")
    out = br.recommend(815)
    comp = out["comparison"]
    assert comp is not None, out
    assert comp["current_bond_ratio_pct"] == 0.0, comp
    assert comp["direction"] == "increase", comp
    assert comp["delta_pct_points"] == out["suggested_bond_ratio_pct"], comp


# ============================================================
# 5) 추천일 뿐 — 자동 반영/주문 0 (불변)
# ============================================================
def test_recommendation_only_no_auto_apply():
    _mk_account(816)
    mc.upsert_indicator("policy_rate_change_3m", _iso(5), 0.5, "test")
    out = br.recommend(816)
    assert out["requires_user_approval"] is True, out
    assert out["auto_applied"] is False, out
    assert out["auto_order_created"] is False, out
    assert out["policy_written"] is False, out
    # 주문/승인 흔적 키가 없어야 함.
    for forbidden in ("order", "orders", "client_order_id", "approval", "fill"):
        assert forbidden not in out, forbidden


def test_unknown_no_auto_apply_too():
    _mk_account(817)
    out = br.recommend(817, snapshot=_empty_snapshot())
    assert out["auto_applied"] is False and out["requires_user_approval"] is True, out


# ============================================================
# 6) regime 분류 직접 호출 + CLI 진입점
# ============================================================
def test_classify_regime_enum_valid():
    _mk_account(818)
    out = br.classify_rate_regime(818, snapshot=_empty_snapshot())
    assert out["rate_regime"] in br.RATE_REGIMES, out


# ============================================================
# 7) Anthropic API 미사용
# ============================================================
def test_no_anthropic_import():
    import pathlib
    src = pathlib.Path(br.__file__).read_text(encoding="utf-8").lower()
    assert "import anthropic" not in src
    assert "anthropic_api_key" not in src


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        setup_function()
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} BOND-RECOMMENDATION TESTS PASSED")
