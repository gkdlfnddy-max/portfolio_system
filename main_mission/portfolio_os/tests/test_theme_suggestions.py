"""관심 분야 AI 후보 제안(theme_suggestions) 테스트 — 규칙+인접맵+evidence+memory(API 미사용).

검증:
  - 5종 분류(adjacent|complement|diversify|hedge|watch) 생성
  - 모든 후보 direction = unknown_direction (neutral — 자동 long 금지)
  - 이미 관심에 있는 테마는 후보에서 제외
  - 반복 무시(user_action=ignored 다수) 후보는 confidence 하향 + 후순위
  - record_action 이 user_action 기록 + 격리
  - 후보가 allocation/policy 에 자동 반영 안 됨 (applied_to_policy=0)
  - 계좌 격리 (다른 계좌 후보는 보이지/수정되지 않음)
  - Anthropic API 미사용
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_theme_suggestions.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import theme_suggestions as ts
from main_mission.portfolio_os import profile as profile_mod


def setup():
    store_db.init()


def _seed(account_index: int, interests: str, *, posture: str = "", views: str = ""):
    profile_mod.save(account_index, {"interests_text": interests, "posture_text": posture,
                                     "views_text": views})


# ---- NO Anthropic import (CLAUDE.md §17) ----
def test_no_anthropic_import():
    src = importlib.util.find_spec("main_mission.portfolio_os.theme_suggestions").origin
    with open(src, encoding="utf-8") as f:
        text = f.read()
    low = text.lower()
    assert "import anthropic" not in low
    assert "from anthropic" not in low
    assert "anthropic-ai" not in low
    assert "ANTHROPIC_API_KEY" not in text
    assert "claude-" not in low


def test_five_classification_types_generated():
    _seed(1, "로봇, 바이오, 양자컴퓨터, 반도체", posture="공격적으로 가되 일부 과열 우려")
    out = ts.suggest(1)
    assert out["ok"], out
    types = {c["candidate_type"] for c in out["candidates"]}
    for t in ("adjacent", "complement", "diversify", "hedge", "watch"):
        assert t in types, (t, types)
    assert all(c["candidate_type"] in ts.VALID_TYPES for c in out["candidates"])


def test_candidates_are_neutral_unknown_direction():
    _seed(2, "반도체, 바이오")
    out = ts.suggest(2)
    assert out["ok"]
    assert out["candidates"], "후보가 생성돼야 함"
    # neutral 증거: 모든 후보 direction = unknown_direction (자동 long 금지)
    assert all(c["direction"] == "unknown_direction" for c in out["candidates"])
    assert ts.DEFAULT_DIRECTION == "unknown_direction"


def test_existing_interests_excluded():
    _seed(3, "반도체, AI")
    out = ts.suggest(3)
    have = {ts._norm_theme("반도체"), ts._norm_theme("AI")}
    cand_norm = {ts._norm_theme(c["candidate_theme"]) for c in out["candidates"]}
    assert not (have & cand_norm), (have, cand_norm)


def test_not_applied_to_policy_or_allocation():
    _seed(4, "로봇, 양자컴퓨터")
    out = ts.suggest(4)
    # 자동반영 안 됨 증거: 모든 후보 applied_to_policy=0, applied_to_research_queue=0, user_action=suggested
    assert all(c["applied_to_policy"] == 0 for c in out["candidates"])
    assert all(c["applied_to_research_queue"] == 0 for c in out["candidates"])
    assert all(c["user_action"] == "suggested" for c in out["candidates"])
    # DB 에서도 확인
    conn = store_db.connect()
    try:
        n_applied = conn.execute(
            "SELECT COUNT(*) FROM theme_suggestion_candidates WHERE account_index=4 AND applied_to_policy=1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n_applied == 0


def test_record_action_added_to_research_not_policy():
    _seed(5, "바이오")
    out = ts.suggest(5)
    cid = out["candidates"][0]["id"]
    # [조사 후보로 추가] — policy 직접 반영 아님
    res = ts.record_action(cid, 5, "added_to_research")
    assert res["ok"], res
    assert res["user_action"] == "added_to_research"
    assert res["applied_to_research_queue"] == 1
    assert res["applied_to_policy"] == 0  # 조사 추가는 policy 반영 아님


def test_added_to_research_appends_to_interests_neutral():
    # 끊긴 고리 수정: [조사 후보로 추가] → candidate_theme 이 관심 분야에 '방향 미정'으로 등장.
    _seed(20, "반도체")
    out = ts.suggest(20)
    cand = out["candidates"][0]
    cid, theme = cand["id"], cand["candidate_theme"]
    # 추가 전: 관심 분야에 후보 테마 없음
    before = profile_mod.get(20)["interests_text"]
    assert theme not in before
    res = ts.record_action(cid, 20, "added_to_research")
    assert res["ok"], res
    assert res["added_to_interests"] is True
    assert res["direction"] == "unknown_direction"  # neutral — 자동 long 금지
    # 추가 후: 관심 분야 interests_text 에 후보 테마가 등재됨
    after = profile_mod.get(20)["interests_text"]
    assert theme in after
    # **policy/allocation 자동 반영 없음** — applied_to_policy=0 유지
    assert res["applied_to_policy"] == 0


def test_added_to_research_surfaces_in_theme_directions_unknown():
    # 조사 후보로 추가한 테마가 advice.generate 의 themes(관심 테마별 정리)에 '방향 미정'으로 등장.
    from main_mission.portfolio_os import advice as advice_mod
    _seed(21, "로봇")
    out = ts.suggest(21)
    # THEME_KEYWORDS 에 없을 수 있는 임의 후보(예: 'AI 인프라')도 등장해야 한다(끊긴 고리).
    cand = out["candidates"][0]
    ts.record_action(cand["id"], 21, "added_to_research")
    gen = advice_mod.generate(21, "로봇")
    themes = {t["theme"]: t for t in gen["themes"]}
    assert cand["candidate_theme"] in themes, (cand["candidate_theme"], list(themes))
    # 자동 long 금지: 방향 미지정이면 long_candidate 아님(미반영)
    assert themes[cand["candidate_theme"]]["direction"] != "long_candidate"


def test_added_to_research_no_duplicate_interest():
    _seed(22, "바이오")
    out = ts.suggest(22)
    cid = out["candidates"][0]["id"]
    ts.record_action(cid, 22, "added_to_research")
    first = profile_mod.get(22)["interests_text"]
    # 같은 후보 재추가 — 중복 등재 안 함
    res2 = ts.record_action(cid, 22, "added_to_research")
    assert res2["added_to_interests"] is False
    assert profile_mod.get(22)["interests_text"] == first


def test_ignored_does_not_add_interest():
    # 무시는 관심 분야에 절대 추가하지 않는다.
    _seed(23, "양자컴퓨터")
    out = ts.suggest(23)
    cand = out["candidates"][0]
    before = profile_mod.get(23)["interests_text"]
    res = ts.record_action(cand["id"], 23, "ignored")
    assert res["ok"]
    assert not res.get("added_to_interests")
    assert profile_mod.get(23)["interests_text"] == before


def test_saved_to_policy_sets_flag():
    _seed(6, "로봇")
    out = ts.suggest(6)
    cid = out["candidates"][0]["id"]
    res = ts.record_action(cid, 6, "saved_to_policy")
    assert res["ok"]
    assert res["applied_to_policy"] == 1  # 명시적 저장 시에만 1


def test_repeated_ignore_suppresses():
    _seed(7, "반도체")
    # 1차 제안 → 모든 후보 ignored 처리(2회 누적되도록 두 사이클)
    first = ts.suggest(7)
    target = first["candidates"][0]["candidate_theme"]
    for c in first["candidates"]:
        if c["candidate_theme"] == target:
            ts.record_action(c["id"], 7, "ignored")
    second = ts.suggest(7)
    for c in second["candidates"]:
        if c["candidate_theme"] == target:
            ts.record_action(c["id"], 7, "ignored")
    # 3차 제안 — 반복 무시된 후보는 confidence 하향 + deprioritized
    third = ts.suggest(7)
    match = [c for c in third["candidates"] if c["candidate_theme"] == target]
    assert match, target
    m = match[0]
    assert m["ignored_count"] >= 2
    assert m["deprioritized"] is True
    # 후순위: deprioritized 후보는 비-deprioritized 후보보다 뒤에 위치
    idxs_dep = [i for i, c in enumerate(third["candidates"]) if c["deprioritized"]]
    idxs_keep = [i for i, c in enumerate(third["candidates"]) if not c["deprioritized"]]
    if idxs_dep and idxs_keep:
        assert min(idxs_dep) > max(idxs_keep)


def test_account_isolation():
    _seed(8, "로봇")
    _seed(9, "바이오")
    out8 = ts.suggest(8)
    cid8 = out8["candidates"][0]["id"]
    # 계좌 9 가 계좌 8 의 후보를 수정 시도 → 실패(격리)
    res = ts.record_action(cid8, 9, "ignored")
    assert not res["ok"], "다른 계좌 후보는 수정 불가(격리)"


def test_account_required_hard_block():
    out = ts.suggest(None)
    assert not out["ok"]
    assert out.get("gate") == "block"


def test_evidence_freshness_label_present():
    _seed(10, "양자컴퓨터")
    out = ts.suggest(10)
    # evidence 없어도 freshness_label='none' 으로 정직히 표시(근거 없는 강한 추천 금지)
    assert all("evidence_freshness" in c for c in out["candidates"])
    assert all(c["evidence_freshness"] in ("fresh", "stale", "conflicting", "none")
               for c in out["candidates"])
    # 근거 없으면 confidence 상한(<=0.45)
    for c in out["candidates"]:
        if c["evidence_freshness"] == "none":
            assert c["confidence"] <= 0.45 + 1e-9


def test_cli_json_output():
    _seed(11, "반도체")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "-m", "main_mission.portfolio_os.theme_suggestions",
         "--account", "11", "--suggest"],
        cwd=root, capture_output=True, text=True, env=env,
    )
    line = [l for l in proc.stdout.strip().splitlines() if l.strip()][-1]
    data = json.loads(line)
    assert data["ok"], (proc.stdout, proc.stderr)
    assert "candidates" in data
