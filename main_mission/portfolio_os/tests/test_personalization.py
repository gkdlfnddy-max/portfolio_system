"""통합 개인화 루프(Track A) 테스트 — 선택/무시/수정 → 다음 조언 ranking 반영.

검증:
  - record_feedback 가 accepted/ignored/modified 를 구분해 저장(upsert·카운트++)
  - weight 산식: 선택多→>1, 무시多→<1, 평활(한두 표본으로 과격 변동 X)
  - rank() 가 개인화 가중을 표시순서에 반영(반복 무시 하향·선호 상향)
  - **다음 조언 ranking 변화**: theme_suggestions 가 무시한 유형/테마를 하향
  - **계좌 격리**: 한 계좌 선호가 다른 계좌에 미반영
  - **공통 lessons 와 분리**: personalization_weights 만 갱신
  - **자동 주문/policy 0**: ranking 만 바뀌고 applied_to_policy=0 유지
  - Anthropic API 미사용
"""
from __future__ import annotations

import importlib
import os
import tempfile

# 신규 임시 SQLite 핀 (다른 테스트 모듈과 분리) — conftest 가 DB_BACKEND=sqlite 강제.
_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_personalization.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import personalization as pz
from main_mission.portfolio_os import theme_suggestions as ts
from main_mission.portfolio_os import profile as profile_mod


def setup():
    store_db.init()


def _seed(account_index: int, interests: str, *, posture: str = "", views: str = ""):
    profile_mod.save(account_index, {"interests_text": interests, "posture_text": posture,
                                     "views_text": views})


# ---- NO Anthropic import (CLAUDE.md §17) ----
def test_no_anthropic_import():
    src = importlib.util.find_spec("main_mission.portfolio_os.personalization").origin
    with open(src, encoding="utf-8") as f:
        text = f.read()
    low = text.lower()
    assert "import anthropic" not in low
    assert "from anthropic" not in low
    assert "anthropic-ai" not in low
    assert "ANTHROPIC_API_KEY" not in text
    assert "claude-" not in low


# ---- weight 산식 ----
def test_compute_weight_neutral_when_no_samples():
    # 표본 없으면 중립 1.0 근처
    assert abs(pz.compute_weight(0, 0, 0) - 1.0) < 1e-9


def test_compute_weight_accept_above_one():
    w_few = pz.compute_weight(1, 0, 0)
    w_many = pz.compute_weight(10, 0, 0)
    assert w_few > 1.0
    assert w_many > w_few  # 선택 누적될수록 상향


def test_compute_weight_ignore_below_one():
    w_few = pz.compute_weight(0, 1, 0)
    w_many = pz.compute_weight(0, 10, 0)
    assert w_few < 1.0
    assert w_many < w_few  # 무시 누적될수록 하향


def test_compute_weight_smoothing_not_extreme():
    # 한 번의 신호로 0/2 같은 극단으로 튀지 않음(평활)
    w = pz.compute_weight(1, 0, 0)
    assert 1.0 < w < 1.5


def test_modified_is_weak_positive():
    # 수정은 약한 긍정 — 무시보다는 위, 선택보다는 아래
    w_mod = pz.compute_weight(0, 0, 1)
    w_acc = pz.compute_weight(1, 0, 0)
    w_ign = pz.compute_weight(0, 1, 0)
    assert w_ign < w_mod
    assert w_mod < w_acc


# ---- record_feedback: 저장·구분 ----
def test_record_feedback_distinguishes_actions():
    pz.record_feedback(100, "candidate_type", "hedge", "accepted")
    pz.record_feedback(100, "candidate_type", "hedge", "ignored")
    pz.record_feedback(100, "candidate_type", "hedge", "modified")
    conn = store_db.connect()
    try:
        row = conn.execute(
            "SELECT accepted_count, ignored_count, modified_count FROM personalization_weights "
            "WHERE account_index=100 AND scope='candidate_type' AND key='hedge'",
        ).fetchone()
    finally:
        conn.close()
    assert row["accepted_count"] == 1
    assert row["ignored_count"] == 1
    assert row["modified_count"] == 1


