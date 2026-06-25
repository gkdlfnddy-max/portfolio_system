"""하락 징후 성장 루프 영속화 + Dashboard 데이터 테스트.

검증(불변 규칙):
  - decline_analyses 분석 기록 저장(예측 시점) + user_action 갱신
  - 결과 평가 lookahead 차단: **분석일 이후 일봉만** 낙폭 계산(미래 누설 금지)
  - reliability 갱신 동작: hit→↑, miss→↓ (중립 0.5 기준)
  - 실현결과 적으면 reliability 중립(0.5) 유지(정직)
  - scope 격리: 종목/섹터 reliability 계좌 교차적용 금지
  - dashboard 데이터(추이·부족축·보수전환·적중/미스)
  - 자동주문 0 / Anthropic API 0
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_decline_growth.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_PATH"] = _TMP

from datetime import date, timedelta

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import decline_scan as scan_mod
from main_mission.portfolio_os import lessons as lessons_mod
from main_mission.portfolio_os.decline import analysis_log as alog
from main_mission.portfolio_os.decline import dashboard as dash
from main_mission.portfolio_os.decline import track_record as tr


def setup():
    store_db.init()


# ============================================================
# 합성 일봉 헬퍼
# ============================================================
def _bars(closes, *, start="2025-01-01", vols=None):
    d0 = date.fromisoformat(start)
    return [{"date": (d0 + timedelta(days=i)).isoformat(),
             "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
             "volume": (vols[i] if vols else 1000.0)} for i, c in enumerate(closes)]


def _crash_then_more(pre_n=70, drop=0.03, drop_n=20):
    """상승 → (분석 시점) → 이후 급락. 분석 시점 = pre 마지막 바."""
    up = [100.0 + 1.5 * i for i in range(pre_n)]   # 가파른 상승(과열) → 위험 발화
    peak = up[-1]
    after = [peak * (1 - drop * (k + 1)) for k in range(drop_n)]  # 분석일 이후 급락
    return _bars(up + after), pre_n - 1  # (history, analysis_idx)


def _recover_after(pre_n=70, up_n=20):
    """상승(과열) → (분석 시점) → 이후 계속 상승(예측 미스)."""
    up = [100.0 + 1.5 * i for i in range(pre_n)]
    peak = up[-1]
    after = [peak * (1 + 0.01 * (k + 1)) for k in range(up_n)]
    return _bars(up + after), pre_n - 1


def _seed_hit_and_miss():
    """HITCODE(적중)·MISSCODE(미스) 실현결과를 *이 테스트 안에서* 생성.

    conftest 의 per-test DB 격리(autouse) 때문에 다른 테스트가 남긴 기록은 보이지 않는다.
    따라서 scope/scoreboard 를 검증하는 테스트는 자체적으로 hit/miss 를 시드해야 한다.
    """
    hist_h, idx_h = _crash_then_more(drop=0.04)
    scan_h = scan_mod.scan_instrument("HITCODE", history=hist_h[: idx_h + 1], sector="섹터HIT")
    rid_h = alog.record_analysis("HITCODE", scan_h, analysis_date=hist_h[idx_h]["date"],
                                 sector="섹터HIT")["analysis_id"]
    alog.evaluate_outcome(rid_h, history=hist_h, future_window=10)
    hist_m, idx_m = _recover_after()
    scan_m = scan_mod.scan_instrument("MISSCODE", history=hist_m[: idx_m + 1])
    rid_m = alog.record_analysis("MISSCODE", scan_m, analysis_date=hist_m[idx_m]["date"])["analysis_id"]
    alog.evaluate_outcome(rid_m, history=hist_m, future_window=10)


# ============================================================
# 1. 분석 기록 저장 + user_action
# ============================================================
def test_record_analysis_persists_prediction():
    hist, idx = _crash_then_more()
    pre = hist[: idx + 1]                 # 예측 시점까지만(lookahead 없음)
    scan = scan_mod.scan_instrument("GROW1", history=pre, sector="반도체")
    res = alog.record_analysis("GROW1", scan, analysis_date=hist[idx]["date"],
                               account_index=1, sector="반도체")
    assert res["ok"] and res["analysis_id"]
    conn = store_db.connect()
    try:
        row = conn.execute("SELECT * FROM decline_analyses WHERE analysis_id=?",
                           (res["analysis_id"],)).fetchone()
    finally:
        conn.close()
    assert row["code"] == "GROW1"
    assert row["hit_or_miss"] == "pending"
    assert row["overall_risk"] is not None


def test_set_user_action():
    hist, idx = _crash_then_more()
    scan = scan_mod.scan_instrument("GROW_UA", history=hist[: idx + 1])
    rid = alog.record_analysis("GROW_UA", scan, analysis_date=hist[idx]["date"])["analysis_id"]
    ok = alog.set_user_action(rid, "saved_to_policy", policy_draft_created=True)
    assert ok["ok"]
    bad = alog.set_user_action(rid, "nonsense")
    assert bad["ok"] is False


# ============================================================
# 2. lookahead 차단 — 분석일 이후 일봉만 결과평가
# ============================================================
def test_lookahead_guard_only_future_bars():
    hist, idx = _crash_then_more()
    analysis_date = hist[idx]["date"]
    fut = alog._future_drawdown(hist, analysis_date, window=10)
    assert fut is not None
    # 사용된 윈도우는 전부 분석일 **이후**여야 한다(미래 누설 금지)
    assert fut["window_from"] > analysis_date
    assert fut["window_to"] > analysis_date
    # 분석일까지만 있는 history 면 미래 데이터 없음 → 평가 보류(정직)
    none_fut = alog._future_drawdown(hist[: idx + 1], analysis_date, window=10)
    assert none_fut is None


def test_evaluate_defers_without_future_data():
    hist, idx = _crash_then_more()
    pre = hist[: idx + 1]
    scan = scan_mod.scan_instrument("GROW_DEFER", history=pre)
    rid = alog.record_analysis("GROW_DEFER", scan, analysis_date=hist[idx]["date"])["analysis_id"]
    # 미래 일봉 없는 history 만 줌 → 평가 보류
    res = alog.evaluate_outcome(rid, history=pre)
    assert res["ok"] is False and res["reason"] == "no_future_data_yet"


# ============================================================
# 3. reliability 갱신 — hit→↑, miss→↓
# ============================================================
def test_hit_raises_reliability():
    hist, idx = _crash_then_more(drop=0.04)        # 급락 → 예측 적중
    pre = hist[: idx + 1]
    scan = scan_mod.scan_instrument("HITCODE", history=pre, sector="섹터HIT")
    rid = alog.record_analysis("HITCODE", scan, analysis_date=hist[idx]["date"],
                               sector="섹터HIT")["analysis_id"]
    res = alog.evaluate_outcome(rid, history=hist, future_window=10)
    assert res["ok"] and res["predicted_decline"] is True
    assert res["hit_or_miss"] == "hit", res
    assert res["actual_drawdown"] < 0
    # 중립(0.5)에서 hit → reliability 상승
    assert res["reliability_after"] > res["reliability_before"], res
    assert tr.reliability_scoped("instrument", "HITCODE")["reliability"] > 0.5


def test_miss_lowers_reliability():
    hist, idx = _recover_after()                    # 이후 상승 → 예측 미스
    pre = hist[: idx + 1]
    scan = scan_mod.scan_instrument("MISSCODE", history=pre)
    rid = alog.record_analysis("MISSCODE", scan, analysis_date=hist[idx]["date"])["analysis_id"]
    res = alog.evaluate_outcome(rid, history=hist, future_window=10)
    assert res["ok"] and res["predicted_decline"] is True
    assert res["hit_or_miss"] == "miss", res
    # 중립(0.5)에서 miss → reliability 하락
    assert res["reliability_after"] < res["reliability_before"], res
    assert tr.reliability_scoped("instrument", "MISSCODE")["reliability"] < 0.5


def test_reliability_neutral_without_results():
    # 실현결과 한 번도 없으면 중립 0.5 유지(정직)
    rel = tr.reliability_scoped("instrument", "NEVER_SEEN")
    assert rel["reliability"] == 0.5 and rel["source"] == "no_track_record"


# ============================================================
# 4. scope 격리 — 종목 reliability 계좌 교차적용 금지(시장 공통, 종목별)
# ============================================================
def test_scope_isolation_per_instrument():
    _seed_hit_and_miss()  # per-test DB 격리 — 자체 시드
    # HITCODE(적중) 와 MISSCODE(미스) 는 서로 reliability 섞이지 않음
    hit_rel = tr.reliability_scoped("instrument", "HITCODE")["reliability"]
    miss_rel = tr.reliability_scoped("instrument", "MISSCODE")["reliability"]
    assert hit_rel > 0.5 and miss_rel < 0.5
    # 종목 scope 와 섹터 scope 도 분리
    sec = tr.reliability_scoped("sector", "섹터HIT")
    assert sec["scope"] == "sector"


# ============================================================
# 5. Dashboard 데이터
# ============================================================
def test_dashboard_data_shapes():
    d = dash.dashboard(account_index=1)
    assert d["ok"] and d["auto_order_created"] is False and d["read_only"] is True
    assert isinstance(d["risk_trend"], list)
    sb = d["prediction_scoreboard"]
    assert "hits" in sb and "misses" in sb and "scored" in sb
    mf = d["missing_axes_freq"]
    assert "missing_axes" in mf and "available_axes" in mf


def test_dashboard_scoreboard_counts_results():
    _seed_hit_and_miss()  # per-test DB 격리 — 자체 시드(hit 1·miss 1)
    sb = dash.prediction_scoreboard()
    assert sb["hits"] >= 1 and sb["misses"] >= 1
    assert sb["scored"] == sb["hits"] + sb["misses"]


def test_dashboard_risk_trend_chronological():
    trend = dash.risk_trend(account_index=1)
    dates = [r["analysis_date"] for r in trend]
    assert dates == sorted(dates)  # 오래된→최신


# ============================================================
# 6. 자동주문 0
# ============================================================
def test_no_orders_created():
    conn = store_db.connect()
    try:
        before = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    finally:
        conn.close()
    hist, idx = _crash_then_more()
    scan = scan_mod.scan_instrument("NOORDER", history=hist[: idx + 1])
    rid = alog.record_analysis("NOORDER", scan, analysis_date=hist[idx]["date"])["analysis_id"]
    alog.evaluate_outcome(rid, history=hist)
    dash.dashboard()
    conn = store_db.connect()
    try:
        after = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    finally:
        conn.close()
    assert after == before, (before, after)


# ============================================================
# 7. evaluate_pending 일괄
# ============================================================
def test_evaluate_pending_batch():
    res = alog.evaluate_pending(future_window=10)
    assert res["ok"]
    assert "evaluated_count" in res and "deferred_count" in res


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} DECLINE-GROWTH TESTS PASSED")
