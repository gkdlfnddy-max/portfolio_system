"""이벤트 캘린더 + 심리축 테스트 (decline 5/6축 데이터 연결).

검증:
  - 이벤트 캘린더 적재(멱등) / 조회 / 다가오는 이벤트
  - 이벤트 위험 알림 = 일정 기반(예측 아님) · 자동주문 0 · 데이터 없으면 data_available=False
  - 심리지표 적재(sentiment_index) · 거시와 분리 · VIX 하나면 confidence 낮음(과장 금지)
  - event/sentiment 축이 DB 데이터로 data_available=True, 없으면 False(가짜 0 금지)
  - decline.context 가 두 축에 데이터 주입 → composite 5/6축 가능
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_event_sentiment.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import event_calendar as ec
from main_mission.portfolio_os.decline.axes import event as event_axis
from main_mission.portfolio_os.decline.axes import sentiment as sentiment_axis
from main_mission.portfolio_os.decline import context as ctx_mod
from main_mission.portfolio_os.decline import composite as composite_mod


def setup():
    os.environ["SQLITE_PATH"] = _TMP
    store_db.init()


def setup_function(_fn=None):
    os.environ["SQLITE_PATH"] = _TMP
    conn = store_db.connect()
    try:
        conn.execute("DELETE FROM market_events")
        conn.execute("DELETE FROM sentiment_index")
        conn.commit()
    finally:
        conn.close()


def _iso(days_from_now: int) -> str:
    return (date.today() + timedelta(days=days_from_now)).isoformat()


# ============================================================
# 1) 이벤트 캘린더 적재 / 멱등 / 조회
# ============================================================
def test_add_event_and_list():
    assert ec.add_event("2026-07-30", "FOMC", impact="high", region="US") is True
    rows = ec.list_events()
    assert len(rows) == 1 and rows[0]["name"] == "FOMC"
    assert rows[0]["impact"] == "high" and rows[0]["source"] == "manual"


def test_add_event_idempotent():
    ec.add_event("2026-07-30", "FOMC", impact="high", region="US")
    ec.add_event("2026-07-30", "FOMC", impact="medium", region="US")  # 같은 키 → 갱신만
    rows = ec.list_events()
    assert len(rows) == 1
    assert rows[0]["impact"] == "medium"  # 갱신됨


def test_add_event_rejects_bad_input():
    assert ec.add_event(None, "FOMC") is False        # 날짜 없음
    assert ec.add_event("2026-07-30", "") is False     # 이름 없음
    assert ec.add_event("not-a-date", "FOMC") is False  # 파싱 불가(가짜 날짜 금지)
    assert ec.list_events() == []


def test_seed_official_schedule_fills_template_defaults():
    n = ec.seed_official_schedule([
        {"name": "FOMC", "event_date": "2026-07-30"},          # impact/region 템플릿 보완
        {"name": "한국 금통위", "event_date": "2026-08-28"},
        {"name": "FOMC"},  # 날짜 없음 → skip(placeholder 금지)
    ])
    assert n == 2
    rows = {r["name"]: r for r in ec.list_events()}
    assert rows["FOMC"]["impact"] == "high" and rows["FOMC"]["region"] == "US"
    assert rows["FOMC"]["source"] == "official"


def test_upcoming_events_window_and_order():
    ec.add_event(_iso(2), "CPI", impact="high", region="US")
    ec.add_event(_iso(1), "FOMC", impact="high", region="US")
    ec.add_event(_iso(30), "한국 CPI", impact="medium", region="KR")  # 윈도우 밖
    up = ec.upcoming_events(window_days=7)
    names = [e["name"] for e in up]
    assert names == ["FOMC", "CPI"]            # 임박순
    assert up[0]["days_until"] == 1


# ============================================================
# 2) 이벤트 위험 알림 — 예측 아님 · 자동주문 0 · 정직한 data_available
# ============================================================
def test_event_alert_no_data_is_unavailable():
    a = ec.event_risk_alert()
    assert a["data_available"] is False        # 일정 미연동 → 가짜 0 아님
    assert a["alert"] is False and a["upcoming"] == []


def test_event_alert_fires_on_imminent_high():
    ec.add_event(_iso(1), "FOMC", impact="high", region="US")
    a = ec.event_risk_alert()
    assert a["data_available"] is True
    assert a["alert"] is True
    assert len(a["imminent"]) == 1 and a["imminent"][0]["name"] == "FOMC"
    assert a["suggestions"]                     # 관망/현금/헤지 후보(사람 승인)
    # 예측(방향) 단어 없음 — 일정 기반 변동성 알림만.
    assert "예측" in a["note"] and "예측 아님" in a["note"]


def test_event_alert_distant_event_no_alert():
    ec.add_event(_iso(20), "FOMC", impact="high", region="US")
    a = ec.event_risk_alert()
    assert a["data_available"] is True          # 데이터는 있음
    assert a["alert"] is False                   # 임박 아님 → 경보 없음(정직)


def test_event_alert_no_auto_order():
    ec.add_event(_iso(1), "FOMC", impact="high", region="US")
    conn = store_db.connect()
    try:
        before = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    finally:
        conn.close()
    ec.event_risk_alert()
    conn = store_db.connect()
    try:
        after = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    finally:
        conn.close()
    assert after == before


# ============================================================
# 3) 심리지표 적재 / 스냅샷 / 커버리지 (거시와 분리)
# ============================================================
def test_upsert_sentiment_and_snapshot():
    assert ec.upsert_sentiment("vix", "2026-06-20", 28.0, "market") is True
    assert ec.upsert_sentiment("vkospi", "2026-06-20", 22.0, "krx") is True
    snap = ec.sentiment_snapshot()
    assert snap["vix"]["value"] == 28.0
    assert snap["vkospi"]["value"] == 22.0


def test_upsert_sentiment_rejects_bad_value():
    assert ec.upsert_sentiment("vix", "2026-06-20", None) is False
    assert ec.upsert_sentiment("vix", "2026-06-20", "high") is False  # 비숫자 → 가짜 0 금지
    assert ec.upsert_sentiment("vix", "bad-date", 28.0) is False
    assert ec.sentiment_snapshot() == {}


def test_sentiment_upsert_idempotent():
    ec.upsert_sentiment("vix", "2026-06-20", 28.0, "market")
    ec.upsert_sentiment("vix", "2026-06-20", 31.0, "market")  # 같은 PK → 갱신
    assert ec.sentiment_snapshot()["vix"]["value"] == 31.0


def test_sentiment_coverage_honest_no_overclaim():
    cov0 = ec.sentiment_coverage()
    assert cov0["data_available"] is False and cov0["present_count"] == 0
    ec.upsert_sentiment("vix", "2026-06-20", 28.0)
    cov1 = ec.sentiment_coverage()
    # VIX 하나로 "심리축 완성" 과장 금지 — confidence_hint 낮음(1/5).
    assert cov1["present_count"] == 1
    assert cov1["confidence_hint"] < 0.6
    # 지표 늘면 confidence_hint 상승.
    ec.upsert_sentiment("vkospi", "2026-06-20", 22.0)
    ec.upsert_sentiment("put_call_ratio", "2026-06-20", 1.1)
    cov3 = ec.sentiment_coverage()
    assert cov3["confidence_hint"] > cov1["confidence_hint"]


# ============================================================
# 4) 축 통합 — DB 데이터 → event/sentiment 축 data_available=True
# ============================================================
def test_event_axis_via_context_available():
    ec.add_event(_iso(1), "FOMC", impact="high", region="US")
    ctx = ctx_mod.build_context("TEST_INST", history=[], as_of_date=date.today().isoformat())
    r = event_axis.score(ctx)
    assert r["data_available"] is True
    assert any(s["fired"] for s in r["signals"]), r
    assert r["risk_0_100"] > 0


def test_event_axis_unavailable_when_empty():
    ctx = ctx_mod.build_context("TEST_INST", history=[], as_of_date=date.today().isoformat())
    r = event_axis.score(ctx)
    assert r["data_available"] is False and r["risk_0_100"] == 0.0  # 가짜 0


def test_sentiment_axis_via_context_available():
    ec.upsert_sentiment("vix", date.today().isoformat(), 45.0, "market")
    ec.upsert_sentiment("vkospi", date.today().isoformat(), 38.0, "krx")
    ctx = ctx_mod.build_context("TEST_INST", history=[], as_of_date=date.today().isoformat())
    r = sentiment_axis.score(ctx)
    assert r["data_available"] is True and r["risk_0_100"] > 0
    assert "fear_spike" in [s["name"] for s in r["signals"] if s["fired"]]


def test_sentiment_axis_vix_only_low_confidence():
    one = sentiment_axis.score({"sentiment_index": {"vix": 45.0}})
    five = sentiment_axis.score({"sentiment_index": {
        "vix": 45.0, "vkospi": 40.0, "put_call_ratio": 1.3,
        "margin_balance_change_1m": 12.0, "trading_value_change": 60.0}})
    assert one["data_available"] is True
    assert one["confidence"] < five["confidence"]  # 1개 < 5개 (과장 금지)


def test_sentiment_axis_recognizes_new_factors():
    r = sentiment_axis.score({"sentiment_index": {
        "vkospi": 42.0, "trading_value_change": 80.0}})
    fired = [s["name"] for s in r["signals"] if s["fired"]]
    assert "fear_spike" in fired       # VKOSPI 공포
    assert "volume_surge" in fired     # 거래대금 급증


def test_sentiment_axis_unavailable_when_empty():
    r = sentiment_axis.score({})
    assert r["data_available"] is False and r["risk_0_100"] == 0.0


# ============================================================
# 5) composite 5/6축 — 데이터 붙는 만큼 미연동 축 줄어듦
# ============================================================
def test_composite_event_sentiment_reduce_missing_axes():
    ec.add_event(_iso(1), "FOMC", impact="high", region="US")
    ec.upsert_sentiment("vix", date.today().isoformat(), 45.0, "market")
    ec.upsert_sentiment("vkospi", date.today().isoformat(), 40.0, "krx")
    ctx = ctx_mod.build_context(
        "TEST_INST",
        history=[{"date": "2025-01-0%d" % (i + 1), "open": 100, "high": 101,
                  "low": 99, "close": 100, "volume": 1000} for i in range(5)],
        as_of_date=date.today().isoformat())
    comp = composite_mod.composite(ctx)
    missing = comp["metacognition"]["data_missing_axes"]
    assert "event" not in missing       # 이벤트 데이터 붙음
    assert "sentiment" not in missing   # 심리 데이터 붙음
    assert comp["auto_order_created"] is False


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        setup_function()
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} EVENT/SENTIMENT TESTS PASSED")
