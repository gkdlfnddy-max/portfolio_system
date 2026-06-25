"""memory_prehook — 최신/장기/시장반응/사용자반응/stale 분리·상충 저장·계좌격리·005930 실증.

키 없이 임시 SQLite 로 검증(Anthropic API 미사용). import 전 SQLITE_PATH 핀 → setup() init.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_memory_prehook.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import asset_memory as am
from main_mission.portfolio_os import lesson_runs as lr
from main_mission.portfolio_os import memory_prehook as ph


def setup():
    store_db.init()


def _past(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _seed_market(conn):
    """price/flows/macro/user_views 시드 — read-only 경로 검증용."""
    now = datetime.now(timezone.utc).isoformat()
    # 가격 하락(상충 유도): 어제 100000 → 오늘 95000
    conn.execute("INSERT OR REPLACE INTO price_history(instrument_code,trade_date,close,source,captured_at) VALUES(?,?,?,?,?)",
                 ("005930", "2026-06-19", 100000, "test", now))
    conn.execute("INSERT OR REPLACE INTO price_history(instrument_code,trade_date,close,source,captured_at) VALUES(?,?,?,?,?)",
                 ("005930", "2026-06-20", 95000, "test", now))
    # 수급 악화: 외국인+기관 순매도
    conn.execute("INSERT OR REPLACE INTO investor_flows(instrument_code,trade_date,foreign_net,institution_net,retail_net,source,captured_at) VALUES(?,?,?,?,?,?,?)",
                 ("005930", "2026-06-20", -5000, -2000, 7000, "test", now))
    # 거시
    conn.execute("INSERT OR REPLACE INTO macro_indicators(indicator,obs_date,value,source,captured_at) VALUES(?,?,?,?,?)",
                 ("yield_10y", "2026-06-20", 3.2, "test", now))
    # 사용자 견해(계좌 5): 반도체 장기 긍정
    conn.execute("INSERT INTO user_views(account_index,layer,theme,ticker,stance,conviction,horizon,note,status,created_at,updated_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (5, "long", "반도체", "005930", "positive", 0.7, "long", "삼성전자 장기 긍정", "active", now, now))
    conn.commit()


# ──────────────── 005930 실증 (핵심) ────────────────
def test_005930_prehook_combines_signals_and_user_view():
    conn = store_db.connect()
    try:
        _seed_market(conn)
    finally:
        conn.close()

    # 공통 자산지식 + 장기 thesis(출처 있음)
    am.record("stock", "005930", "fact", ticker="005930", title="외국인 최근 순매도 전환",
              source="kis_investor", source_date="2026-06-20", freshness=0.9, confidence=0.6)
    am.record("stock", "005930", "lesson", ticker="005930", time_horizon="long",
              title="장기 긍정 thesis", positive_factors=["HBM 수요"],
              source="report", source_date="2026-06-15", freshness=0.8, confidence=0.7)

    ctx = ph.prehook_context(5, "stock", "005930", theme="반도체",
                             macro_factors=["yield_10y"])

    # 수급/가격/거시 결합
    assert ctx["latest_price"] and ctx["latest_price"][0]["close"] == 95000
    assert ctx["latest_flows"] and ctx["latest_flows"][0]["foreign_net"] == -5000
    assert "yield_10y" in ctx["latest_macro"]
    # 사용자 반도체 관점 결합(계좌 5 격리)
    assert any(v["theme"] == "반도체" and v["stance"] == "positive" for v in ctx["user_views"])
    # 공통 자산지식 존재
    assert any(m["title"] == "외국인 최근 순매도 전환" for m in ctx["asset_memory_shared"])
    # 장기 thesis 분리
    assert any("장기" in (m["title"] or "") for m in ctx["long_thesis"])
    # 자동 적용 아님
    assert ctx["advisory_only"] is True and ctx["applied"] is False


def test_conflict_long_positive_vs_short_outflow():
    """장기 긍정 vs 단기 수급/가격 악화 → conflicts 에 노출(숨기지 않음)."""
    # self-contained: 상충 판정에 필요한 수급/가격/견해 + 장기 긍정 thesis 를 직접 시드 — 앞 테스트 의존 제거.
    conn = store_db.connect()
    try:
        _seed_market(conn)
    finally:
        conn.close()
    am.record("stock", "005930", "fact", ticker="005930", title="외국인 최근 순매도 전환",
              source="kis_investor", source_date="2026-06-20", freshness=0.9, confidence=0.6)
    am.record("stock", "005930", "lesson", ticker="005930", time_horizon="long",
              title="장기 긍정 thesis", positive_factors=["HBM 수요"],
              source="report", source_date="2026-06-15", freshness=0.8, confidence=0.7)
    ctx = ph.prehook_context(5, "stock", "005930", theme="반도체")
    types = {c["type"] for c in ctx["conflicts"]}
    assert "long_positive_vs_short_outflow" in types or "long_positive_vs_price_drop" in types
    # 요약 cautions 에도 상충 반영
    assert any("상충" in c for c in ctx["summary"]["cautions"])


# ──────────────── 분리 뷰 ────────────────
def test_stale_separated_in_prehook():
    am.record("stock", "005930", "fact", ticker="005930", title="오래된 정보",
              source="x", source_date="2026-01-01", freshness=0.3, stale_at=_past(1))
    ctx = ph.prehook_context(5, "stock", "005930")
    assert any(m["title"] == "오래된 정보" for m in ctx["stale"])
    # stale 은 fresh 공통 메모리(장기 thesis 등)와 섞이지 않음
    assert not any(m["title"] == "오래된 정보" for m in ctx["long_thesis"])


def test_weak_unsourced_separated():
    am.record("stock", "005930", "interpretation", ticker="005930",
              title="근거없는 강세 단정", confidence=0.95)  # 출처 없음 → weak
    ctx = ph.prehook_context(5, "stock", "005930")
    assert any(m["title"] == "근거없는 강세 단정" for m in ctx["weak_unsourced"])
    assert any("출처 없는" in c for c in ctx["summary"]["cautions"])


# ──────────────── 계좌 격리 ────────────────
def test_prehook_account_isolation():
    """다른 계좌(99)로 조회하면 계좌5 user_view 가 섞이지 않는다."""
    ctx99 = ph.prehook_context(99, "stock", "005930")
    assert not any(v.get("note") == "삼성전자 장기 긍정" for v in ctx99["user_views"])
    # 사용자 관점 메모리도 계좌 격리
    assert all(m["account_index"] == 99 for m in ctx99["asset_memory_user"])


# ──────────────── 시장반응(lesson) 통합 ────────────────
def test_prehook_includes_lesson_reliability():
    r = lr.record_lesson("stock", "005930", suggested_action="buy")
    lr.record_outcome(r["id"], 20, {"return_pct": 5.0})
    ctx = ph.prehook_context(5, "stock", "005930")
    assert ctx["reliability"]["evaluated"] >= 1
    assert any(run["scope_key"] == "005930" for run in ctx["lesson_runs"])


def test_no_account_market_only():
    """account None 이면 시장 공통 조회(user_views/allocation 없음, 공통 메모리만)."""
    ctx = ph.prehook_context(None, "stock", "005930")
    assert ctx["user_views"] == []
    assert ctx["asset_memory_user"] == []
    assert ctx["selected_allocation"] is None
    assert isinstance(ctx["asset_memory_shared"], list)


def test_priority_order_account_first():
    """개선 1: 계좌 확정안이 최우선, 공통 자산지식보다 앞."""
    ctx = ph.prehook_context(5, "stock", "005930")
    assert ctx["priority_order"] == list(ph.PRIORITY_ORDER)
    assert ctx["priority_order"][0] == "account_selected_allocation"
    # account_* 키들이 공통 asset_memory_shared 보다 앞 인덱스
    po = ctx["priority_order"]
    assert po.index("account_objective") < po.index("asset_memory")
    assert po.index("account_policy") < po.index("user_global_views")


def test_sections_six_categories():
    """개선 3: 공통 builder 6분류 섹션 노출."""
    ctx = ph.build_context(5, "stock", "005930")   # build_context = 공통 builder 별칭
    assert ph.build_context is ph.prehook_context
    for sec in ("latest", "long_thesis", "market_reaction", "user_response", "stale", "conflicts"):
        assert sec in ctx["sections"], sec


def test_user_response_from_lesson_user_action():
    """개선 3: 사용자 반응 기록 = user_action 이 기록된 lesson_run."""
    setup()
    lr.record_lesson("stock", "005930", account_index=5,
                     decision_context="진입 보류 제안", user_action="ignored",
                     lesson_text="사용자가 무시")
    ctx = ph.prehook_context(5, "stock", "005930")
    ur = ctx["user_response"]
    assert any(r.get("user_action") == "ignored" for r in ur), ur
    assert ctx["sections"]["user_response"] == ur


def test_asset_memory_with_lessons_unifies_scope():
    """개선 2: 자산 memory + lesson_run/reliability 한 번에(7 scope 공통)."""
    setup()
    am.record("etf", "069500", "fact", title="코스피200 추종", body="대표 ETF")
    lr.record_lesson("etf", "069500", decision_context="비교 제시")
    out = ph.asset_memory_with_lessons("etf", "069500")
    assert out["scope_type"] == "etf"
    assert isinstance(out["memory_shared"], list) and len(out["memory_shared"]) >= 1
    assert isinstance(out["lesson_runs"], list) and len(out["lesson_runs"]) >= 1
    assert "reliability" in out["reliability"]


def test_selected_allocation_loaded_from_canonical_source():
    """개선 1: 확정안(allocation_selections, status='active')이 prehook 최우선 truth 로 실제 반영.

    과거엔 존재하지 않는 'selected_allocation' 테이블 조회로 항상 None 이었음(SSOT 드리프트 버그).
    """
    setup()
    now = datetime.now(timezone.utc).isoformat()
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO allocation_selections(account_index, variant, allocation, status, selected_by, selected_at) "
            "VALUES(?,?,?,?,?,?)",
            (7, "base", json.dumps([{"ticker": "SPY", "weight_pct": 60}]), "active", "user", now))
        # 다른 계좌 확정안 — 계좌 격리 확인용
        conn.execute(
            "INSERT INTO allocation_selections(account_index, variant, allocation, status, selected_by, selected_at) "
            "VALUES(?,?,?,?,?,?)",
            (8, "aggressive", json.dumps([{"ticker": "QQQ", "weight_pct": 80}]), "active", "user", now))
        conn.commit()
    finally:
        conn.close()
    ctx = ph.prehook_context(7, "stock", "005930")
    sel = ctx["selected_allocation"]
    assert sel is not None, "확정안이 prehook 최우선 truth 로 로드돼야 함(SSOT)"
    assert sel.get("variant") == "base"
    assert sel["allocation_rows"][0]["ticker"] == "SPY"
    # 계좌 격리 — 8번 확정안이 7번으로 새지 않음
    assert all(r["ticker"] != "QQQ" for r in sel["allocation_rows"])
