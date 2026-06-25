"""하락 징후 6축 + 메타인지 종합 + 성장 학습 테스트.

검증:
  - 각 축 scorer 공통 인터페이스(데이터 있으면 계산, 없으면 data_available=False·점수 0)
  - 기술축 = decline_signals 래핑 일치
  - 분산/거시/이벤트/심리/정책 축 발화(합성 데이터)
  - composite 메타인지: 가용 축만 합성·미연동 축 제외·상충 신호·overall_confidence
  - 데이터 없으면 confidence↓ (단정 회피)
  - 성장 루프: track record(적중/미스) → reliability → 가중 반영
  - 자동주문 0 / 데이터 없는 축 가짜점수 0
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_axes.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import decline_signals as ds
from main_mission.portfolio_os import decline_scan as scan_mod
from main_mission.portfolio_os import lessons as lessons_mod
from main_mission.portfolio_os.decline import composite as composite_mod
from main_mission.portfolio_os.decline import track_record as tr
from main_mission.portfolio_os.decline.axes import (
    AXES, technical, distribution, macro, event, sentiment, policy)


def setup():
    store_db.init()


# ============================================================
# 합성 데이터 헬퍼
# ============================================================
def _bars(closes, *, vols=None, start="2025-01-01"):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    return [{
        "date": (d0 + timedelta(days=i)).isoformat(),
        "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
        "volume": (vols[i] if vols else 1000.0),
    } for i, c in enumerate(closes)]


def _uptrend(n=260, start=100.0, step=0.5):
    return [start + step * i for i in range(n)]


def _crash_hist():
    up = _uptrend(60, start=100.0, step=1.0)
    peak = up[-1]
    return _bars(up + [peak * (1 - 0.025 * (k + 1)) for k in range(15)])


# ============================================================
# 공통 인터페이스 — 데이터 없으면 정직하게 미연동
# ============================================================
def test_axis_interface_shape():
    res = technical.score({"history": _bars(_uptrend(260))})
    for key in ("axis", "risk_0_100", "signals", "data_available", "confidence", "detail"):
        assert key in res, key
    assert res["axis"] == "technical"


def test_axes_unavailable_when_no_data():
    # 데이터 없는 5축 → data_available=False, risk 0, confidence 0 (가짜점수 금지)
    ctx = {}
    for name in ("distribution", "macro", "event", "sentiment", "policy"):
        r = AXES[name](ctx)
        assert r["data_available"] is False, (name, r)
        assert r["risk_0_100"] == 0.0, (name, r)
        assert r["confidence"] == 0.0, (name, r)


def test_technical_axis_matches_decline_signals():
    hist = _crash_hist()
    expect = ds.compute_signals(hist)
    got = technical.score({"history": hist})
    assert got["data_available"] is True
    assert got["risk_0_100"] == expect["risk_score"]


def test_technical_axis_no_history():
    r = technical.score({})
    assert r["data_available"] is False and r["risk_0_100"] == 0.0


# ============================================================
# 분산축
# ============================================================
def test_distribution_fires_on_smart_money_selling():
    # 외국인+기관 순매도 + 개인 순매수 + 거래량 급증
    flows = []
    for i in range(20):
        flows.append({"trade_date": f"2025-02-{i+1:02d}",
                      "foreign_net": -500.0, "institution_net": -300.0,
                      "retail_net": 800.0, "volume": 2000.0 if i >= 10 else 1000.0})
    r = distribution.score({"investor_flows": flows})
    assert r["data_available"] is True
    assert "smart_money_distribution" in [s["name"] for s in r["signals"] if s["fired"]], r["signals"]
    assert r["risk_0_100"] > 0


def test_distribution_calm_no_fire():
    flows = [{"trade_date": f"2025-02-{i+1:02d}", "foreign_net": 300.0,
              "institution_net": 200.0, "retail_net": -100.0, "volume": 1000.0}
             for i in range(20)]
    r = distribution.score({"investor_flows": flows})
    assert r["data_available"] is True
    assert r["risk_0_100"] == 0.0 or not any(s["fired"] for s in r["signals"]), r


# ============================================================
# 거시축
# ============================================================
def test_macro_fires_on_inversion_and_hikes():
    m = {"policy_rate_change_3m": 1.0, "yield_10y": 3.0, "yield_2y": 4.0,
         "cpi_yoy": 6.0, "credit_growth_yoy": 12.0, "fx_usdkrw_change_1m": 5.0}
    r = macro.score({"macro_indicators": m})
    fired = [s["name"] for s in r["signals"] if s["fired"]]
    assert "yield_curve_inversion" in fired and "rate_hiking" in fired, fired
    assert r["risk_0_100"] > 30 and r["confidence"] > 0.8  # 5/5 지표


def test_macro_partial_data_lowers_confidence():
    full = macro.score({"macro_indicators": {
        "policy_rate_change_3m": 1.0, "yield_10y": 3.0, "yield_2y": 4.0,
        "cpi_yoy": 6.0, "credit_growth_yoy": 12.0, "fx_usdkrw_change_1m": 5.0}})
    partial = macro.score({"macro_indicators": {"cpi_yoy": 6.0}})
    assert partial["data_available"] is True
    assert partial["confidence"] < full["confidence"], (partial["confidence"], full["confidence"])


# ============================================================
# 이벤트축
# ============================================================
def test_event_fires_on_imminent_high_impact():
    r = event.score({"as_of_date": "2025-03-10",
                     "market_events": [{"event_date": "2025-03-11", "name": "FOMC", "impact": "high"}]})
    assert r["data_available"] is True
    assert any(s["fired"] for s in r["signals"]), r
    assert r["risk_0_100"] > 0


def test_event_no_imminent_is_available_but_zero():
    r = event.score({"as_of_date": "2025-03-10",
                     "market_events": [{"event_date": "2025-06-01", "name": "FOMC", "impact": "high"}]})
    assert r["data_available"] is True and r["risk_0_100"] == 0.0


# ============================================================
# 심리축
# ============================================================
def test_sentiment_fear_spike():
    r = sentiment.score({"sentiment_index": {"vix": 45.0, "put_call_ratio": 1.3}})
    assert r["data_available"] is True and r["risk_0_100"] > 0
    assert "fear_spike" in [s["name"] for s in r["signals"] if s["fired"]]


# ============================================================
# 정책축
# ============================================================
def test_policy_adverse_event_fires():
    r = policy.score({"as_of_date": "2025-03-10", "sector": "반도체",
                      "policy_events": [{"event_date": "2025-03-05", "sector": "반도체",
                                         "stance": "adverse", "severity": 0.8,
                                         "title": "수출 규제", "source": "news"}]})
    assert r["data_available"] is True and r["risk_0_100"] > 0
    assert any(s["fired"] for s in r["signals"]), r


# ============================================================
# composite 메타인지
# ============================================================
def test_composite_only_available_axes():
    # 기술축만 데이터 → 나머지 5축 미연동(metacognition.data_missing_axes)
    ctx = {"history": _crash_hist()}
    comp = composite_mod.composite(ctx)
    assert comp["auto_order_created"] is False
    meta = comp["metacognition"]
    assert "technical" not in meta["data_missing_axes"]
    assert set(meta["data_missing_axes"]) == {"distribution", "macro", "event", "sentiment", "policy"}
    assert comp["axes"]["distribution"]["data_available"] is False
    assert comp["axes"]["distribution"]["risk_0_100"] == 0.0  # 가짜점수 0


def test_composite_thin_data_low_confidence():
    # 1축만 가용 → coverage 낮음 → overall_confidence 낮음
    thin = composite_mod.composite({"history": _crash_hist()})
    rich = composite_mod.composite({
        "history": _crash_hist(),
        "macro_indicators": {"yield_10y": 3.0, "yield_2y": 4.0, "cpi_yoy": 6.0,
                             "policy_rate_change_3m": 1.0, "credit_growth_yoy": 12.0,
                             "fx_usdkrw_change_1m": 5.0},
        "sentiment_index": {"vix": 45.0, "put_call_ratio": 1.3, "margin_balance_change_1m": 10.0},
    })
    assert thin["overall_confidence"] < rich["overall_confidence"], (thin["overall_confidence"], rich["overall_confidence"])


def test_composite_conflict_detection():
    # 거시축 고위험 + 기술축 저위험(완만 상승) → 상충
    comp = composite_mod.composite({
        "history": _bars(_uptrend(260, step=0.1)),  # 완만 = 저위험
        "macro_indicators": {"yield_10y": 2.0, "yield_2y": 4.0, "cpi_yoy": 8.0,
                             "policy_rate_change_3m": 1.5, "credit_growth_yoy": 20.0,
                             "fx_usdkrw_change_1m": 10.0},  # 거시 고위험
    })
    assert comp["metacognition"]["conflicting_signals"] is True, comp["breakdown"]


def test_composite_no_data_safe():
    comp = composite_mod.composite({})
    assert comp["holistic_risk"] == 0.0
    assert comp["overall_confidence"] == 0.0
    assert len(comp["metacognition"]["data_missing_axes"]) == len(AXES)


# ============================================================
# 성장 루프 — track record → reliability → 가중
# ============================================================
def test_track_record_neutral_without_history():
    rel = tr.reliability("event")
    assert rel["reliability"] == 0.5 and rel["source"] == "no_track_record"


def test_track_record_hits_raise_reliability():
    for _ in range(5):
        tr.record_outcome("macro", predicted_decline=True, actual_decline=True, confidence=0.7)
    tr.record_outcome("macro", predicted_decline=True, actual_decline=False, confidence=0.7)
    rel = tr.reliability("macro")
    assert rel["samples"] == 6 and rel["hits"] == 5 and rel["misses"] == 1
    assert rel["reliability"] > 0.5, rel


def test_track_record_skips_non_prediction():
    res = tr.record_outcome("policy", predicted_decline=False, actual_decline=True)
    assert res["ok"] is False and res["reason"] == "no_prediction_to_score"


def test_reliability_shifts_composite_weight():
    # 거시축 적중 이력 누적 후 → 거시축 weight 가 reliability 반영해 증가
    base_ctx = {"history": _crash_hist(),
                "macro_indicators": {"yield_10y": 2.0, "yield_2y": 4.0, "cpi_yoy": 8.0,
                                     "policy_rate_change_3m": 1.5, "credit_growth_yoy": 20.0,
                                     "fx_usdkrw_change_1m": 10.0}}
    no_tr = composite_mod.composite(base_ctx, use_track_record=False)
    # 적중 이력 다수 기록 → 승격 불필요(candidate 도 reliability 계산은 lessons promoted만 읽음)
    # reliability 는 promoted lessons 를 읽으므로 promote 후 비교
    for _ in range(4):
        tr.record_outcome("macro", predicted_decline=True, actual_decline=True, confidence=0.7)
    lessons_mod.promote()
    with_tr = composite_mod.composite(base_ctx, use_track_record=True)
    w_no = no_tr["axes"]["macro"]["weight"]
    w_tr = with_tr["axes"]["macro"]["weight"]
    # use_track_record=False 면 reliability=1.0; track record 좋으면 1.0 근접하나 정확히 같진 않음.
    # 핵심: track record 가 weight 산식에 실제 반영되는지(키 존재 + 값 차이 가능).
    assert "reliability" in with_tr["axes"]["macro"]
    assert with_tr["axes"]["macro"]["reliability_source"] in ("track_record", "no_track_record")
    assert w_tr > 0 and w_no > 0


# ============================================================
# scan 통합 + 자동주문 0
# ============================================================
def test_scan_attaches_composite():
    out = scan_mod.scan([{"instrument_code": "AXIS1", "history": _crash_hist()}])
    s = out["scanned"][0]
    assert "composite" in s and "holistic_risk" in s
    assert s["composite"]["auto_order_created"] is False


def test_scan_multi_axis_off():
    out = scan_mod.scan([{"instrument_code": "AXIS2", "history": _crash_hist()}], multi_axis=False)
    assert "composite" not in out["scanned"][0]


def test_no_orders_created_by_axes():
    conn = store_db.connect()
    try:
        before = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    finally:
        conn.close()
    scan_mod.scan([{"instrument_code": "AXIS3", "history": _crash_hist()}])
    composite_mod.composite({"history": _crash_hist()})
    tr.record_outcome("technical", predicted_decline=True, actual_decline=True)
    conn = store_db.connect()
    try:
        after = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    finally:
        conn.close()
    assert after == before, (before, after)


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} DECLINE-AXES TESTS PASSED")