def test_record_feedback_upsert_increments():
    pz.record_feedback(101, "theme", "반도체", "accepted")
    r = pz.record_feedback(101, "theme", "반도체", "accepted")
    assert r["ok"]
    assert r["accepted_count"] == 2  # upsert 누적
    # 단일 행(UNIQUE) — 중복 INSERT 안 함
    conn = store_db.connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM personalization_weights WHERE account_index=101 AND scope='theme' AND key='반도체'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_record_feedback_reason_stored():
    r = pz.record_feedback(102, "theme", "바이오", "ignored", reason="임상 리스크 싫음")
    assert r["ok"]
    assert pz.weight_for(102, "theme", "바이오") < 1.0
    conn = store_db.connect()
    try:
        row = conn.execute(
            "SELECT last_reason FROM personalization_weights WHERE account_index=102 AND scope='theme' AND key='바이오'"
        ).fetchone()
    finally:
        conn.close()
    assert row["last_reason"] == "임상 리스크 싫음"


def test_record_feedback_account_required_hard_block():
    out = pz.record_feedback(None, "theme", "반도체", "accepted")
    assert not out["ok"]
    assert out.get("gate") == "block"


def test_record_feedback_invalid_scope_and_action():
    assert not pz.record_feedback(103, "bogus_scope", "x", "accepted")["ok"]
    assert not pz.record_feedback(103, "theme", "x", "bogus_action")["ok"]


# ---- weight_for ----
def test_weight_for_default_neutral():
    assert pz.weight_for(999, "theme", "없는테마") == 1.0
    assert pz.weight_for(None, "theme", "x") == 1.0


# ---- rank: 표시순서 반영 ----
def test_rank_prefers_accepted_demotes_ignored():
    pz.record_feedback(110, "candidate_type", "defensive", "accepted")
    pz.record_feedback(110, "candidate_type", "defensive", "accepted")
    for _ in range(3):
        pz.record_feedback(110, "candidate_type", "aggressive", "ignored")
    items = [
        {"key": "aggressive", "confidence": 0.6},
        {"key": "defensive", "confidence": 0.6},
    ]
    ranked = pz.rank(110, "candidate_type", items)
    assert ranked[0]["key"] == "defensive"   # 선호 상향
    assert ranked[-1]["key"] == "aggressive"  # 반복 무시 하향
    # 비파괴: personalized_score/weight 부가, 원본 confidence 보존
    assert ranked[0]["confidence"] == 0.6
    assert ranked[0]["personalization_weight"] > 1.0


def test_rank_stable_when_no_history():
    items = [{"key": "a", "confidence": 0.5}, {"key": "b", "confidence": 0.5}]
    ranked = pz.rank(111, "theme", items)
    assert [r["key"] for r in ranked] == ["a", "b"]  # 이력 없으면 원래 순서


def test_rank_empty_safe():
    assert pz.rank(112, "theme", []) == []


# ---- 계좌 격리 ----
def test_account_isolation_weights():
    pz.record_feedback(200, "theme", "반도체", "accepted")
    pz.record_feedback(200, "theme", "반도체", "accepted")
    # 계좌 201 은 영향 없음
    assert pz.weight_for(200, "theme", "반도체") > 1.0
    assert pz.weight_for(201, "theme", "반도체") == 1.0


def test_account_isolation_rank():
    for _ in range(3):
        pz.record_feedback(210, "candidate_type", "hedge", "ignored")
    items = [{"key": "hedge", "confidence": 0.5}, {"key": "core", "confidence": 0.5}]
    # 계좌 210: hedge 반복 무시 → 하향
    r210 = pz.rank(210, "candidate_type", items)
    assert r210[-1]["key"] == "hedge"
    # 계좌 211: 이력 없음 → 원래 순서(타 계좌 미반영)
    r211 = pz.rank(211, "candidate_type", items)
    assert [x["key"] for x in r211] == ["hedge", "core"]


# ---- 공통 lessons 와 분리 ----
def test_separated_from_agent_lessons():
    # personalization 기록은 lessons 테이블을 건드리지 않는다(개인/공통 분리).
    conn = store_db.connect()
    try:
        before = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    finally:
        conn.close()
    pz.record_feedback(220, "theme", "로봇", "accepted")
    conn = store_db.connect()
    try:
        after = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        n_pz = conn.execute(
            "SELECT COUNT(*) FROM personalization_weights WHERE account_index=220"
        ).fetchone()[0]
    finally:
        conn.close()
    assert after == before  # lessons 불변
    assert n_pz == 1        # personalization 에만 기록


