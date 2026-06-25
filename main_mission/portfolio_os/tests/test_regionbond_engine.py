"""지역/채권 엔진 반영 테스트.

검증:
  - allocation _variant: 채권 bucket(현금과 분리) + 지역별 anchor 분해, 합계 100, 현금밴드 우선(현금 보존)
  - allocation.generate E2E: investor_profile(region/bond) → 3안에 bond/region anchor 반영
  - regionbond.validate 배선: selection.precheck (합계오류=warn, 국가집중/신흥국/현금채권충돌=block)
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_rbengine.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import allocation as alloc
from main_mission.portfolio_os import selection as sel
from main_mission.portfolio_os import regionbond


def setup():
    store_db.init()


def _sum(rows):
    return round(sum(r["weight_pct"] for r in rows), 1)


# ---- _variant: 국채(현금의 일부) / 지역 분해 ----
def test_variant_govbond_within_cash():
    # CEO 방침: bond_pct = 방어자산 중 국채 비율. 방어 30 중 국채 비율 50% → 국채 15? 아니다.
    # 여기선 국채 절대 10을 원하므로 방어 30의 1/3 = 비율 33.33% → round(30*33.33/100)=10, 순현금 20.
    # (테마 2개 → tilt가 섹터상한에 안 걸려 shortfall 없음 = 깨끗한 분배)
    rows = alloc._variant("base", 30.0, ["반도체", "2차전지"], [], sector_max=30.0, inverse_max=10.0,
                          bond_pct=100.0 / 3, region_targets={"미국": 60, "한국": 40}, duration="short")
    cash = [r for r in rows if r["kind"] == "cash"][0]["weight_pct"]
    bonds = [r for r in rows if r["kind"] == "bond"]
    assert cash == 20.0, rows                                  # 순현금 = 방어 - 국채
    assert len(bonds) == 1 and bonds[0]["weight_pct"] == 10.0, rows
    assert "국채" in bonds[0]["ref"] and "short" in bonds[0]["ref"], bonds
    assert round(cash + bonds[0]["weight_pct"], 1) == 30.0, rows  # 방어 총량(현금밴드) 보존
    assert _sum(rows) == 100.0, _sum(rows)


def test_variant_region_splits_anchor():
    rows = alloc._variant("base", 40.0, [], [], sector_max=30.0, inverse_max=10.0,
                          bond_pct=0.0, region_targets={"미국": 50, "한국": 30, "기타/글로벌": 20})
    anchors = [r for r in rows if r["kind"] == "anchor"]
    refs = {a["ref"] for a in anchors}
    assert any("미국" in r for r in refs) and any("한국" in r for r in refs), refs
    # 광범위 단일 anchor 가 아니라 지역별로 쪼개짐
    assert "글로벌 코어 ETF" not in refs, refs
    assert _sum(rows) == 100.0, _sum(rows)


def test_variant_no_region_falls_back_to_broad():
    rows = alloc._variant("base", 40.0, [], [], sector_max=30.0, inverse_max=10.0, region_targets={})
    anchors = [r for r in rows if r["kind"] == "anchor"]
    assert anchors and anchors[0]["ref"] == "글로벌 코어 ETF", anchors


def test_variant_govbond_clamped_to_cash():
    # 현금(방어) 90 → 국채는 그 안에서 비율로. bond_pct=100(방어 전부 국채) → govbond 90, 순현금 0.
    # ratio 는 0~100 으로 clamp(>100 입력해도 100 으로 묶임 = 방어 전부 국채).
    # (테마 1개로 invested(10)의 tilt가 깔끔히 배치 — shortfall 없음)
    rows = alloc._variant("conservative", 90.0, ["반도체"], [], sector_max=30.0, inverse_max=10.0, bond_pct=150.0)
    cash = [r for r in rows if r["kind"] == "cash"][0]["weight_pct"]
    bond = sum(r["weight_pct"] for r in rows if r["kind"] == "bond")
    assert bond == 90.0, rows                    # ratio clamp 100 → 방어 전부 국채
    assert round(cash + bond, 1) == 90.0, rows  # 방어 총량 보존(국채는 현금 침범 아님 — 현금의 구성)
    assert _sum(rows) == 100.0, _sum(rows)


# ---- generate E2E ----
def _seed_profile(account_index, region_json, bond_pct, dur):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
            "interests_text, hedge_themes, region_targets, bond_target_pct, bond_duration_pref, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(account_index) DO UPDATE SET region_targets=excluded.region_targets, "
            "bond_target_pct=excluded.bond_target_pct, bond_duration_pref=excluded.bond_duration_pref",
            (account_index, "neutral", 10.0, 30.0, "반도체, 2차전지", None, region_json, bond_pct, dur),
        )
        conn.commit()
    finally:
        conn.close()


def test_generate_reflects_region_and_bond():
    _seed_profile(7, '{"미국": 50, "한국": 50}', 12.0, "short")
    out = alloc.generate(7)
    assert out["ok"], out
    assert out["bond"]["target_pct"] == 12.0 and out["region_targets"]["미국"] == 50, out
    base = out["variants"]["base"]
    assert any(r["kind"] == "bond" for r in base), base           # 채권 bucket 생성
    assert any(r["kind"] == "anchor" and "미국" in (r["ref"] or "") for r in base), base  # 지역 anchor
    for v, rows in out["variants"].items():
        assert _sum(rows) == 100.0, (v, _sum(rows))


# ---- regionbond.validate 배선: selection.precheck ----
def _policy(region_targets, bond_pct, cmin=10.0, cmax=40.0):
    return {"limits": {"sector_max_pct": 30.0, "single_name_max_pct": 20.0, "one_order_cap_pct": 5.0,
                       "inverse_max_pct": 10.0, "max_single_country_pct": 70.0, "emerging_market_max_pct": 20.0},
            "cash_band": {"min": cmin, "max": cmax},
            "region_targets": region_targets, "bond": {"target_pct": bond_pct}}


def test_precheck_region_sum_warns_not_blocks():
    rows = [{"kind": "cash", "ref": None, "weight_pct": 30.0}, {"kind": "anchor", "ref": "미국 기본배분", "weight_pct": 70.0}]
    out = sel.precheck(rows, _policy({"미국": 50, "한국": 40}, None), stale=False)  # 합계 90
    assert out["status"] == "warn", out
    assert any("합계" in r["msg"] for r in out["reasons"]), out


def test_precheck_country_concentration_blocks():
    rows = [{"kind": "cash", "ref": None, "weight_pct": 20.0}, {"kind": "anchor", "ref": "미국 기본배분", "weight_pct": 80.0}]
    out = sel.precheck(rows, _policy({"미국": 80, "한국": 20}, None), stale=False)
    assert out["status"] == "block", out
    assert any("국가 집중" in r["msg"] for r in out["reasons"]), out


def test_precheck_cash_bond_conflict_blocks():
    rows = [{"kind": "cash", "ref": None, "weight_pct": 20.0}, {"kind": "anchor", "ref": "미국 코어", "weight_pct": 80.0}]
    # bond_target_pct = 방어자산 대비 비율(ratio). 120 > 100 → block (방어 100% 초과 불가).
    out = sel.precheck(rows, _policy({}, 120.0, cmin=10.0, cmax=40.0), stale=False)
    assert out["status"] == "block", out
    assert any("100%를 초과" in r["msg"] for r in out["reasons"]), out


def test_govbond_defensive_bucket_ceo_example():
    # CEO 예시: 방어자산 40, 국채 = 방어의 25% → 국채 40×0.25 = 10, 순현금 30, 위험자산 60.
    # (bond_pct 는 이제 방어자산 대비 비율(ratio). 방어 bucket 안에서 순현금과 분할)
    rows = alloc._variant("base", 40.0, ["반도체", "2차전지"], [], sector_max=30.0, inverse_max=10.0, bond_pct=25.0)
    cash = next(r["weight_pct"] for r in rows if r["kind"] == "cash")
    bond = sum(r["weight_pct"] for r in rows if r["kind"] == "bond")
    risk = sum(r["weight_pct"] for r in rows if r["kind"] not in ("cash", "bond"))
    assert cash == 30.0, rows           # 순현금 = 방어 - 채권
    assert bond == 10.0, rows           # 채권/국채
    assert round(cash + bond, 1) == 40.0  # 방어자산 = 순현금 + 채권 (현금밴드 총량)
    assert round(risk, 1) == 60.0       # 위험자산 = 100 - 방어 (채권이 위험자산을 줄이지 않음)


def test_validate_direct_emerging_cap():
    v = regionbond.validate({"신흥국": 30, "미국": 70}, None, {"min": 10}, emerging_max=20.0)
    assert any(x["limit"] == "emerging_market_max_pct" for x in v), v


# ---- 확정 흐름 연결 (#3) + history 데이터 (#4) ----
def test_selection_carries_region_bond_into_confirmed_allocation():
    import json
    _seed_profile(8, '{"미국": 60, "한국": 40}', 10.0, "short")
    conn = store_db.connect()
    try:
        conn.execute("INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, source, captured_at) "
                     "VALUES(8, 5000000, 10000000, 0, 'test', datetime('now'))")
        conn.commit()
    finally:
        conn.close()
    out = alloc.generate(8)
    res = sel.select(8, out["proposal_id"], "base")
    assert res["ok"], res
    conn = store_db.connect()
    try:
        row = conn.execute("SELECT allocation, policy_version FROM allocation_selections "
                           "WHERE account_index=8 AND status='active'").fetchone()
    finally:
        conn.close()
    rows = json.loads(row["allocation"])
    kinds = {r["kind"] for r in rows}
    assert "bond" in kinds, rows                                              # 채권 bucket이 확정안에 보존(#3)
    assert any(r["kind"] == "anchor" and "미국" in (r["ref"] or "") for r in rows), rows  # 지역 anchor 보존
    # history 페이지 composeOf 가 읽는 구성: 현금/채권/지역 합산 가능
    cash = sum(r["weight_pct"] for r in rows if r["kind"] == "cash")
    bond = sum(r["weight_pct"] for r in rows if r["kind"] == "bond")
    # bond_pct=10 은 이제 방어자산 대비 비율. base 방어=target 20 → 국채 20×0.10 = 2.0(절대%).
    assert cash > 0 and bond == 2.0, (cash, bond)


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} REGIONBOND-ENGINE TESTS PASSED")
