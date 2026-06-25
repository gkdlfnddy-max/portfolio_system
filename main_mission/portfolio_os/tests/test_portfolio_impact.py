"""포트폴리오 영향 분석 + ETF 겹침 + Daily Review 종합 + 영향 draft 테스트.

검증(불변 안전):
  - 영향 분석: 보유+evidence+user_views+하락신호 → 영향/위험·기회/조정 후보.
  - 견해 vs 데이터 충돌(장기긍정↔단기과열) → mixed_swing 구조 후보(long 유지+분할매수+hedge).
  - confidence 낮으면 약한 후보/관망(단정 금지).
  - ETF 겹침: 반도체ETF+AI ETF 의 공통 종목(NVIDIA/TSMC/Samsung) 계산. 데이터 없으면 정직 미연동.
  - Daily Review 종합 블록: broker-neutral, auto_order_created=false.
  - **자동 적용 차단**: 영향 draft 저장해도 compile_policy 결과 불변(accepted 만 읽음).
  - 자동주문 0 · Anthropic API 미사용.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_impact.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import portfolio_impact as impact_mod
from main_mission.portfolio_os import etf_analysis as etf_mod
from main_mission.portfolio_os import decline_policy_draft as draft_mod
from main_mission.portfolio_os import policy as policy_mod
from main_mission.portfolio_os import price_history as ph
from main_mission.portfolio_os import daily_review as dr

_PREV = None


def setup():
    store_db.init()


def setup_function(_fn=None):
    global _PREV
    _PREV = os.environ.get("SQLITE_PATH")
    os.environ["SQLITE_PATH"] = _TMP
    # 매 테스트 깨끗한 DB(테스트 간 etf_constituents/holdings 누수 차단).
    if os.path.exists(_TMP):
        os.remove(_TMP)
    store_db._bootstrapped = False
    store_db.init()


def teardown_function(_fn=None):
    if _PREV is not None:
        os.environ["SQLITE_PATH"] = _PREV


# ============================================================
# 합성 데이터
# ============================================================
def _bars(closes, *, start="2025-01-01"):
    d0 = date.fromisoformat(start)
    return [{"date": (d0 + timedelta(days=i)).isoformat(),
             "open": round(c, 4), "high": round(c * 1.01, 4),
             "low": round(c * 0.99, 4), "close": round(c, 4), "volume": 1000.0}
            for i, c in enumerate(closes)]


def _crash_history():
    """상승 후 급락 — 하락 징후 강하게 발화."""
    up = [100.0 + i for i in range(60)]
    peak = up[-1]
    return _bars(up + [peak * (1 - 0.03 * (k + 1)) for k in range(15)])


def _calm_history():
    """완만 상승 — 하락 징후 약함."""
    return _bars([100.0 + i * 0.1 for i in range(80)])


def _profile(conn, idx, *, cmin=10.0, cmax=30.0):
    conn.execute(
        "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
        "interests_text, updated_at) VALUES(?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(account_index) DO NOTHING",
        (idx, "neutral", cmin, cmax, "반도체"))


def _snapshot_with_holding(conn, idx, ticker, *, asset_class="semiconductor_etf"):
    cur = conn.execute(
        "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, source, captured_at) "
        "VALUES(?,?,?,?,?,datetime('now'))", (idx, 3000000, 10000000, 1, "test"))
    sid = cur.lastrowid
    conn.execute(
        "INSERT INTO holdings(snapshot_id, account_index, ticker, name, qty, avg_price, market_value, captured_at) "
        "VALUES(?,?,?,?,?,?,?,datetime('now'))", (sid, idx, ticker, ticker, 10, 5000, 7000000))
    conn.execute(
        "INSERT INTO universe_instruments(account_index, ticker, name, asset_class, is_active) "
        "VALUES(?,?,?,?,1)", (idx, ticker, ticker, asset_class))
    return sid


def _user_view(conn, idx, **kw):
    conn.execute(
        "INSERT INTO user_views(account_index, layer, theme, ticker, etf, stance, conviction, horizon, note, status) "
        "VALUES(?,?,?,?,?,?,?,?,?, 'active')",
        (idx, kw.get("layer", "mid"), kw.get("theme"), kw.get("ticker"), kw.get("etf"),
         kw.get("stance"), kw.get("conviction"), kw.get("horizon"), kw.get("note", "")))


def _evidence(conn, **kw):
    conn.execute(
        "INSERT INTO evidence_items(source_type, source_date, freshness, confidence, related_account, "
        "related_ticker, related_etf, related_theme, summary, positive_factors, negative_factors, stale) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
        (kw.get("source_type", "news"), kw.get("source_date", "2025-03-01"),
         kw.get("freshness", 0.9), kw.get("confidence", 0.7), kw.get("related_account"),
         kw.get("related_ticker"), kw.get("related_etf"), kw.get("related_theme"),
         kw.get("summary", ""), kw.get("positive_factors"), kw.get("negative_factors")))


def _etf_const(conn, etf, ticker, name, weight, *, sector=None, country=None, as_of="2025-03-01"):
    conn.execute(
        "INSERT INTO etf_constituents(etf_ticker, constituent_ticker, constituent_name, weight_pct, "
        "sector, country, as_of, source) VALUES(?,?,?,?,?,?,?,?)",
        (etf, ticker, name, weight, sector, country, as_of, "test"))


# ============================================================
# 1. 영향 분석 — mixed_swing 충돌
# ============================================================
def test_conflict_long_positive_vs_short_decline_mixed_swing():
    """보유 ETF + 사용자 장기긍정 + 단기 하락신호 → 충돌 → mixed_swing 후보(long 유지+분할매수+hedge)."""
    idx = 101
    ph.upsert_bars("SEMI", _crash_history(), "test")  # 하락 신호 강함
    conn = store_db.connect()
    try:
        _profile(conn, idx)
        _snapshot_with_holding(conn, idx, "SEMI")
        _user_view(conn, idx, ticker="SEMI", stance="positive", conviction=0.8, horizon="long",
                   note="장기 구조적 성장")
        _user_view(conn, idx, ticker="SEMI", stance="negative", conviction=0.6, horizon="short",
                   note="단기 과열")
        _evidence(conn, related_account=idx, related_ticker="SEMI", confidence=0.7,
                  negative_factors="고점 신호", summary="단기 과열")
        conn.commit()
    finally:
        conn.close()

    out = impact_mod.analyze_account(idx)
    assert out["ok"]
    semi = next(i for i in out["instrument_impacts"] if i["instrument_code"] == "SEMI")
    assert semi["alignment"] == "conflict", semi
    assert semi["mixed_swing"] is True, semi
    kinds = {c["kind"] for c in semi["adjustment_candidates"]}
    assert "hold_long" in kinds and "staged_buy" in kinds and "consider_hedge" in kinds, kinds
    # 위험/기회 둘 다 표기
    assert semi["risks"] and semi["opportunities"], semi
    assert out["auto_order_created"] is False and out["auto_applied"] is False


# ============================================================
# 2. 신뢰도 낮음 → 약한 후보/관망(단정 금지)
# ============================================================
def test_low_confidence_observe_only():
    idx = 102
    # 일봉 없음(decline 분석 불가) + evidence 없음 → 데이터 신뢰도 None = low.
    conn = store_db.connect()
    try:
        _profile(conn, idx)
        _snapshot_with_holding(conn, idx, "NODATA")
        _user_view(conn, idx, ticker="NODATA", stance="positive", conviction=0.5, horizon="long")
        conn.commit()
    finally:
        conn.close()
    out = impact_mod.analyze_account(idx)
    nd = next(i for i in out["instrument_impacts"] if i["instrument_code"] == "NODATA")
    assert nd["low_confidence"] is True, nd
    kinds = {c["kind"] for c in nd["adjustment_candidates"]}
    assert "observe" in kinds, kinds
    # 강한 단정(reduce_risk/consider_hedge moderate) 금지
    assert not any(c["kind"] == "reduce_risk" for c in nd["adjustment_candidates"]), nd


# ============================================================
# 3. ETF 겹침 — 반도체ETF + AI ETF 공통 종목
# ============================================================
def test_etf_overlap_shared_constituents():
    conn = store_db.connect()
    try:
        # SEMI_ETF: NVDA, TSM, SAMSUNG
        _etf_const(conn, "SEMI_ETF", "NVDA", "NVIDIA", 25.0, sector="Tech", country="US")
        _etf_const(conn, "SEMI_ETF", "TSM", "TSMC", 20.0, sector="Tech", country="TW")
        _etf_const(conn, "SEMI_ETF", "SAMSUNG", "삼성전자", 15.0, sector="Tech", country="KR")
        # AI_ETF: NVDA, TSM, MSFT
        _etf_const(conn, "AI_ETF", "NVDA", "NVIDIA", 30.0, sector="Tech", country="US")
        _etf_const(conn, "AI_ETF", "TSM", "TSMC", 10.0, sector="Tech", country="TW")
        _etf_const(conn, "AI_ETF", "MSFT", "Microsoft", 12.0, sector="Tech", country="US")
        conn.commit()
    finally:
        conn.close()
    ov = etf_mod.overlap("SEMI_ETF", "AI_ETF")
    assert ov["data_connected"] is True
    shared = {s["ticker"] for s in ov["shared"]}
    assert shared == {"NVDA", "TSM"}, shared
    # min_overlap: NVDA min(25,30)=25, TSM min(20,10)=10 → 35
    assert ov["overlap_weight_pct"] == 35.0, ov
    assert ov["concentration_flag"] is True


def test_etf_overlap_no_data_honest():
    ov = etf_mod.overlap("UNKNOWN_A", "UNKNOWN_B")
    assert ov["data_connected"] is False and ov["shared_count"] == 0


def test_analyze_etf_exposure_and_account():
    idx = 103
    conn = store_db.connect()
    try:
        _profile(conn, idx)
        _etf_const(conn, "SEMI_ETF", "NVDA", "NVIDIA", 25.0, sector="Tech", country="US")
        _etf_const(conn, "SEMI_ETF", "TSM", "TSMC", 20.0, sector="Tech", country="TW")
        _etf_const(conn, "AI_ETF", "NVDA", "NVIDIA", 30.0, sector="Tech", country="US")
        conn.execute("INSERT INTO universe_instruments(account_index, ticker, name, asset_class, is_active) "
                     "VALUES(?,?,?,?,1)", (idx, "SEMI_ETF", "반도체ETF", "semiconductor_etf"))
        conn.execute("INSERT INTO universe_instruments(account_index, ticker, name, asset_class, is_active) "
                     "VALUES(?,?,?,?,1)", (idx, "AI_ETF", "AI ETF", "ai_etf"))
        conn.commit()
    finally:
        conn.close()
    single = etf_mod.analyze_etf("SEMI_ETF")
    assert single["data_connected"] and single["constituent_count"] == 2
    assert single["sector_exposure"][0]["sector"] == "Tech"
    acct = etf_mod.analyze_account_etfs(idx)
    assert acct["etf_count"] == 2 and acct["overlaps"], acct
    assert acct["overlaps"][0]["shared_count"] >= 1


# ============================================================
# 4. Daily Review 종합 블록 — broker-neutral, auto_order_created=false
# ============================================================
def test_daily_review_synthesis_block():
    idx = 104
    ph.upsert_bars("SEMI", _crash_history(), "test")
    conn = store_db.connect()
    try:
        _profile(conn, idx)
        _snapshot_with_holding(conn, idx, "SEMI")
        _user_view(conn, idx, ticker="SEMI", stance="positive", conviction=0.8, horizon="long")
        _user_view(conn, idx, ticker="SEMI", stance="negative", conviction=0.6, horizon="short")
        conn.commit()
    finally:
        conn.close()
    r = dr.generate_review(idx)
    assert r["ok"], r
    syn = r.get("synthesis")
    assert syn is not None, r
    assert syn["auto_order_created"] is False and syn["auto_applied"] is False
    # 종합 블록 구성 요소 존재
    for k in ("view_vs_data", "today_adjustment_candidates", "not_doing_today",
              "need_more_confirmation", "etf_overlaps"):
        assert k in syn, k
    assert syn["requires_user_approval"] is True


# ============================================================
# 5. 영향 draft 저장해도 compile_policy 불변(자동 적용 차단)
# ============================================================
def test_impact_draft_does_not_change_compile_policy():
    idx = 105
    ph.upsert_bars("SEMI", _crash_history(), "test")
    conn = store_db.connect()
    try:
        _profile(conn, idx, cmin=10.0, cmax=30.0)
        _snapshot_with_holding(conn, idx, "SEMI")
        _user_view(conn, idx, ticker="SEMI", stance="positive", conviction=0.8, horizon="long")
        _user_view(conn, idx, ticker="SEMI", stance="negative", conviction=0.6, horizon="short")
        _evidence(conn, related_account=idx, related_ticker="SEMI", confidence=0.7,
                  negative_factors="고점")
        conn.commit()
    finally:
        conn.close()

    before = policy_mod.compile_policy(idx)
    res = draft_mod.generate_impact_draft_and_save(idx)
    assert res["draft"]["has_draft"] is True, res
    assert res["saved"]["saved"] >= 1, res
    assert res["auto_applied"] is False and res["auto_order_created"] is False
    # draft 는 status=open 으로만 저장됨
    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT status, source FROM advice_items WHERE account_index=? AND source=?",
            (idx, draft_mod.IMPACT_SOURCE)).fetchall()
        assert rows and all(r["status"] == "open" for r in rows), [dict(r) for r in rows]
    finally:
        conn.close()
    # compile_policy(accepted 만 읽음) 결과 불변 → 자동 적용 차단 증거.
    # compiled_at(타임스탬프) 만 제외하고 비교(정책 본문은 완전 동일해야 함).
    after = policy_mod.compile_policy(idx)
    b = {k: v for k, v in before.items() if k != "compiled_at"}
    a = {k: v for k, v in after.items() if k != "compiled_at"}
    assert b["cash_band"] == a["cash_band"], (before, after)
    assert b == a, "draft 저장이 policy 를 바꿈(자동 적용 금지 위반)"


def test_impact_draft_respects_rejected():
    """거절된 영향 draft 는 재저장(강요) 안 함."""
    idx = 106
    ph.upsert_bars("SEMI", _crash_history(), "test")
    conn = store_db.connect()
    try:
        _profile(conn, idx)
        _snapshot_with_holding(conn, idx, "SEMI")
        _user_view(conn, idx, ticker="SEMI", stance="positive", conviction=0.8, horizon="long")
        _user_view(conn, idx, ticker="SEMI", stance="negative", conviction=0.6, horizon="short")
        _evidence(conn, related_account=idx, related_ticker="SEMI", confidence=0.7, negative_factors="고점")
        conn.commit()
    finally:
        conn.close()
    res = draft_mod.generate_impact_draft_and_save(idx)
    first_id = res["saved"]["advice_ids"][0]
    conn = store_db.connect()
    try:
        conn.execute("UPDATE advice_items SET status='rejected' WHERE id=?", (first_id,))
        conn.commit()
    finally:
        conn.close()
    draft_mod.generate_impact_draft_and_save(idx)  # 재실행
    conn = store_db.connect()
    try:
        st = conn.execute("SELECT status FROM advice_items WHERE id=?", (first_id,)).fetchone()
        assert st["status"] == "rejected", st  # 거절 유지(반복 강요 금지)
    finally:
        conn.close()


# ============================================================
# 6. Anthropic API 미사용
# ============================================================
def test_no_anthropic_import():
    import pathlib
    for mod in ("portfolio_impact.py", "etf_analysis.py"):
        text = pathlib.Path(__file__).resolve().parents[1].joinpath(mod).read_text(encoding="utf-8")
        low = text.lower()
        assert "import anthropic" not in low
        assert "from anthropic" not in low
        assert "anthropic-ai" not in low
        assert "ANTHROPIC_API_KEY" not in text
