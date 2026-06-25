"""방어자산 조언 숫자형 결론 — 검증/3안/정규화 (CEO: 설명으로 끝내지 말 것)."""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_defnum.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import field_advisors as fa


def setup():
    store_db.init()


# ---- validate_defensive ----
def test_valid_sum_100():
    v = fa.validate_defensive(30, 10)            # 방어 40, 위험 60
    assert v["ok"], v
    assert v["defensive_bucket_pct"] == 40.0 and v["risk_asset_pct"] == 60.0, v


def test_separate_bucket_110_is_error():
    # 현금 40 + 채권 10 + 위험 60 = 110 → hard error (방어에 채권 무조건 더하기 금지)
    v = fa.validate_defensive(40, 10, risk_asset_pct=60)
    assert not v["ok"], v
    assert any("110" in e or "100%가 아" in e for e in v["errors"]), v


def test_bond_exceeds_defensive_error():
    v = fa.validate_defensive(5, 95)             # 채권 95 > 방어 100? defensive=100, bond=95 ≤100 ok
    assert v["ok"], v
    v2 = fa.validate_defensive(-5, 50)           # 순현금 음수
    assert not v2["ok"], v2
    # 채권 > 방어: 순현금 음수로 표현되거나 bond>defensive
    v3 = fa.validate_defensive(10, 40, risk_asset_pct=60)  # 방어 50, 위험 60 → 합 110
    assert not v3["ok"], v3


def test_normalize_duration():
    assert fa.normalize_duration("단기") == "short"
    assert fa.normalize_duration("장기채") == "long"
    assert fa.normalize_duration("사다리") == "ladder"
    assert fa.normalize_duration("혼합") == "mixed"
    assert fa.normalize_duration("xyz") is None


# ---- defensive_options 3안 ----
def test_three_options_numeric():
    opts = fa.defensive_options(cash_min=30, cash_max=50, bond_pct=10, duration="단기")
    names = [o["option"] for o in opts]
    assert names == ["conservative", "base", "aggressive"], names
    for o in opts:
        # 방어 = 순현금 + 채권, 위험 = 100 - 방어, 채권 ≤ 방어 (표시용 절대값 불변식)
        assert round(o["pure_cash_pct"] + o["bond_pct"], 1) == o["defensive_bucket_pct"], o
        assert round(o["defensive_bucket_pct"] + o["risk_asset_pct"], 1) == 100.0, o
        assert o["bond_pct"] <= o["defensive_bucket_pct"], o
        assert 0 <= o["bond_ratio_pct"] <= 100, o
    # 채권 비율 중심: 보수 국채비율 > 기준 > 공격 (현금은 안 건드림)
    assert opts[0]["bond_ratio_pct"] > opts[1]["bond_ratio_pct"] >= opts[2]["bond_ratio_pct"], opts
    # 방어자산(현금밴드)은 3안 공통 고정 — 채권 조언이 현금을 바꾸지 않음
    assert opts[0]["defensive_bucket_pct"] == opts[1]["defensive_bucket_pct"] == opts[2]["defensive_bucket_pct"], opts
    # 보수=사다리(mixed), 공격=단기(short)
    assert opts[0]["bond_duration_preference"] == "mixed" and opts[2]["bond_duration_preference"] == "short", opts


# ---- defensive_advisor 통합 ----
def test_advisor_returns_numeric_and_options():
    out = fa.defensive_advisor(1, "현금 40, 채권 10% 단기채")
    ev = out["extracted_variables"]
    assert "recommendation" in ev and "options" in ev, ev
    rec = ev["recommendation"]
    assert rec["defensive_bucket_pct"] == rec["pure_cash_pct"] + rec["bond_pct"], rec
    assert len(ev["options"]) == 3, ev


def test_long_duration_warning():
    out = fa.defensive_advisor(1, "채권 10% 장기채")
    assert any("장기채" in w or "듀레이션" in w for w in out["risk_warnings"]), out["risk_warnings"]


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} DEFENSIVE-NUMERIC TESTS PASSED")
