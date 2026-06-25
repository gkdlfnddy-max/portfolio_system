"""Daily Review 통합 강화 테스트 — 관점/거시/관점별 A·B·C/물어볼 질문/보수적 후보 통합.

검증 핵심(불변):
  - 추가 섹션이 모든 분기(watch/hold/rebalance)에서 정직하게 존재한다.
  - 병렬 모듈(macro_connect=미존재, evidence_summary=존재)이 늦게 와도 graceful(안 깨짐).
  - **자동주문 0 · 자동 policy 0 · broker-neutral · 사용자 승인 필요** 표기 유지.
  - 데이터 없으면 정직("미연동/데이터 없음") + confidence 낮춤.
  - '오늘 물어볼 질문'은 단정이 아니라 선택지 질문(options 존재).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

# 신규 SQLITE_PATH 핀(다른 테스트 파일과 격리) — import 전에 설정.
_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_dailyreview_integration.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import allocation as alloc
from main_mission.portfolio_os import selection as sel
from main_mission.portfolio_os import daily_review as dr
from main_mission.portfolio_os import user_views as uv_mod
from main_mission.portfolio_os import investor_objective as io_mod
from main_mission.portfolio_os import evidence_summary as es_mod


def setup():
    os.environ["SQLITE_PATH"] = _TMP
    store_db.init()


def setup_function(_fn=None):
    # 매 테스트마다 이 모듈 DB 로 재핀(다른 모듈이 SQLITE_PATH 를 바꿔도 격리 유지).
    os.environ["SQLITE_PATH"] = _TMP
    store_db.init()


def _profile(idx, *, interests="반도체, 2차전지"):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
            "interests_text, updated_at) VALUES(?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(account_index) DO NOTHING",
            (idx, "neutral", 10.0, 30.0, interests),
        )
        conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, "
            "source, captured_at) VALUES(?,?,?,?,?,datetime('now'))",
            (idx, 9000000, 10000000, 0, "test"),
        )
        conn.commit()
    finally:
        conn.close()


def _select(idx):
    out = alloc.generate(idx)
    sel.select(idx, out["proposal_id"], "base")


# ── 핵심 1: 모든 통합 섹션이 존재(스냅샷 없는 watch 분기에서도) ──
def test_all_integration_sections_present_on_watch():
    r = dr.generate_review(910)  # 스냅샷 없음 → watch
    assert r["ok"] and r["action_decision"] == "watch", r
    for key in ("user_views", "macro", "perspective_variants", "evidence_summary",
                "conservative_candidates", "today_questions", "integration_confidence"):
        assert r.get(key) is not None, (key, r)
    # 자동주문 0 · 승인 필요 표기.
    assert r["auto_order_created"] is False, r
    assert r["requires_user_approval"] is True, r


def test_integration_sections_present_on_full_path():
    _profile(911)
    _select(911)
    r = dr.generate_review(911)
    assert r["ok"], r
    for key in ("user_views", "macro", "perspective_variants", "evidence_summary",
                "conservative_candidates", "today_questions", "integration_confidence"):
        assert r.get(key) is not None, (key, r)


# ── 핵심 2: macro 미연동(병렬 B 미도착) graceful ──
def test_macro_graceful_when_not_connected():
    _profile(912)
    r = dr.generate_review(912)
    macro = r["macro"]
    assert macro["connected"] is False, macro
    assert "미연동" in macro["note"], macro
    assert macro["changes"] == [], macro  # 거짓 거시 수치 생성 금지


# ── 핵심 3: 사용자 관점 요약 — 견해/목적 있으면 반영, 없으면 정직 ──
def test_user_views_reflected_when_present():
    idx = 913
    _profile(idx)
    uv_mod.add(idx, layer="long", theme="반도체", stance="positive",
               conviction=0.7, note="장기 긍정")
    io_mod.set_objective(idx, {"investment_goal": "loss_reduction", "risk_tolerance": "low"})
    r = dr.generate_review(idx)
    u = r["user_views"]
    assert u["has_views"] is True and u["views_count"] >= 1, u
    assert u["objective_set"] is True, u


def test_user_views_honest_when_empty():
    idx = 914
    _profile(idx)
    r = dr.generate_review(idx)
    u = r["user_views"]
    assert u["has_views"] is False and u["objective_set"] is False, u
    assert "입력" in u["note"], u


# ── 핵심 4: 관점별 A/B/C 후보 — 자동 적용/주문 0 ──
def test_perspective_variants_candidates_no_auto():
    idx = 915
    _profile(idx)
    r = dr.generate_review(idx)
    p = r["perspective_variants"]
    assert p.get("auto_order_created") is False, p
    assert p.get("requires_user_approval") is True, p
    if p.get("connected"):
        labels = {c["perspective"] for c in p["candidates"]}
        assert labels == {"A", "B", "C"} or labels <= {"A", "B", "C"}, p
    # 후보를 draft 로 저장하지 않았는지 확인(save_draft=False) — target_allocations status='draft' 없음.
    conn = store_db.connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM target_allocations WHERE account_index=? AND status='draft'",
            (idx,)).fetchone()["c"]
    finally:
        conn.close()
    assert n == 0, f"daily_review 의 관점블록은 draft 저장 금지인데 {n}행 생김"


# ── 핵심 5: 오늘 물어볼 질문 — 선택지 질문(단정 아님) ──
def test_today_questions_are_choice_questions():
    idx = 916
    _profile(idx)
    r = dr.generate_review(idx)
    q = r["today_questions"]
    assert q["count"] >= 1, q
    for item in q["questions"]:
        assert item.get("question"), item
        assert isinstance(item.get("options"), list) and len(item["options"]) >= 1, item
    # 관점/목적 미입력 계좌이므로 입력 요청 질문이 들어가야 함.
    topics = {i["topic"] for i in q["questions"]}
    assert "missing_views" in topics or "missing_objective" in topics, q


# ── 핵심 6: confidence 데이터 부족 시 낮춤 ──
def test_confidence_low_when_no_snapshot():
    r = dr.generate_review(917)  # 스냅샷 없음
    c = r["integration_confidence"]
    assert c["level"] in ("low", "medium"), c
    assert c["penalties"] >= 2, c  # 스냅샷 없음 패널티 포함


# ── 핵심 7: evidence_summary 연동(존재 모듈) graceful + honest ──
def test_evidence_summary_block_present():
    idx = 918
    _profile(idx)
    _select(idx)
    r = dr.generate_review(idx)
    es = r["evidence_summary"]
    assert es is not None, r
    # 모듈 존재하므로 connected True, 자료 없으면 honest 빈 목록.
    assert es.get("connected") is True, es
    assert "items" in es, es


# ── 핵심 8: 자동주문 0 / broker-neutral 불변(통합 후에도) ──
def test_no_auto_order_anywhere():
    idx = 919
    _profile(idx)
    _select(idx)
    r = dr.generate_review(idx)
    assert r["auto_order_created"] is False, r
    # 어떤 주문 step 도 체결 상태가 아니어야 함.
    conn = store_db.connect()
    try:
        bad = conn.execute(
            "SELECT COUNT(*) c FROM scheduled_order_steps s "
            "JOIN scheduled_order_plans p ON p.id=s.plan_id "
            "WHERE p.account_index=? AND s.status NOT IN ('candidate','hold','blocked')",
            (idx,)).fetchone()["c"]
    finally:
        conn.close()
    assert bad == 0, "통합 후에도 자동 주문 체결 금지"


# ── 핵심 9: 보수적 전환 후보 — 전부 후보(자동 적용 0) ──
def test_conservative_candidates_are_candidates_only():
    idx = 920
    _profile(idx)
    _select(idx)
    r = dr.generate_review(idx)
    cc = r["conservative_candidates"]
    assert cc.get("auto_order_created") is False, cc
    assert cc.get("requires_user_approval") is True, cc
    assert isinstance(cc.get("candidates"), list), cc


# ── 핵심 10: latest() 로 통합 섹션 round-trip(payload JSON 보존) ──
def test_latest_roundtrip_keeps_integration():
    idx = 921
    _profile(idx)
    _select(idx)
    dr.generate_review(idx)
    last = dr.latest(idx)
    assert last and isinstance(last.get("payload"), dict), last
    pl = last["payload"]
    for key in ("user_views", "macro", "perspective_variants", "today_questions",
                "integration_confidence", "conservative_candidates"):
        assert key in pl, (key, list(pl.keys()))
    assert pl.get("auto_order_created") is False, pl
    assert pl.get("broker_neutral") is True, pl


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} DAILY-REVIEW-INTEGRATION TESTS PASSED")