# ============================================================
# theme_suggestions 통합: 다음 조언 ranking 변화
# ============================================================

def test_suggestion_ranking_changes_after_ignore():
    # 계좌가 'adjacent' 유형 후보(보통 상단)를 반복 무시 → 다음 제안에서 하향(개인화 가중).
    # adjacent 는 base_conf 가 가장 높아(0.62) 초기엔 상단 → 개인화 하향 효과가 위치로 드러남.
    _seed(300, "반도체, 바이오")
    first = ts.suggest(300)
    assert first["ok"], first
    adj = [c for c in first["candidates"]
           if c["candidate_type"] == "adjacent" and not c["deprioritized"]]
    assert adj, "adjacent 후보가 있어야 테스트 가능"
    # 무시는 candidate_type='hedge' 가 아닌 adjacent 가중을 떨어뜨림 — 단, theme 무시도 누적되어
    # 반복-무시(deprioritized)가 위치를 지배하지 않도록 *서로 다른* adjacent 후보를 1회씩만 무시.
    for c in adj:
        ts.record_action(c["id"], 300, "ignored")
    # candidate_type='adjacent' 가중이 1 미만으로 내려갔는지(개인화 학습)
    assert pz.weight_for(300, "candidate_type", "adjacent") < 1.0

    # 두 번째 제안: adjacent 후보의 평균 위치가 첫 제안보다 뒤로 밀려야 함(표시순서 변화).
    second = ts.suggest(300)

    def _avg_pos(out, ctype):
        idxs = [i for i, c in enumerate(out["candidates"]) if c["candidate_type"] == ctype]
        return sum(idxs) / len(idxs) if idxs else None

    p1 = _avg_pos(first, "adjacent")
    p2 = _avg_pos(second, "adjacent")
    assert p1 is not None and p2 is not None
    assert p2 > p1, (p1, p2)  # 무시 후 adjacent 가 더 뒤로


def test_suggestion_accepted_type_promoted():
    # diversify 후보를 채택(added_to_research) → diversify 가중 상향 → 다음 제안 상향.
    _seed(301, "로봇")
    first = ts.suggest(301)
    div = [c for c in first["candidates"] if c["candidate_type"] == "diversify"]
    assert div
    ts.record_action(div[0]["id"], 301, "added_to_research")
    assert pz.weight_for(301, "candidate_type", "diversify") > 1.0


def test_suggestion_personalization_account_isolated():
    # 계좌 310 의 무시가 계좌 311 제안 순서에 영향 없어야 함.
    _seed(310, "반도체")
    _seed(311, "반도체")
    out310 = ts.suggest(310)
    for c in [x for x in out310["candidates"] if x["candidate_type"] == "hedge"]:
        ts.record_action(c["id"], 310, "ignored")
    assert pz.weight_for(310, "candidate_type", "hedge") < 1.0
    assert pz.weight_for(311, "candidate_type", "hedge") == 1.0  # 격리


def test_suggestion_no_auto_policy_after_personalization():
    # 개인화가 적용돼도 자동 policy/주문 0 — applied_to_policy 는 명시 저장 때만 1.
    _seed(320, "바이오")
    out = ts.suggest(320)
    assert all(c["applied_to_policy"] == 0 for c in out["candidates"])
    # 무시/채택 후 다시 제안해도 자동반영 없음
    ts.record_action(out["candidates"][0]["id"], 320, "ignored")
    out2 = ts.suggest(320)
    assert all(c["applied_to_policy"] == 0 for c in out2["candidates"])
    assert all(c["user_action"] == "suggested" for c in out2["candidates"])


def test_per_theme_differentiation():
    # 반도체 hedge 수용·바이오 hedge 무시 → 테마별 차등(theme scope 가중 분리).
    pz.record_feedback(330, "theme", "반도체", "accepted")
    pz.record_feedback(330, "theme", "반도체", "accepted")
    pz.record_feedback(330, "theme", "바이오", "ignored")
    pz.record_feedback(330, "theme", "바이오", "ignored")
    assert pz.weight_for(330, "theme", "반도체") > 1.0
    assert pz.weight_for(330, "theme", "바이오") < 1.0
