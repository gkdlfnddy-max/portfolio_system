"""하락 징후 → policy draft + confidence별 판단 강도 + Daily Review 연결 테스트.

검증(불변 안전):
  - confidence별 판단 강도: confidence<0.3 에서 강한 조언(단정) 안 나옴(관망/주의·후보로만).
  - policy draft: 보수적 전환 → draft 생성(requires_user_approval, auto_applied=false).
  - **자동 적용 차단**: draft 저장해도 policy.compile_policy 결과 불변(accepted 만 읽음).
  - 자동주문 0 (어떤 경로도 order/scheduled_order 생성 없음).
  - Daily Review 하락 징후 섹션: broker-neutral, 일봉 없으면 not_enough_data 정직 표기.
  (임시 SQLite, Anthropic API 미사용)
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_decline_policy.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import decline_scan as scan_mod
from main_mission.portfolio_os import decline_policy_draft as draft_mod
from main_mission.portfolio_os import policy as policy_mod
from main_mission.portfolio_os import daily_review as dr


_PREV_PATH = None


def setup_function(_fn=None):
    # 매 테스트마다 이 모듈 DB 로 재핀(다른 모듈이 SQLITE_PATH 를 바꿔도 격리 유지).
    global _PREV_PATH
    _PREV_PATH = os.environ.get("SQLITE_PATH")
    os.environ["SQLITE_PATH"] = _TMP
    store_db.init()


def teardown_function(_fn=None):
    # 이전 경로 복원 — 다음 모듈(import 시점 경로에 의존하는 NOPIN 모듈)을 깨지 않게.
    if _PREV_PATH is not None:
        os.environ["SQLITE_PATH"] = _PREV_PATH


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
    up = [100.0 + i for i in range(60)]
    peak = up[-1]
    return _bars(up + [peak * (1 - 0.03 * (k + 1)) for k in range(15)])


def _profile(idx, cash_min=10.0, cash_max=30.0):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
            "interests_text, updated_at) VALUES(?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(account_index) DO UPDATE SET cash_min_pct=excluded.cash_min_pct, "
            "cash_max_pct=excluded.cash_max_pct",
            (idx, "neutral", cash_min, cash_max, "반도체"))
        conn.commit()
    finally:
        conn.close()


# ============================================================
# 1. confidence별 판단 강도
# ============================================================
def test_confidence_judgment_tiers():
    # < 0.3 → insufficient: 단정 금지(assert_ok False, candidate_only)
    j_low = scan_mod.confidence_judgment(0.1)
    assert j_low["tier"] == "insufficient" and j_low["assert_ok"] is False
    assert j_low["allowed_strength"] == "candidate_only", j_low
    # None(미상)도 보수적으로 insufficient 취급
    assert scan_mod.confidence_judgment(None)["allowed_strength"] == "candidate_only"
    # 0.3~0.6 → weak(약한 보수전환), 단정은 아직 금지
    j_mid = scan_mod.confidence_judgment(0.45)
    assert j_mid["tier"] == "weak" and j_mid["assert_ok"] is False
    assert j_mid["allowed_strength"] == "weak", j_mid
    # ≥0.6 → moderate(비교적 강한 보수전환 가능, 단 사람 승인)
    j_hi = scan_mod.confidence_judgment(0.8)
    assert j_hi["tier"] == "moderate" and j_hi["assert_ok"] is True
    assert j_hi["allowed_strength"] == "moderate", j_hi


def test_low_confidence_no_strong_advice():
    """6축 대부분 미연동(기술축만) → confidence 낮음 → 강한 조언(단정) 금지."""
    crash = _crash_history()
    insts = [{"instrument_code": f"LC{i}", "sector": "반도체", "history": crash} for i in range(3)]
    out = scan_mod.scan(insts, account_index=901, current_cash_band={"min": 10.0, "max": 30.0})
    prop = out["proposal"]
    assert prop is not None, out["summary"]
    cj = prop["confidence_judgment"]
    if cj["allowed_strength"] == "candidate_only":
        # 단정 금지: asserted False, strength=candidate, 강한 조언(헤지/위험축소 단정) 없음
        assert prop["asserted"] is False, prop
        assert prop["strength"] == "candidate", prop
        assert prop["reduce_risk_assets"] is False, prop
        assert prop["consider_hedge"] is False, prop
        # 허용 행동은 관망/주의·데이터 추가 수준만
        assert "관망" in prop["allowed_actions"] and "리스크 경고" in prop["allowed_actions"]
        assert all("매수" not in a and "매도" not in a for a in prop["allowed_actions"]), prop


# ============================================================
# 2. policy draft 흐름 + 자동 적용 차단
# ============================================================
def test_draft_requires_approval_and_not_auto_applied():
    prop = {"action": "shift_conservative", "strength": "moderate",
            "suggested_cash_band": {"min": 25.0, "max": 45.0, "from": {"min": 10.0, "max": 30.0}},
            "reduce_risk_assets": True, "consider_hedge": False, "asserted": True,
            "overall_confidence": 0.7,
            "confidence_judgment": {"tier": "moderate", "assert_ok": True,
                                    "allowed_strength": "moderate"},
            "allowed_actions": ["관망"], "rationale": "테스트"}
    draft = draft_mod.build_draft(prop, account_index=902)
    assert draft["has_draft"] is True
    assert draft["auto_applied"] is False, draft
    assert draft["requires_user_approval"] is True, draft
    assert draft["status"] == "draft", draft
    # 현금밴드 상향 후보가 proposed_changes 에 들어감
    kinds = [c["kind"] for c in draft["proposed_changes"]]
    assert "cash_band_raise_candidate" in kinds, draft


def test_no_proposal_means_no_draft():
    draft = draft_mod.build_draft(None, account_index=903)
    assert draft["has_draft"] is False
    assert draft["auto_applied"] is False and draft["requires_user_approval"] is True


def test_saved_draft_does_not_change_policy():
    """draft 저장(open) 해도 compile_policy 의 cash_band 는 그대로 — 자동 적용 절대 차단 증거."""
    idx = 904
    _profile(idx, cash_min=10.0, cash_max=30.0)
    before = policy_mod.compile_policy(idx)["cash_band"]

    prop = {"action": "shift_conservative", "strength": "moderate",
            "suggested_cash_band": {"min": 20.0, "max": 40.0, "from": {"min": 10.0, "max": 30.0}},
            "reduce_risk_assets": True, "consider_hedge": False, "asserted": True,
            "overall_confidence": 0.7,
            "confidence_judgment": {"tier": "moderate", "assert_ok": True,
                                    "allowed_strength": "moderate"},
            "allowed_actions": ["관망"], "rationale": "테스트"}
    draft = draft_mod.build_draft(prop, account_index=idx)
    saved = draft_mod.save_draft(idx, draft)
    assert saved["saved"] >= 1 and saved["auto_applied"] is False
    assert saved["requires_user_approval"] is True

    # 저장 후에도 정책 cash_band 불변(승인 전 = 미반영)
    after = policy_mod.compile_policy(idx)["cash_band"]
    assert after == before, (before, after)

    # 저장된 draft 는 미승인(open) 상태
    drafts = draft_mod.list_drafts(idx)
    assert drafts and all(d["status"] == "open" for d in drafts), drafts


def test_no_orders_created_anywhere():
    """draft 생성·저장 어떤 경로에서도 order / scheduled_order 생성 0."""
    idx = 905
    _profile(idx)
    prop = {"action": "shift_conservative", "strength": "candidate",
            "suggested_cash_band": {"min": 15.0, "max": 35.0, "from": {"min": 10.0, "max": 30.0}},
            "reduce_risk_assets": False, "consider_hedge": False, "asserted": False,
            "overall_confidence": 0.1,
            "confidence_judgment": {"tier": "insufficient", "assert_ok": False,
                                    "allowed_strength": "candidate_only"},
            "allowed_actions": ["관망"], "rationale": "테스트"}
    def _counts():
        c = store_db.connect()
        try:
            o = c.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
            s = c.execute("SELECT COUNT(*) c FROM scheduled_order_steps").fetchone()["c"]
            return o, s
        finally:
            c.close()

    # 공유 DB 의 절대값이 아니라 draft 작업 전후 **델타 0** 을 검증(자동주문 0).
    o0, s0 = _counts()
    draft = draft_mod.build_draft(prop, account_index=idx)
    draft_mod.save_draft(idx, draft)
    draft_mod.list_drafts(idx)
    o1, s1 = _counts()
    assert (o1 - o0) == 0 and (s1 - s0) == 0, (o0, s0, o1, s1)


# ============================================================
# 3. Daily Review 하락 징후 섹션 (broker-neutral, honest)
# ============================================================
def test_daily_review_decline_section_present_honest():
    """일봉 없는 종목 → not_enough_data 정직 표기, 자동주문 없음, broker-neutral."""
    idx = 906
    _profile(idx)
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, "
            "source, captured_at) VALUES(?,?,?,?,?,datetime('now'))",
            (idx, 9000000, 10000000, 1, "test"))
        snap_id = conn.execute(
            "SELECT id FROM account_snapshots WHERE account_index=? ORDER BY id DESC LIMIT 1",
            (idx,)).fetchone()["id"]
        conn.execute(
            "INSERT INTO holdings(snapshot_id, account_index, ticker, name, qty, avg_price, market_value) "
            "VALUES(?,?,?,?,?,?,?)", (snap_id, idx, "NODATA1", "테스트종목", 10, 1000.0, 10000.0))
        conn.commit()
    finally:
        conn.close()

    r = dr.generate_review(idx)
    assert r["ok"] is True, r
    decline = r.get("decline")
    assert decline is not None, r
    assert decline["auto_order_created"] is False, decline
    # 일봉 없음 → not_enough_data 정직 표기
    names = decline["names"]
    assert names and any(n["status"] == "not_enough_data" for n in names), decline
    # 데이터 없으면 보수전환 제안 없음(관망/유지) — 거짓 경보 금지
    assert decline["proposal"] is None, decline
    assert decline["today_action"] in ("관망", "유지"), decline


def test_daily_review_decline_no_universe():
    """보유/관심 종목 없음 → 스캔 대상 없음, 유지(관망)·자동주문 없음."""
    idx = 907
    _profile(idx)
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, "
            "source, captured_at) VALUES(?,?,?,?,?,datetime('now'))",
            (idx, 10000000, 10000000, 0, "test"))
        conn.commit()
    finally:
        conn.close()
    r = dr.generate_review(idx)
    decline = r.get("decline")
    assert decline is not None and decline["auto_order_created"] is False
    assert decline["scanned_count"] == 0 and decline["today_action"] == "유지", decline
