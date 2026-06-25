"""Daily Review 수급(분산축) 섹션 테스트 — 외국인/기관/개인 흐름 요약+해석.

원칙 검증:
  - 데이터 있으면 외국인/기관/개인 순매수 집계 + 해석 + confidence.
  - 데이터 부족이면 정직하게 '수급 판단 제외'(가짜 0 금지, data_available=False).
  - 단정 금지: '순매도=매도' 식 단정 없음 — 허용은 진입 속도 조절·현금밴드 상향·hedge 검토·관찰뿐.
  - broker-neutral · 자동주문 0 · policy 변경 0.
  - A(투자자 매매동향 로더) 미연동이어도 daily_review 가 깨지지 않음(graceful).
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

# 테스트 격리 — 신규 SQLITE 파일 핀(운영 PG 오염 금지).
_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_dailyflow.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import daily_review as dr


def setup():
    store_db.init()


def _seed_profile_snapshot(idx, *, holdings=None):
    """프로필 + 스냅샷(+ 보유) 시드 — 수급 점검 대상 종목을 만든다."""
    now = datetime.now(timezone.utc).isoformat()
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
            "interests_text, updated_at) VALUES(?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(account_index) DO NOTHING",
            (idx, "neutral", 10.0, 30.0, "반도체"))
        cur = conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, "
            "source, captured_at) VALUES(?,?,?,?,?,?)",
            (idx, 9_000_000, 10_000_000, len(holdings or []), "test", now))
        snap_id = cur.lastrowid
        for t in (holdings or []):
            conn.execute(
                "INSERT INTO holdings(snapshot_id, account_index, ticker, name, qty, avg_price, "
                "market_value, captured_at) VALUES(?,?,?,?,?,?,?,?)",
                (snap_id, idx, t, t, 10, 1000, 10000, now))
        conn.commit()
        return snap_id
    finally:
        conn.close()


def _seed_flows(code, rows):
    """investor_flows 시드. rows = [(date, foreign, inst, retail, volume), ...]"""
    conn = store_db.connect()
    try:
        for d, f, i, r, v in rows:
            conn.execute(
                "INSERT INTO investor_flows(instrument_code, trade_date, foreign_net, "
                "institution_net, retail_net, volume, source, captured_at) "
                "VALUES(?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(instrument_code, trade_date) DO UPDATE SET "
                "foreign_net=excluded.foreign_net",
                (code, d, f, i, r, v, "test"))
        conn.commit()
    finally:
        conn.close()


def _dates(n):
    base = datetime.now(timezone.utc).date()
    return [(base - timedelta(days=n - 1 - k)).isoformat() for k in range(n)]


# ---- 데이터 부족 → 정직 제외(가짜 0 금지) ----
def test_supply_demand_honest_excluded_when_no_data():
    idx = 201
    _seed_profile_snapshot(idx, holdings=["005930"])  # 보유는 있으나 flows 없음
    conn = store_db.connect()
    try:
        sd = dr._supply_demand_block(conn, idx)
    finally:
        conn.close()
    assert sd["data_available"] is False, sd
    assert sd["aggregate"] is None, sd
    assert "수급 판단 제외" in sd["note"], sd
    assert sd["confidence"] == 0.0, sd            # 가짜 0 점수 금지 — confidence 0
    assert sd["auto_order_created"] is False and sd["requires_user_approval"] is True, sd


def test_supply_demand_no_target_when_empty():
    idx = 202  # 보유/유니버스 없음
    _seed_profile_snapshot(idx, holdings=[])
    conn = store_db.connect()
    try:
        sd = dr._supply_demand_block(conn, idx)
    finally:
        conn.close()
    assert sd["data_available"] is False, sd
    assert "대상이 없" in sd["note"] or "대상" in sd["note"], sd


# ---- 분산 수급(외인·기관 순매도 + 개인 순매수) → 해석 + 후보(단정 금지) ----
def test_supply_demand_distribution_interpreted():
    idx = 203
    code = "000660"
    _seed_profile_snapshot(idx, holdings=[code])
    ds = _dates(5)
    # 외국인+기관 동반 순매도, 개인 순매수 — 5일 지속(분산 패턴).
    _seed_flows(code, [(ds[k], -100, -50, 200, 1000) for k in range(5)])
    conn = store_db.connect()
    try:
        sd = dr._supply_demand_block(conn, idx)
    finally:
        conn.close()
    assert sd["data_available"] is True, sd
    agg = sd["aggregate"]
    assert agg["smart_money_net_cum"] < 0, agg       # 외인+기관 합산 순매도
    assert agg["retail_net_cum"] > 0, agg            # 개인 순매수
    assert agg["foreign"] == "순매도" and agg["retail"] == "순매수", agg
    assert code in agg["distribution_names"], agg
    assert sd["interpretation"], sd                  # 해석 존재
    assert any("분산" in t for t in sd["interpretation"]), sd
    # 후보는 진입 속도 조절·현금밴드 상향·hedge 검토뿐 — '매도' 단정 후보 없음.
    kinds = {c["kind"] for c in sd["candidates"]}
    assert kinds & {"slow_entry", "cash_band_up", "hedge_review"}, sd
    for c in sd["candidates"]:
        assert c["auto"] is False, c                 # 자동주문/적용 0
        assert "매도" not in c["candidate"] or "순매도" in c["candidate"], c
    assert 0.0 < sd["confidence"] <= 0.9, sd         # 단일 축 단정 금지 — 상한 0.9


# ---- 스마트머니 유입 → 우호 해석(단정 아님) ----
def test_supply_demand_smart_inflow_interpreted():
    idx = 204
    code = "035420"
    _seed_profile_snapshot(idx, holdings=[code])
    ds = _dates(5)
    _seed_flows(code, [(ds[k], 150, 80, -100, 1000) for k in range(5)])
    conn = store_db.connect()
    try:
        sd = dr._supply_demand_block(conn, idx)
    finally:
        conn.close()
    assert sd["data_available"] is True, sd
    assert sd["aggregate"]["smart_money_net_cum"] > 0, sd
    assert any("유입" in t for t in sd["interpretation"]), sd


# ---- 데이터 적으면 confidence 낮춤(정직) ----
def test_supply_demand_min_days_excluded():
    idx = 205
    code = "051910"
    _seed_profile_snapshot(idx, holdings=[code])
    ds = _dates(2)  # 최소 일수 미만
    _seed_flows(code, [(ds[k], -100, -50, 200, 1000) for k in range(2)])
    conn = store_db.connect()
    try:
        sd = dr._supply_demand_block(conn, idx)
    finally:
        conn.close()
    # 유효 종목 0 → 정직 제외.
    assert sd["data_available"] is False, sd
    assert sd["covered_count"] == 0, sd
    names = {n["instrument_code"]: n for n in sd["names"]}
    assert names[code]["status"] == "not_enough_data", names


# ---- 통합: generate_review 가 수급 섹션을 싣고도 깨지지 않음(자동주문 0) ----
def test_review_includes_supply_demand_section():
    idx = 206
    code = "005930"
    _seed_profile_snapshot(idx, holdings=[code])
    ds = _dates(5)
    _seed_flows(code, [(ds[k], -100, -50, 200, 1000) for k in range(5)])
    r = dr.generate_review(idx)
    assert r["ok"], r
    sd = r.get("supply_demand") or (r.get("payload", {}) or {}).get("supply_demand")
    assert sd is not None, r
    assert sd["auto_order_created"] is False, sd
    # 자동주문 0 불변: 스냅샷 있으나 selected 없음 → watch(주문 후보 없음).
    assert r["has_orders"] is False, r
    assert r["scheduled_order_plan_id"] is None, r


def test_review_supply_demand_question_when_distribution():
    idx = 207
    code = "000660"
    _seed_profile_snapshot(idx, holdings=[code])
    ds = _dates(5)
    _seed_flows(code, [(ds[k], -100, -50, 200, 1000) for k in range(5)])
    r = dr.generate_review(idx)
    tq = r.get("today_questions") or (r.get("payload", {}) or {}).get("today_questions")
    assert tq is not None, r
    topics = {q["topic"] for q in tq["questions"]}
    assert "supply_demand" in topics, tq
    # 질문은 선택지 질문(단정 아님) — '매도하세요' 류 없음.
    for q in tq["questions"]:
        if q["topic"] == "supply_demand":
            assert q["options"], q
            assert "관망" in "".join(q["options"]) or "관찰" in "".join(q["options"]), q


def test_review_honest_excluded_when_no_flows():
    idx = 208
    _seed_profile_snapshot(idx, holdings=["207940"])  # flows 없는 종목(다른 테스트와 미충돌)
    r = dr.generate_review(idx)
    sd = r.get("supply_demand") or (r.get("payload", {}) or {}).get("supply_demand")
    assert sd is not None and sd["data_available"] is False, sd
    assert "수급 판단 제외" in sd["note"], sd


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} SUPPLY-DEMAND TESTS PASSED")
