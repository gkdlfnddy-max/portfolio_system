"""investor_objective — 목적/성향 저장·조회 · "최선 기준" 매핑 · 계좌 격리 · 정직(미설정).

키 없이 임시 SQLite로 전 경로 검증(Anthropic API 미사용).
import 전에 임시 SQLITE_PATH 주입 → setup() 에서 store_db.init() (격리 필수).
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_investor_objective.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import investor_objective as io


def setup():
    store_db.init()


# ──────────────── 저장/조회 ────────────────
def test_set_and_get():
    r = io.set_objective(1, {
        "investment_goal": "loss_reduction",
        "horizon": "3년",
        "risk_tolerance": "low",
        "loss_aversion": 0.8,
        "prefers": ["cash", "bond"],
        "allows": ["inverse"],
        "region_pref": "kr",
        "market_view": "long",
        "note": "손실이 제일 싫다",
    })
    assert r["ok"], r
    obj = io.get(1)
    assert obj["investment_goal"] == "loss_reduction"
    assert obj["risk_tolerance"] == "low"
    assert obj["loss_aversion"] == 0.8
    assert obj["prefers"] == ["cash", "bond"]
    assert obj["allows"] == {"inverse": True, "leverage": False}
    assert obj["region_pref"] == "kr"
    assert obj["market_view"] == "long"
    assert io.is_set(1) is True


def test_unset_returns_none_no_default():
    # 정직: 미설정 계좌는 None — 기본값을 가정하지 않는다.
    assert io.get(50) is None
    assert io.is_set(50) is False


def test_partial_without_goal_is_set_false():
    io.set_objective(2, {"risk_tolerance": "mid"})
    assert io.get(2)["investment_goal"] is None
    assert io.is_set(2) is False  # goal 없으면 의미있게 설정된 것 아님


def test_empty_input_rejected():
    r = io.set_objective(3, {})
    assert not r["ok"]


def test_invalid_enums_rejected():
    for data in (
        {"investment_goal": "moon"},
        {"risk_tolerance": "extreme"},
        {"loss_aversion": 1.5},
        {"region_pref": "mars"},
        {"market_view": "forever"},
        {"prefers": ["gold"]},
    ):
        try:
            io.set_objective(4, data)
            assert False, f"should reject {data}"
        except ValueError:
            pass


# ──────────────── supersede 이력 보존 ────────────────
def test_set_supersedes_old():
    io.set_objective(5, {"investment_goal": "growth", "risk_tolerance": "high"})
    first = io.get(5)["view_id"]
    io.set_objective(5, {"investment_goal": "loss_reduction", "risk_tolerance": "low"})
    cur = io.get(5)
    assert cur["view_id"] != first
    assert cur["investment_goal"] == "loss_reduction"
    # 옛 행은 superseded — active 는 정확히 1개
    conn = store_db.connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM user_views WHERE account_index=5 AND layer='objective' AND status='active'",
        ).fetchone()["c"]
        old = conn.execute(
            "SELECT status, superseded_by FROM user_views WHERE id=?", (first,),
        ).fetchone()
    finally:
        conn.close()
    assert n == 1
    assert old["status"] == "superseded"
    assert old["superseded_by"] == cur["view_id"]


# ──────────────── 계좌 격리 (교차적용 금지) ────────────────
def test_account_isolation():
    io.set_objective(6, {"investment_goal": "dividend"})
    # 다른 계좌(7)는 계좌6 목적을 못 본다
    assert io.get(7) is None
    assert io.is_set(7) is False
    # 계좌7 에 다른 목적을 저장해도 계좌6 은 그대로
    io.set_objective(7, {"investment_goal": "aggressive_growth"})
    assert io.get(6)["investment_goal"] == "dividend"
    assert io.get(7)["investment_goal"] == "aggressive_growth"
    # supersede 가 다른 계좌 행을 건드리지 않는다
    io.set_objective(6, {"investment_goal": "growth"})
    assert io.get(7)["investment_goal"] == "aggressive_growth"


# ──────────────── "최선 기준" 매핑 ────────────────
def test_criteria_loss_reduction():
    c = io.objective_to_criteria("loss_reduction")
    assert c["ok"] and c["is_set"]
    metrics = {x["metric"]: x for x in c["criteria"]}
    assert "max_drawdown" in metrics and metrics["max_drawdown"]["direction"] == "min"
    assert "cash_band" in metrics and metrics["cash_band"]["direction"] == "max"
    # 손실축소 목적이면 수익 최대화는 우선순위에서 내림
    assert "max_return" in c["deprioritize"]


def test_criteria_dividend_growth_volatility():
    div = io.objective_to_criteria("dividend")
    assert any(x["metric"] == "dividend_yield" and x["direction"] == "max" for x in div["criteria"])
    grw = io.objective_to_criteria("growth")
    assert any(x["metric"] == "cagr" and x["direction"] == "max" for x in grw["criteria"])
    assert any(x["metric"] == "growth_tilt" for x in grw["criteria"])
    vol = io.objective_to_criteria("volatility_reduction")
    assert any(x["metric"] == "volatility" and x["direction"] == "min" for x in vol["criteria"])
    assert any(x["metric"] == "diversification" for x in vol["criteria"])


def test_all_goals_have_criteria():
    for g in io.GOALS:
        c = io.objective_to_criteria(g)
        assert c["ok"] and c["is_set"] and len(c["criteria"]) >= 2, g


def test_criteria_unset_no_default():
    # 정직: 목적 없으면 기준 비움 + 안내 (수익률 최대화 등 기본 가정 금지)
    c = io.objective_to_criteria(None)
    assert c["ok"] and c["is_set"] is False
    assert c["criteria"] == []
    assert "미설정" in c["headline"]


def test_criteria_unknown_goal():
    c = io.objective_to_criteria("rocket")
    assert not c["ok"]


def test_criteria_for_account_unset_honest():
    out = io.criteria_for_account(40)
    assert out["is_set"] is False
    assert out["criteria"] == []
    assert out["account_index"] == 40


def test_criteria_for_account_set():
    io.set_objective(41, {"investment_goal": "thesis_hold"})
    out = io.criteria_for_account(41)
    assert out["is_set"] and out["goal"] == "thesis_hold"
    assert any(x["metric"] == "thesis_alignment" for x in out["criteria"])


# ──────────────── 카탈로그 / 자동적용 0 ────────────────
def test_goals_catalog():
    cat = io.goals_catalog()
    assert "loss_reduction" in cat["goals"]
    assert set(cat["risk_levels"]) == {"low", "mid", "high"}
    assert "inverse" in cat["allows"]


def test_no_auto_apply_only_user_views_objective_rows():
    # 저장은 user_views(layer='objective')에만 — 다른 테이블/정책을 건드리지 않는다(자동적용 0).
    # (DB 가 다른 모듈과 공유될 수 있으므로 set_objective 호출의 *델타*만 검증.)
    conn = store_db.connect()
    try:
        prof_before = conn.execute(
            "SELECT COUNT(*) c FROM investor_profile",
        ).fetchone()["c"]
    finally:
        conn.close()

    io.set_objective(60, {"investment_goal": "growth"})

    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT layer FROM user_views WHERE account_index=60",
        ).fetchall()
        prof_after = conn.execute(
            "SELECT COUNT(*) c FROM investor_profile",
        ).fetchone()["c"]
    finally:
        conn.close()
    # 계좌60 의 user_views 행은 objective 레이어만(자동 다른 견해/정책 생성 없음)
    assert [r["layer"] for r in rows] == ["objective"]
    # set_objective 가 investor_profile(정책/프로파일) 테이블을 *전혀* 건드리지 않음
    assert prof_after == prof_before


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} INVESTOR-OBJECTIVE TESTS PASSED")
