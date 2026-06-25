"""asset_for_account — **같은 자산(005930)을 계좌 목적에 따라 다르게 해석** 실증 + 계좌 격리.

운영엔 계좌1 뿐이지만, 여기선 합성 계좌 2개(성장형 vs 방어형)로 동일 종목 판단이
갈리는 것을 실증한다. 키 없이 임시 SQLite(Anthropic API 미사용).
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_asset_for_account.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import asset_for_account as afa
from main_mission.portfolio_os import investor_objective as objective
from main_mission.portfolio_os import asset_memory as am
from main_mission.portfolio_os import user_views as uv

GROWTH = 11   # 성장형 합성 계좌
DEFENS = 12   # 방어형 합성 계좌


def setup():
    store_db.init()


def _seed_profile(idx, *, risk, cash_min, cash_max, short_policy="allow"):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
            "short_policy, updated_at) VALUES(?,?,?,?,?, datetime('now')) "
            "ON CONFLICT(account_index) DO UPDATE SET risk_tolerance=excluded.risk_tolerance, "
            "cash_min_pct=excluded.cash_min_pct, cash_max_pct=excluded.cash_max_pct, "
            "short_policy=excluded.short_policy",
            (idx, risk, cash_min, cash_max, short_policy),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_accounts():
    # 성장형: 공격, 현금 적게, 숏 허용
    _seed_profile(GROWTH, risk="aggressive", cash_min=5.0, cash_max=20.0, short_policy="allow")
    objective.set_objective(GROWTH, {"investment_goal": "aggressive_growth", "risk_tolerance": "high"})
    # 방어형: 방어, 현금 많게, 숏 금지(none → 인버스 금지)
    _seed_profile(DEFENS, risk="defensive", cash_min=30.0, cash_max=60.0, short_policy="none")
    objective.set_objective(DEFENS, {"investment_goal": "loss_reduction", "risk_tolerance": "low"})
    # 공통 자산 사실(005930) — 출처 있음(두 계좌가 공유하는 사실)
    am.record("stock", "005930", "fact", ticker="005930", title="외국인 순매수 전환",
              source="kis_investor", source_date="2026-06-20", freshness=0.9, confidence=0.7)


# ──────────────── 핵심: 같은 자산, 계좌별 다른 해석 ────────────────
def test_same_asset_different_interpretation():
    """동일 005930 → 성장형은 '후보 유지·분할', 방어형은 '직접 편입 보류·ETF/국채 우선'."""
    _seed_accounts()
    g = afa.interpret("005930", GROWTH, scope_type="stock")
    d = afa.interpret("005930", DEFENS, scope_type="stock")

    # 계좌 목적이 다르게 잡힘
    assert g["account_goal"] == "aggressive_growth"
    assert d["account_goal"] == "loss_reduction"

    # stance 가 다르다 — 같은 종목인데 태도가 갈린다
    assert g["stance"] == "growth_candidate"
    assert d["stance"] == "defensive"
    assert g["stance"] != d["stance"]

    # 성장형: 후보 유지/분할 진입 포함
    g_actions = " ".join(g["candidate_actions"])
    assert "후보 유지" in g_actions
    assert "분할" in g_actions

    # 방어형: 직접 편입 보류 + 대체(ETF/국채) 우선
    d_actions = " ".join(d["candidate_actions"])
    assert "보류" in d_actions
    assert ("etf" in d_actions.lower()) or ("국채" in d_actions) or ("bond" in d_actions.lower())

    # 후보 행동 집합이 실제로 다르다
    assert set(g["candidate_actions"]) != set(d["candidate_actions"])


def test_defensive_inverse_hedge_blocked_by_policy():
    """방어형은 short_policy=none → 인버스 금지 제약이 후보에 표시(공통 후보를 제한)."""
    _seed_accounts()
    d = afa.interpret("005930", DEFENS, scope_type="stock")
    assert any("인버스" in c or "hedge" in c.lower() for c in d["risk_constraints"])


def test_single_name_limit_surfaced():
    """계좌 단일 종목 한도가 제약으로 노출(직접 편입 시 초과 금지)."""
    _seed_accounts()
    g = afa.interpret("005930", GROWTH, scope_type="stock")
    assert any("단일 종목 한도" in c for c in g["risk_constraints"])


# ──────────────── 공통 사실은 같으나 판단이 갈린다(출처 표시) ────────────────
def test_shared_fact_referenced_but_account_decides():
    """둘 다 같은 공통 자산 사실을 근거로 보지만(출처 표시), 행동은 계좌가 가른다."""
    _seed_accounts()
    g = afa.interpret("005930", GROWTH, scope_type="stock")
    d = afa.interpret("005930", DEFENS, scope_type="stock")
    # 공통 자산지식이 출처로 잡힘(둘 다)
    assert any("공통 자산지식" in s for s in g["sources"]["shared_facts"])
    assert any("공통 자산지식" in s for s in d["sources"]["shared_facts"])
    # 그러나 headline(판단)은 다르다
    assert g["headline"] != d["headline"]


# ──────────────── confidence 낮으면 단정 회피 ────────────────
def test_unset_objective_low_confidence_and_question():
    """목적 미설정 계좌 → confidence 낮고, 목적 확인 질문을 던진다(단정 회피)."""
    _seed_profile(13, risk="neutral", cash_min=10.0, cash_max=30.0)  # 목적 미설정
    r = afa.interpret("005930", 13, scope_type="stock")
    assert r["account_goal"] is None
    assert r["confidence"] < 0.5
    assert any("목적" in q for q in r["questions"])


def test_no_shared_facts_low_confidence():
    """공통 근거가 없으면(다른 자산) confidence 낮춤 — 단정 회피."""
    _seed_accounts()
    r = afa.interpret("999999", GROWTH, scope_type="stock")  # 근거 없는 종목
    assert r["confidence"] < 0.6
    assert any("근거" in q for q in r["questions"])


# ──────────────── 계좌 격리(교차 누수 금지) ────────────────
def test_account_isolation_view_not_leaked():
    """성장형 계좌 견해가 방어형 해석에 누수되지 않는다."""
    _seed_accounts()
    uv.add(GROWTH, layer="mid", ticker="005930", stance="positive", note="성장형 전용 견해")
    g = afa.interpret("005930", GROWTH, scope_type="stock")
    d = afa.interpret("005930", DEFENS, scope_type="stock")
    assert g["sources"]["account_views"] >= 1
    # 방어형엔 그 견해가 안 보임
    assert d["sources"]["account_views"] == 0
    assert d["isolated"] is True


# ──────────────── 자동 적용 0 ────────────────
def test_advisory_only_no_apply():
    _seed_accounts()
    g = afa.interpret("005930", GROWTH, scope_type="stock")
    assert g["advisory_only"] is True and g["applied"] is False
