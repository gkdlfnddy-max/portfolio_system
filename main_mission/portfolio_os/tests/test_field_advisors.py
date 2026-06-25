"""필드별 전문 조언(field_advisors) 테스트 — 규칙+메모리만(Anthropic API 미사용).

임시 SQLite로 전 경로 검증:
  - 각 advisor 가 구조화 dict 반환
  - theme 과집중 경고 발생
  - defensive_advisor 가 '방어 = 순현금 + 채권'(현금에 무조건 더하지 않음)을 설명
  - 계좌 정책 우선: 테마 불허 정책이면 메모리 theme tilt 제안 억제
  - consult() 가 field_consultations 행 기록 + posthook provenance(task_memory_links)
  - record_action() 가 field_advice_events 기록
  - CLI 가 JSON 출력 / account 없으면 hard-block
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_field_advisors.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import field_advisors as fa
from main_mission.portfolio_os import profile as profile_mod
from main_mission.portfolio_os.growth import memory as memory_mod


def setup():
    store_db.init()


# ---- NO Anthropic import (CLAUDE.md §17) ----
def test_no_anthropic_import():
    src = importlib.util.find_spec("main_mission.portfolio_os.field_advisors").origin
    with open(src, encoding="utf-8") as f:
        text = f.read()
    low = text.lower()
    # SDK import / API key 의존이 없어야 한다(서술 문구 '미사용'은 허용).
    assert "import anthropic" not in low
    assert "from anthropic" not in low
    assert "anthropic-ai" not in low
    assert "ANTHROPIC_API_KEY" not in text
    assert "claude-" not in low  # 모델 id 호출 흔적 없음


def _assert_struct(d, field, agent):
    for k in ("field_name", "agent_name", "advice_type", "original_text", "suggested_text",
              "extracted_variables", "risk_warnings", "missing_points", "follow_up",
              "sources", "confidence"):
        assert k in d, (k, d)
    assert d["field_name"] == field
    assert d["agent_name"] == agent
    assert isinstance(d["extracted_variables"], dict)
    assert isinstance(d["risk_warnings"], list)
    assert isinstance(d["missing_points"], list)
    assert isinstance(d["follow_up"], list)
    assert isinstance(d["sources"], list)
    assert isinstance(d["confidence"], (int, float))


# ---- each advisor returns structured dict ----
def test_theme_advisor_struct():
    d = fa.theme_advisor(1, "로봇, 바이오, 양자컴퓨터", "improve")
    _assert_struct(d, "interests", "theme-field-advisor")
    assert "양자컴퓨터" in d["extracted_variables"]["themes"]


def test_opinion_advisor_struct_and_gaps():
    d = fa.opinion_advisor(1, "공격적으로 가되 현금 20~40%.", "find_gaps")
    _assert_struct(d, "views", "opinion-field-advisor")
    assert d["extracted_variables"].get("risk_tolerance") == "aggressive"
    assert d["extracted_variables"].get("cash_band", {}).get("min") == 20.0
    # 빠진 변수가 missing_points 로 잡힘 (예: horizon).
    assert any("기간" in m for m in d["missing_points"])


def test_region_advisor_validates_sum():
    d = fa.region_advisor(1, "미국 50 / 한국 40 / 기타 10")
    _assert_struct(d, "region", "region-field-advisor")
    assert d["extracted_variables"]["region_policy"]["total"] == 100
    bad = fa.region_advisor(1, "미국 70 / 한국 40")
    assert any("100" in w for w in bad["risk_warnings"]), bad["risk_warnings"]


def test_pace_advisor_struct():
    d = fa.pace_advisor(1, "천천히 분할로 진입")
    _assert_struct(d, "pace", "pace-field-advisor")
    assert d["extracted_variables"]["rebalance_pace"] == "slow"
    assert "limit" in d["extracted_variables"]["entry_rule"]


def test_whole_advisor_struct_reads_both():
    d = fa.whole_advisor(1, "로봇, 양자컴퓨터", "방어적으로 가고 싶다")
    _assert_struct(d, "whole", "whole-field-advisor")
    assert "policy_outline" in d["extracted_variables"]


# ---- theme over-concentration warning fires ----
def test_theme_over_concentration_warning():
    # 방향성 도입 후: 롱 후보(견해에 long 힌트)만 tilt 대상 → 5개 롱이어야 과집중 경고.
    profile_mod.save(60, {"interests_text": "로봇,바이오,양자컴퓨터,AI,반도체",
                          "views_text": "로봇 바이오 양자컴퓨터 AI 반도체 전부 장기성장이라 분할 매수하고 싶어"})
    d = fa.theme_advisor(60, "로봇, 바이오, 양자컴퓨터, AI, 반도체", "risk_check")
    assert any("쏠" in w or "한도" in w for w in d["risk_warnings"]), d["risk_warnings"]


# ---- defensive_advisor explains 방어 = 순현금 + 채권 (NOT added on top) ----
def test_defensive_advisor_explains_bucket():
    d = fa.defensive_advisor(1, "채권 10%, 단기채 위주")
    assert d["extracted_variables"]["bond_policy"]["bond_target_pct"] == 10.0
    # 설명에 방어 = 순현금 + 채권, 무조건 더하지 않음.
    assert "순현금" in d["suggested_text"]
    assert "채권" in d["suggested_text"]
    assert d["extracted_variables"]["defensive_model"].startswith("defensive = net_cash + bond")
    # bond_target_pct 는 이제 방어자산 대비 비율(ratio of defensive).
    assert "ratio of defensive" in d["extracted_variables"]["defensive_model"]


# ---- account policy priority: theme tilt suggestion suppressed when policy forbids themes ----
def test_account_policy_priority_suppresses_theme_tilt():
    acc = 55
    # 계좌 정책: cash_defensive(allow_themes=False) — investor_profile 에 저장.
    profile_mod.save(acc, {"policy_type": "cash_defensive", "interests_text": "AI",
                           "views_text": "AI는 장기성장이라 분할 매수하고 싶어"})
    eff = fa.policy_rules.effective_policy(acc)
    assert eff["flags"].get("allow_themes") is False, eff["flags"]
    # 메모리에 AI 테마 비중확대 제안(공통 agent) 적재 + 승격.
    r = memory_mod.remember("agent", "AI 테마 비중확대", "tilt 권장", agent_name="theme-field-advisor",
                            theme="AI", confidence=0.9, source="agent")
    memory_mod.promote_agent_memory(r["memory_id"])
    d = fa.theme_advisor(acc, "AI", "improve")
    # 억제됨 → sources 에 그 메모리가 'memory:' 로 들어오지 않아야 한다.
    mem_titles = [s.get("title") for s in d["sources"] if str(s.get("kind", "")).startswith("memory")]
    assert "AI 테마 비중확대" not in mem_titles, d["sources"]
    # 정책 우선 경고가 명시되어야 한다.
    assert any("테마" in w and "정책" in w for w in d["risk_warnings"]), d["risk_warnings"]


# ---- consult() writes field_consultations row + posthook provenance ----
def test_consult_writes_row_and_provenance():
    out = fa.consult(3, "interests", text="로봇, 바이오", advice_type="improve")
    assert out["ok"] is True
    cid = out["consultation_id"]
    assert cid is not None
    conn = store_db.connect()
    try:
        row = conn.execute("SELECT * FROM field_consultations WHERE id=?", (cid,)).fetchone()
        assert row is not None
        assert row["account_index"] == 3
        assert row["field_name"] == "interests"
        assert row["agent_name"] == "theme-field-advisor"
        # posthook provenance: task_memory_links 에 이 task 가 연결되어야 한다(prehook+posthook).
        tid = out["task_id"]
        assert tid is not None
        links = conn.execute("SELECT COUNT(*) c FROM task_memory_links WHERE task_id=?", (tid,)).fetchone()
        assert links["c"] >= 1, "prehook/posthook provenance 없음"
        # task 가 done 으로 마감(검증 없는 DONE 금지 — outcome 존재).
        t = conn.execute("SELECT status, outcome FROM tasks WHERE id=?", (tid,)).fetchone()
        assert t["status"] == "done", dict(t)
    finally:
        conn.close()


# ---- consult() hard-block when account missing ----
def test_consult_hard_block_when_account_none():
    out = fa.consult(None, "interests", text="로봇")
    assert out["ok"] is False
    assert out.get("gate") == "block"


# ---- record_action() writes field_advice_events ----
def test_record_action_writes_event():
    out = fa.consult(4, "pace", text="천천히")
    cid = out["consultation_id"]
    ev = fa.record_action(cid, 4, "pace", "applied", detail="form temp-applied (no save)")
    assert ev["ok"] is True
    conn = store_db.connect()
    try:
        row = conn.execute("SELECT * FROM field_advice_events WHERE id=?", (ev["event_id"],)).fetchone()
        assert row["user_action"] == "applied"
        assert row["field_consultation_id"] == cid
        assert row["account_index"] == 4
    finally:
        conn.close()
    bad = fa.record_action(cid, 4, "pace", "bogus")
    assert bad["ok"] is False


# ---- CLI emits JSON ----
def test_cli_emits_json():
    env = dict(os.environ)
    env["SQLITE_PATH"] = _TMP
    env["PYTHONIOENCODING"] = "utf-8"
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    p = subprocess.run(
        [sys.executable, "-m", "main_mission.portfolio_os.field_advisors",
         "--account", "9", "--field", "defensive", "--text", "채권 10% 단기채"],
        capture_output=True, text=True, cwd=root, env=env,
    )
    import json
    line = [l for l in p.stdout.strip().splitlines() if l.strip()][-1]
    j = json.loads(line)
    assert j["ok"] is True
    assert j["advice"]["field_name"] == "defensive"


def test_cli_hard_block_without_account():
    env = dict(os.environ)
    env["SQLITE_PATH"] = _TMP
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    p = subprocess.run(
        [sys.executable, "-m", "main_mission.portfolio_os.field_advisors",
         "--field", "interests", "--text", "로봇"],
        capture_output=True, text=True, cwd=root, env=env,
    )
    import json
    line = [l for l in p.stdout.strip().splitlines() if l.strip()][-1]
    j = json.loads(line)
    assert j["ok"] is False
    assert j.get("gate") == "block"


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} FIELD-ADVISOR TESTS PASSED")
