"""account_memory — 계좌별 통합 조회 + 계좌 격리(교차 누수 금지) + 공통↔계좌 분리.

키 없이 임시 SQLite 로 검증(Anthropic API 미사용). import 전 SQLITE_PATH 핀 → setup() init.
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_account_memory.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import account_memory as acm
from main_mission.portfolio_os import investor_objective as objective
from main_mission.portfolio_os import user_views as uv
from main_mission.portfolio_os import lesson_runs as lr


def setup():
    store_db.init()


def _seed_profile(idx, *, risk="neutral", cash_min=10.0, cash_max=30.0):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, updated_at) "
            "VALUES(?,?,?,?, datetime('now')) "
            "ON CONFLICT(account_index) DO UPDATE SET risk_tolerance=excluded.risk_tolerance, "
            "cash_min_pct=excluded.cash_min_pct, cash_max_pct=excluded.cash_max_pct",
            (idx, risk, cash_min, cash_max),
        )
        conn.commit()
    finally:
        conn.close()


# ──────────────── 통합 조회 ────────────────
def test_account_context_combines_objective_policy():
    """account_context 가 목적·정책·견해·lesson 을 한 번에 모은다(계좌 격리)."""
    _seed_profile(101, risk="aggressive", cash_min=5.0, cash_max=20.0)
    objective.set_objective(101, {"investment_goal": "aggressive_growth", "risk_tolerance": "high"})
    uv.add(101, layer="mid", theme="robotics", stance="positive", note="로봇 긍정")
    lr.record_lesson("stock", "005930", account_index=101,
                     signal_summary="무릎 진입", suggested_action="buy")

    ctx = acm.account_context(101)
    assert ctx["account_index"] == 101
    assert ctx["objective"]["is_set"] is True
    assert ctx["objective"]["objective"]["investment_goal"] == "aggressive_growth"
    assert ctx["policy"]["effective"] is not None
    assert any(v["theme"] == "robotics" for v in ctx["views"])
    assert any(l["scope_key"] == "005930" for l in ctx["lessons"])
    # 자동 적용 아님 · 격리 단언
    assert ctx["advisory_only"] is True and ctx["applied"] is False and ctx["isolated"] is True
    # 우선순위: 계좌 정책/목적이 위
    assert ctx["priority"][0] == "selected_allocation"
    assert "objective" in ctx["priority"] and "policy" in ctx["priority"]


def test_objective_unset_is_honest():
    """목적 미설정 계좌는 기준을 가정하지 않고 정직하게 알림."""
    _seed_profile(102)
    ctx = acm.account_context(102)
    assert ctx["objective"]["is_set"] is False
    assert any("미설정" in n for n in ctx["notes"])


# ──────────────── 계좌 격리(교차 누수 금지) ────────────────
def test_account_isolation_views_and_lessons():
    """계좌 201 의 견해/lesson 이 계좌 202 의 context 에 나오지 않는다."""
    _seed_profile(201)
    _seed_profile(202)
    uv.add(201, layer="mid", theme="quantum", stance="positive", note="201 전용 견해")
    lr.record_lesson("stock", "000660", account_index=201,
                     signal_summary="201 판단", suggested_action="buy")

    c1 = acm.account_context(201)
    c2 = acm.account_context(202)
    assert any(v["theme"] == "quantum" for v in c1["views"])
    assert not any(v.get("theme") == "quantum" for v in c2["views"])
    assert any(l["signal_summary"] == "201 판단" for l in c1["lessons"])
    assert not any(l.get("signal_summary") == "201 판단" for l in c2["lessons"])


def test_objective_isolated_per_account():
    """목적도 계좌별 — 한 계좌 목적이 다른 계좌에 보이지 않는다."""
    _seed_profile(301)
    _seed_profile(302)
    objective.set_objective(301, {"investment_goal": "loss_reduction"})
    objective.set_objective(302, {"investment_goal": "aggressive_growth"})
    assert acm.account_context(301)["objective"]["objective"]["investment_goal"] == "loss_reduction"
    assert acm.account_context(302)["objective"]["objective"]["investment_goal"] == "aggressive_growth"


# ──────────────── 계좌 정책 우선(공통이 덮지 않음) ────────────────
def test_account_policy_present_and_hard_rules_listed():
    """계좌 정책 + hard rule 이 함께 노출 — hard rule 은 변경 불가."""
    _seed_profile(401, risk="defensive")
    pol = acm.account_policy(401)
    assert pol["effective"] is not None
    assert "no_anthropic_api" in pol["hard_rules"]
    assert "account_memory_isolation" in pol["hard_rules"]


# ──────────────── 자동 적용/주문 0 ────────────────
def test_no_auto_order_or_policy_mutation():
    for mod in ("account_memory.py", "asset_for_account.py"):
        path = os.path.join(os.path.dirname(__file__), "..", mod)
        src = open(path, encoding="utf-8").read().lower()
        assert "place_order" not in src
        assert "submit_order" not in src
        assert "insert into portfolio_polic" not in src
        assert "update portfolio_polic" not in src
        assert "insert into orders" not in src


def test_no_anthropic_import():
    for mod in ("account_memory.py", "asset_for_account.py"):
        path = os.path.join(os.path.dirname(__file__), "..", mod)
        low = open(path, encoding="utf-8").read().lower()
        assert "import anthropic" not in low
        assert "from anthropic" not in low
        assert "anthropic_api_key" not in low
        assert "anthropic-ai" not in low
