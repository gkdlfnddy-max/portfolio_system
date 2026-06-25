"""변이별 전략 요약(Track 2) 테스트 — 3안 설명, bucket 합=100, 방어=순현금+국채, 헤지≠테마.

Anthropic API 미사용 — 임시 SQLite 로 실제 allocation 결과 위에서 검증.
임시 SQLITE_PATH 를 import 전에 주입 → setup() 에서 store_db.init().
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_alloc_explain.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import allocation as alloc
from main_mission.portfolio_os import selection as sel
from main_mission.portfolio_os import allocation_explain as ax


def setup():
    store_db.init()


def _seed(idx, *, interests="로봇, 바이오, 양자컴퓨터", hedge_themes=None,
          cash_min=10.0, cash_max=30.0, bond_pct=None):
    """profile + snapshot(현재 현금 100%) 시드. 헤지/채권은 선택."""
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
            "interests_text, hedge_themes, bond_target_pct, updated_at) "
            "VALUES(?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(account_index) DO UPDATE SET interests_text=excluded.interests_text, "
            "hedge_themes=excluded.hedge_themes, bond_target_pct=excluded.bond_target_pct",
            (idx, "neutral", cash_min, cash_max, interests, hedge_themes, bond_pct),
        )
        # 현재 현금 100% (전액 현금) → 큰 drift / 다회차 분할을 유도.
        conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, source, captured_at) "
            "VALUES(?,?,?,?,?,datetime('now'))", (idx, 10000000, 10000000, 0, "test"),
        )
        conn.commit()
    finally:
        conn.close()


def _options(idx):
    alloc.generate(idx)  # 3안 생성(이미 있으면 explain_options 가 최신 사용)
    return ax.explain_options(idx)


# ---- 3안 모두 존재 + summary/rebalance_reason 비어있지 않음 ----
def test_three_variants_present_with_text():
    _seed(31)
    out = _options(31)
    assert out["ok"], out
    opts = out["options"]
    assert set(opts) == {"conservative", "base", "aggressive"}, list(opts)
    for v, o in opts.items():
        assert o["summary"].strip(), (v, o)
        assert o["rebalance_reason"].strip(), (v, o)
        assert o["suitable_for"].strip(), (v, o)
        assert isinstance(o["key_risks"], list) and o["key_risks"], (v, o)


# ---- buckets 합 == 100 (각 변이) ----
def test_buckets_sum_100():
    _seed(32)
    out = _options(32)
    for v, o in out["options"].items():
        total = round(sum(b["pct"] for b in o["buckets"]), 1)
        assert total == 100.0, (v, total, o["buckets"])


# ---- defensive_pct == pure_cash + bond ----
def test_defensive_equals_cash_plus_bond():
    _seed(33, bond_pct=8.0)
    out = _options(33)
    for v, o in out["options"].items():
        bmap = {b["bucket_type"]: b["pct"] for b in o["buckets"]}
        expect = round(bmap.get("pure_cash", 0.0) + bmap.get("bond", 0.0), 1)
        assert o["defensive_pct"] == expect, (v, o["defensive_pct"], bmap)
        assert o["risk_pct"] == round(100.0 - expect, 1), (v, o["risk_pct"])


# ---- bond bucket 은 채권 0% 여도 항상 포함 ----
def test_bond_bucket_present_even_if_zero():
    _seed(34)  # bond_pct 미설정 → 국채 0%
    out = _options(34)
    for v, o in out["options"].items():
        bonds = [b for b in o["buckets"] if b["bucket_type"] == "bond"]
        assert len(bonds) == 1, (v, o["buckets"])
        assert bonds[0]["pct"] == 0.0, (v, bonds[0])
        assert "0%" in bonds[0]["explanation"] or "없" in bonds[0]["explanation"], bonds[0]


# ---- 헤지 row → bucket_type 'hedge' (theme 로 재분류 금지) ----
def test_hedge_row_maps_to_hedge_not_theme():
    # 반도체를 헤지 테마로 → allocation 이 kind=hedge 로 분리, explain 은 bucket hedge 로 렌더.
    _seed(35, interests="로봇, 반도체", hedge_themes="반도체")
    out = _options(35)
    found_hedge = False
    for v, o in out["options"].items():
        btypes = {b["bucket_type"] for b in o["buckets"] if b["pct"] > 0}
        # 반도체가 theme bucket 안에 섞이면 안 됨 — theme 은 롱(로봇)만.
        theme_b = next((b for b in o["buckets"] if b["bucket_type"] == "theme"), None)
        if theme_b:
            assert "반도체" not in theme_b["explanation"], (v, theme_b)
        if "hedge" in btypes:
            found_hedge = True
            hb = next(b for b in o["buckets"] if b["bucket_type"] == "hedge")
            assert "반도체" in hb["explanation"], (v, hb)
    assert found_hedge, "어느 변이에서도 hedge bucket 이 나타나지 않음"


# ---- rebalance_reason 은 분할/회차 또는 drift 를 언급 ----
def test_rebalance_reason_mentions_rounds_or_drift():
    _seed(36)
    out = _options(36)
    for v, o in out["options"].items():
        rr = o["rebalance_reason"]
        assert ("분할" in rr or "회차" in rr or "회로" in rr or "조정" in rr), (v, rr)


# ---- summary 가 방어%/코어 ETF 를 언급 (실측 숫자 기반) ----
def test_summary_mentions_defensive_and_core():
    _seed(37)
    out = _options(37)
    for v, o in out["options"].items():
        s = o["summary"]
        assert "방어자산" in s, (v, s)
        assert "코어 ETF" in s, (v, s)


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} ALLOCATION-EXPLAIN TESTS PASSED")
