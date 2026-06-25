"""Market context + 채권 듀레이션 추천 테스트.

검증:
  - current_context: 구조화 반환 + 실데이터 미연동(data_connected=False) 정직 표시
  - recommend_duration: 금리 불확실/상승 → short/mixed + 장기 경고; 금리 하락 → long/intermediate 허용
  - save_snapshot: market_context_snapshots 적재
  - generate_review: duration_recommendation + market_context_id 포함, no-data honest flag 존재
  - 단기/중기/장기 3종(+사다리) 명시
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_marketctx.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import market_context as mc
from main_mission.portfolio_os import allocation as alloc
from main_mission.portfolio_os import selection as sel
from main_mission.portfolio_os import daily_review as dr


def setup():
    store_db.init()


def _profile(idx, duration_pref=None):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
            "interests_text, bond_target_pct, bond_duration_pref, updated_at) "
            "VALUES(?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(account_index) DO NOTHING",
            (idx, "neutral", 10.0, 30.0, "반도체, 2차전지", 10.0, duration_pref),
        )
        conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, source, captured_at) "
            "VALUES(?,?,?,?,?,datetime('now'))", (idx, 9000000, 10000000, 0, "test"),
        )
        conn.commit()
    finally:
        conn.close()


# ---- current_context: 구조 + 정직한 미연동 플래그 ----
def test_current_context_structured_and_honest():
    ctx = mc.current_context()
    for k in ("rate_outlook", "economy", "summary", "source", "data_connected"):
        assert k in ctx, ctx
    # 실데이터 소스 없음 → 정직하게 미연동 + 보수적 기본값(불확실)
    assert ctx["data_connected"] is False, ctx
    assert ctx["rate_outlook"] == "uncertain", ctx
    assert ctx["source"] is None, ctx
    assert "미연동" in ctx["summary"], ctx
    # 가짜 숫자 금지 — 지표 슬롯은 비어 있어야 함
    assert ctx["rates"] == {} and ctx["fx"] == {} and ctx["indices"] == {}, ctx


# ---- recommend_duration: 단기/중기/장기 3종 규칙 ----
def test_recommend_uncertain_is_mixed_with_long_warning():
    ctx = {"rate_outlook": "uncertain", "economy": "uncertain", "data_connected": False}
    rec = mc.recommend_duration(ctx)
    assert rec["recommended"] == "mixed", rec
    assert any("장기" in w for w in rec["warnings"]), rec
    # 미연동 → honest 라벨이 경고에 포함
    assert any("미연동" in w for w in rec["warnings"]), rec


def test_recommend_rising_is_short_with_long_warning():
    ctx = {"rate_outlook": "rising", "economy": "expansion", "data_connected": True}
    rec = mc.recommend_duration(ctx)
    assert rec["recommended"] == "short", rec
    assert any("장기" in w for w in rec["warnings"]), rec
    # 연동된 경우 미연동 라벨은 없어야 함
    assert not any("미연동" in w for w in rec["warnings"]), rec


def test_recommend_falling_slowdown_allows_long():
    ctx = {"rate_outlook": "falling", "economy": "slowdown", "data_connected": True}
    rec = mc.recommend_duration(ctx)
    assert rec["recommended"] == "long", rec


def test_recommend_falling_default_is_intermediate():
    ctx = {"rate_outlook": "falling", "economy": "uncertain", "data_connected": True}
    rec = mc.recommend_duration(ctx)
    assert rec["recommended"] == "intermediate", rec


def test_recommend_three_durations_distinct_and_labeled():
    seen = set()
    for rate, econ in [("rising", "expansion"), ("falling", "slowdown"), ("falling", "uncertain")]:
        rec = mc.recommend_duration({"rate_outlook": rate, "economy": econ, "data_connected": True})
        seen.add(rec["recommended"])
        assert rec["recommended_ko"] in ("단기", "중기", "장기", "사다리(혼합)"), rec
    # 단기/중기/장기 3종이 규칙으로 모두 도달 가능
    assert {"short", "intermediate", "long"} <= seen, seen


def test_recommend_vs_current_compares_pref():
    ctx = {"rate_outlook": "rising", "economy": "expansion", "data_connected": True}
    rec = mc.recommend_duration(ctx, current_pref="long")
    assert rec["vs_current"] and "단기" in rec["vs_current"], rec
    same = mc.recommend_duration(ctx, current_pref="short")
    assert "일치" in (same["vs_current"] or ""), same


# ---- save_snapshot ----
def test_save_snapshot_inserts_row():
    ctx = mc.current_context()
    sid = mc.save_snapshot(ctx)
    assert isinstance(sid, int) and sid >= 1, sid
    conn = store_db.connect()
    try:
        row = conn.execute("SELECT * FROM market_context_snapshots WHERE id=?", (sid,)).fetchone()
    finally:
        conn.close()
    assert row is not None, "snapshot 미적재"
    assert "미연동" in (row["summary"] or ""), dict(row)


# ---- generate_review 통합: duration_recommendation + market_context_id ----
def test_generate_review_includes_duration_recommendation():
    _profile(31, duration_pref="short")
    alloc.generate(31)  # selected 없어도 관망 — duration 추천은 모든 분기에 포함
    r = dr.generate_review(31)
    assert r["ok"], r
    assert r.get("market_context_id"), "market_context_id 미설정"
    dr_block = r.get("duration_recommendation")
    assert dr_block and dr_block["recommended"] in ("short", "intermediate", "long", "mixed"), dr_block
    # 미연동 honest flag
    assert dr_block["data_connected"] is False, dr_block
    assert "no_data_note" in dr_block and "미연동" in dr_block["no_data_note"], dr_block
    # DB row 에도 market_context_id 가 기록됨
    conn = store_db.connect()
    try:
        row = conn.execute(
            "SELECT market_context_id, payload FROM daily_portfolio_reviews WHERE account_index=?", (31,)
        ).fetchone()
    finally:
        conn.close()
    assert row["market_context_id"] == r["market_context_id"], dict(row)
    assert "duration_recommendation" in (row["payload"] or ""), "payload 에 추천 누락"


def test_generate_review_no_snapshot_still_has_duration():
    # 스냅샷 없는 계좌도 듀레이션 추천은 매 점검마다 산출돼야 함(지속 추천).
    r = dr.generate_review(98)
    assert r["ok"], r
    assert r.get("duration_recommendation"), r
    assert r.get("market_context_id"), r


def test_latest_roundtrip_has_duration():
    _profile(32, duration_pref="mixed")
    dr.generate_review(32)
    last = dr.latest(32)
    assert last and isinstance(last.get("payload"), dict), last
    assert "duration_recommendation" in last["payload"], last["payload"]
    assert last.get("market_context_id"), last


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} MARKET-CONTEXT TESTS PASSED")
