"""방어자산 정밀화 — government_only · duration(mixed 50/50) · defensive_breakdown · 국채 ETF 후보."""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_bondbucket.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import bond_bucket, regionbond, profile as profile_mod


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


# ---- government_only 검증 (regionbond.validate) ----------------------------
def test_validate_government_only_default_ok():
    v = regionbond.validate(None, 40.0, None)
    assert v == [] or all(x["limit"] != "bond_allowed_types" for x in v), v


def test_validate_non_government_types_blocked():
    v = regionbond.validate(None, 40.0, None, bond_allowed_types="corporate")
    assert any(x["limit"] == "bond_allowed_types" for x in v), v


def test_validate_non_government_intent_text_blocked():
    v = regionbond.validate(None, 40.0, None, bond_intent_text="회사채랑 하이일드 좀 담고싶다")
    labels = [x["observed"] for x in v if x["limit"] == "non_government_bond"]
    assert any("회사채" in l for l in labels), v
    assert any("하이일드" in l for l in labels), v


def test_detect_non_government_bonds():
    assert regionbond.detect_non_government_bonds("국채만 단기로") == []
    found = regionbond.detect_non_government_bonds("신흥국 채권 + 전환사채")
    assert "신흥국채" in found and "복잡/구조화 채권" in found, found


def test_parse_bond_flags_non_government():
    b = regionbond.parse_bond("채권 10% 단기채, 하이일드도 조금")
    assert b["allowed_types"] == "government_only", b
    assert "하이일드/정크" in b["non_government"], b
    assert any("government_only" in n for n in b["notes"]), b


# ---- compute_breakdown carve 정합 (allocation 과 일치) ----------------------
def test_breakdown_example_40_40():
    # 방어 40, 국채비율 40% → 국채 16, 순현금 24, 위험 60
    bd = bond_bucket.compute_breakdown(40.0, 40.0, "short")
    assert bd["govbond_pct"] == 16.0, bd
    assert bd["pure_cash_pct"] == 24.0, bd
    assert bd["risk_asset_pct"] == 60.0, bd
    assert bd["short_govbond_pct"] == 16.0, bd


def test_breakdown_mixed_default_50_50():
    # mixed 기본 단기50/장기50 → 국채16 → 단기8 / 장기8
    bd = bond_bucket.compute_breakdown(40.0, 40.0, "mixed")
    assert bd["short_govbond_pct"] == 8.0, bd
    assert bd["long_govbond_pct"] == 8.0, bd
    assert round(bd["short_govbond_pct"] + bd["long_govbond_pct"], 1) == bd["govbond_pct"], bd


def test_breakdown_mixed_custom_split():
    bd = bond_bucket.compute_breakdown(40.0, 40.0, "mixed", {"short": 75.0, "long": 25.0})
    assert bd["short_govbond_pct"] == 12.0, bd
    assert bd["long_govbond_pct"] == 4.0, bd


def test_breakdown_invariants():
    for defn, ratio, dur in [(40, 40, "short"), (30, 50, "mixed"), (60, 0, None), (50, 100, "long")]:
        bd = bond_bucket.compute_breakdown(defn, ratio, dur)
        assert round(bd["pure_cash_pct"] + bd["govbond_pct"], 1) == bd["defensive_bucket_pct"], bd
        assert round(bd["defensive_bucket_pct"] + bd["risk_asset_pct"], 1) == 100.0, bd
        assert round(bd["short_govbond_pct"] + bd["intermediate_govbond_pct"]
                     + bd["long_govbond_pct"], 1) == bd["govbond_pct"], bd


# ---- 국채 ETF 후보 (정직 미연동) -------------------------------------------
def test_etf_candidates_government_only_and_honest():
    cands = bond_bucket.govbond_etf_candidates()
    assert cands, "후보가 비어있음"
    for c in cands:
        assert c["bond_type"] == "government", c       # 국채만
        assert c["data_connected"] is False, c          # 미연동 정직표기
        assert c["status"] == "후보·검증 필요·데이터 미연동", c
        # 가짜 지표 금지: 수익률/보수율/듀레이션 수치 키가 없어야 함
        for forbidden in ("yield", "ytm", "expense_ratio", "duration_years", "price"):
            assert forbidden not in c, (forbidden, c)


def test_etf_candidates_duration_filter_mixed():
    cands = bond_bucket.govbond_etf_candidates("mixed")
    bands = {c["duration_band"] for c in cands}
    assert bands <= {"short", "long"}, bands
    assert "intermediate" not in bands, bands


def test_etf_candidates_region_filter():
    us = bond_bucket.govbond_etf_candidates(region="미국")
    assert us and all(c["region"] == "미국" for c in us), us


# ---- profile.save: government_only 강제 · duration_split 정규화 -------------
def test_profile_forces_government_only():
    _mk_account(701)
    profile_mod.save(701, {"bond_target_pct": 40, "bond_duration_pref": "short",
                           "bond_allowed_types": "high_yield"})
    p = profile_mod.get(701)
    assert p["bond_allowed_types"] == "government_only", p


def test_profile_mixed_default_split_saved():
    _mk_account(702)
    profile_mod.save(702, {"bond_target_pct": 40, "bond_duration_pref": "mixed"})
    p = profile_mod.get(702)
    import json
    split = json.loads(p["bond_duration_split"])
    assert split == {"short": 50.0, "long": 50.0}, split


def test_profile_mixed_custom_split_normalized_to_100():
    _mk_account(703)
    profile_mod.save(703, {"bond_target_pct": 40, "bond_duration_pref": "mixed",
                           "bond_duration_split": {"short": 30, "long": 10}})  # 합 40 → 정규화
    p = profile_mod.get(703)
    import json
    split = json.loads(p["bond_duration_split"])
    assert round(split["short"] + split["long"], 1) == 100.0, split
    assert split["short"] == 75.0 and split["long"] == 25.0, split


def test_profile_non_mixed_no_split():
    _mk_account(704)
    profile_mod.save(704, {"bond_target_pct": 40, "bond_duration_pref": "short"})
    p = profile_mod.get(704)
    assert p["bond_duration_split"] in (None, ""), p


# ---- defensive_breakdown 통합 ---------------------------------------------
def test_defensive_breakdown_integration():
    _mk_account(705)
    profile_mod.save(705, {"bond_target_pct": 40, "bond_duration_pref": "mixed"})
    out = bond_bucket.defensive_breakdown(705)
    assert out["ok"], out
    assert out["bond_allowed_types"] == "government_only", out
    bd = out["breakdown"]
    # carve 정합
    assert round(bd["pure_cash_pct"] + bd["govbond_pct"], 1) == bd["defensive_bucket_pct"], bd
    assert round(bd["short_govbond_pct"] + bd["long_govbond_pct"], 1) == bd["govbond_pct"], bd
    assert out["govbond_etf_candidates"], out
    assert all(c["data_connected"] is False for c in out["govbond_etf_candidates"]), out


def test_defensive_breakdown_no_auto_order_or_policy():
    # 계산 전용 — 반환에 주문/정책 변경 흔적이 없어야 한다(키 부재로 확인).
    _mk_account(706)
    profile_mod.save(706, {"bond_target_pct": 30, "bond_duration_pref": "short"})
    out = bond_bucket.defensive_breakdown(706)
    for forbidden in ("order", "orders", "client_order_id", "policy_written", "approval"):
        assert forbidden not in out, forbidden


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} BOND-BUCKET TESTS PASSED")
