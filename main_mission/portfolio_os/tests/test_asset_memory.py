"""asset_memory + lesson_runs — 공통↔사용자 분리·계좌격리·stale·출처없는 강한기억 차단·reliability 갱신.

키 없이 임시 SQLite 로 전 경로 검증(Anthropic API 미사용). import 전 SQLITE_PATH 핀 → setup() init.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_asset_memory.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import asset_memory as am
from main_mission.portfolio_os import lesson_runs as lr


def setup():
    store_db.init()


def _past(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ──────────────── record / search 기본 ────────────────
def test_record_shared_vs_user_separated():
    """공통(account NULL)과 사용자 관점(account 지정)이 분리 저장/조회."""
    s = am.record("stock", "005930", "fact", ticker="005930",
                  title="외국인 순매수 전환", source="kis_investor", source_date="2026-06-20",
                  freshness=0.9, confidence=0.7)
    assert s["ok"] and s["shared"] is True
    u = am.record("stock", "005930", "user_view", account_index=7, ticker="005930",
                  title="반도체 장기 긍정", source="user", source_date="2026-06-20", freshness=0.8)
    assert u["shared"] is False and u["account_index"] == 7

    shared = am.search(scope_type="stock", scope_key="005930", account_index="__shared__")
    assert all(m["account_index"] is None for m in shared)
    assert any(m["title"] == "외국인 순매수 전환" for m in shared)
    # 공통 검색에 사용자 관점 혼입 금지
    assert not any(m["title"] == "반도체 장기 긍정" for m in shared)

    user7 = am.search(scope_type="stock", scope_key="005930", account_index=7)
    assert all(m["account_index"] == 7 for m in user7)
    assert any(m["title"] == "반도체 장기 긍정" for m in user7)


def test_account_isolation():
    """계좌 8 의 메모리는 계좌 9 검색에 나오지 않는다(교차 금지)."""
    am.record("theme", "robotics", "user_view", account_index=8, theme="robotics",
              title="계좌8 로봇 관점", source="user", source_date="2026-06-20", freshness=0.7)
    a8 = am.search(scope_type="theme", scope_key="robotics", account_index=8)
    a9 = am.search(scope_type="theme", scope_key="robotics", account_index=9)
    assert any(m["title"] == "계좌8 로봇 관점" for m in a8)
    assert not any(m["title"] == "계좌8 로봇 관점" for m in a9)


# ──────────────── 출처 없는 강한 기억 차단 ────────────────
def test_unsourced_strong_memory_downgraded():
    """출처(evidence/source) 없이 강한 confidence 주장 → 자동 강등 + downgraded 표시."""
    r = am.record("sector", "semiconductor", "interpretation", sector="semiconductor",
                  title="섹터 강세 단정", confidence=0.9)  # 출처 없음
    assert r["downgraded"] is True
    assert r["confidence"] <= am.WEAK_CONFIDENCE_CAP
    assert r["has_source"] is False
    # 조회 시 weak 플래그
    got = am.get(r["id"])
    assert got["weak"] is True


def test_sourced_strong_memory_kept():
    """출처 있으면 강한 confidence 유지."""
    r = am.record("sector", "semiconductor", "interpretation", sector="semiconductor",
                  title="섹터 강세(근거 있음)", confidence=0.85,
                  source="report", source_date="2026-06-20", freshness=0.9)
    assert r["downgraded"] is False
    assert r["confidence"] == 0.85
    assert r["has_source"] is True


# ──────────────── stale 표시 ────────────────
def test_stale_flagged_and_filterable():
    r = am.record("macro", "interest_rate", "fact", macro_factor="policy_rate",
                  title="금리 동결", source="ecos", source_date="2026-06-20", freshness=0.9,
                  stale_at=_past(1))  # 이미 지난 stale_at → stale
    got = am.get(r["id"])
    assert got["stale"] is True
    # include_stale=False 면 빠진다
    fresh_only = am.search(scope_type="macro", scope_key="interest_rate", include_stale=False)
    assert not any(m["id"] == r["id"] for m in fresh_only)


# ──────────────── search filter ────────────────
def test_freshness_confidence_filter():
    am.record("etf", "069500", "fact", ticker="069500", title="저신뢰",
              source="x", source_date="2026-06-20", freshness=0.2, confidence=0.2)
    am.record("etf", "069500", "fact", ticker="069500", title="고신뢰",
              source="x", source_date="2026-06-20", freshness=0.95, confidence=0.8)
    hi = am.search(scope_type="etf", scope_key="069500", min_confidence=0.5)
    assert all((m["confidence"] or 0) >= 0.5 for m in hi)
    assert any(m["title"] == "고신뢰" for m in hi)


def test_search_by_ticker_exact():
    res = am.search(ticker="005930", account_index="__shared__")
    assert all(m["ticker"] == "005930" for m in res)


# ──────────────── growth_report ────────────────
def test_growth_report():
    # self-contained: 자기 prerequisite(공통 005930 기억) 를 직접 시드 — 앞 테스트 의존 제거.
    am.record("stock", "005930", "fact", ticker="005930", title="삼성전자 기초",
              source="x", source_date="2026-06-20", freshness=0.9, confidence=0.7)
    rep = am.growth_report("stock", "005930")
    assert rep["scope_key"] == "005930"
    assert rep["total"] >= 1
    assert rep["shared_count"] >= 1
    assert "by_memory_type" in rep


# ──────────────── lesson_runs reliability 갱신 ────────────────
def test_lesson_outcome_updates_reliability_hit():
    base = lr.reliability("stock", "000660")["reliability"]
    run = lr.record_lesson("stock", "000660", account_index=3,
                           signal_summary="외국인 순매수+가격 무릎",
                           suggested_action="buy")
    assert run["hit_or_miss"] == "pending"
    out = lr.record_outcome(run["id"], 20, {"return_pct": 6.0})  # buy + 상승 → hit
    assert out["hit_or_miss"] == "hit"
    assert out["reliability_after"] > out["reliability_before"]
    after = lr.reliability("stock", "000660")
    assert after["reliability"] >= base
    assert after["counts"]["hit"] >= 1


def test_lesson_outcome_miss_and_false_alarm():
    # buy 인데 하락 → miss
    r1 = lr.record_lesson("theme", "quantum", suggested_action="buy")
    o1 = lr.record_outcome(r1["id"], 10, {"return_pct": -4.0})
    assert o1["hit_or_miss"] == "miss"
    assert o1["reliability_after"] < o1["reliability_before"]
    # 방어(shift_conservative) 인데 안 떨어짐 → false_alarm
    r2 = lr.record_lesson("theme", "quantum", suggested_action="shift_conservative")
    o2 = lr.record_outcome(r2["id"], 10, {"return_pct": 2.0, "drawdown_pct": -1.0})
    assert o2["hit_or_miss"] == "false_alarm"


def test_defensive_hit_on_drawdown():
    r = lr.record_lesson("stock", "035720", suggested_action="shift_conservative")
    o = lr.record_outcome(r["id"], 20, {"drawdown_pct": -9.0})  # 큰 낙폭 → 방어 적중
    assert o["hit_or_miss"] == "hit"


# ──────────────── Anthropic 미사용 ────────────────
def test_no_anthropic_import():
    for mod in ("asset_memory.py", "lesson_runs.py", "memory_prehook.py"):
        path = os.path.join(os.path.dirname(__file__), "..", mod)
        low = open(path, encoding="utf-8").read().lower()
        assert "import anthropic" not in low
        assert "from anthropic" not in low
        assert "anthropic_api_key" not in low
        assert "anthropic-ai" not in low


def test_no_auto_order_or_policy_mutation():
    """주문/policy 자동변경 코드 없음(조회/저장만)."""
    for mod in ("asset_memory.py", "lesson_runs.py", "memory_prehook.py"):
        path = os.path.join(os.path.dirname(__file__), "..", mod)
        src = open(path, encoding="utf-8").read().lower()
        assert "place_order" not in src
        assert "submit_order" not in src
        # policy 테이블에 INSERT/UPDATE 하지 않음
        assert "insert into portfolio_polic" not in src
        assert "update portfolio_polic" not in src
