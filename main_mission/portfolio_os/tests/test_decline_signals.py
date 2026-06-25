"""하락 징후 분석 엔진 테스트.

검증:
  - 신호 계산 정확성(합성 데이터: 과열/데드크로스/거래량 다이버전스/낙폭 발화)
  - 위험점수 단조성(신호 더 강하면 점수↑)
  - 보수적 전환 제안 생성(읽기 전용, cash_band↑)
  - 데이터 없을 때 안전(빈 결과 / NotEnoughData)
  - 자동주문 미생성(어떤 경로도 order 생성 0)
  - price_history upsert 멱등 + quotes seed
  - backtest 낙폭 라벨링 + 선행신호 + 노하우 누적(lessons candidate)
"""
from __future__ import annotations

import math
import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_decline.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import decline_signals as ds
from main_mission.portfolio_os import decline_scan as scan_mod
from main_mission.portfolio_os import decline_backtest as bt_mod
from main_mission.portfolio_os import price_history as ph
from main_mission.portfolio_os import lessons as lessons_mod


def setup():
    store_db.init()


# ============================================================
# 합성 데이터 생성기
# ============================================================
def _bars(closes, *, vols=None, hi_mult=1.01, lo_mult=0.99, start="2025-01-01"):
    """종가 리스트 → [{date, open, high, low, close, volume}] (오래된→최신)."""
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    out = []
    for i, c in enumerate(closes):
        out.append({
            "date": (d0 + timedelta(days=i)).isoformat(),
            "open": round(c, 4), "high": round(c * hi_mult, 4),
            "low": round(c * lo_mult, 4), "close": round(c, 4),
            "volume": (vols[i] if vols else 1000.0),
        })
    return out


def _steady_uptrend(n=260, start=100.0, step=0.5):
    return [start + step * i for i in range(n)]


# ============================================================
# 신호 정확성
# ============================================================
def test_calm_uptrend_low_risk():
    # 완만한 정배열 상승 — 과열/데드크로스/낙폭 발화 적어야
    hist = _bars(_steady_uptrend(260, step=0.3))
    res = ds.compute_signals(hist)
    assert res["risk_score"] < 25, res["risk_score"]
    assert res["risk_level"] in ("low", "elevated"), res


def test_overextended_and_rsi_fire_on_parabolic():
    # 200일 완만 + 막판 급등(포물선) → 과열(이격) + RSI 과매수 발화
    base = _steady_uptrend(200, start=100.0, step=0.2)   # 100~~140
    blowoff = [base[-1] * (1.10 ** (k + 1)) for k in range(20)]  # 막판 급등
    hist = _bars(base + blowoff)
    res = ds.compute_signals(hist)
    fired = res["fired"]
    assert "overextended_ma200" in fired, fired
    assert "rsi_overbought" in fired, fired
    assert res["risk_score"] > 15, res


def test_drawdown_fires_after_peak_drop():
    # 상승 후 고점대비 큰 하락 → drawdown_from_high 발화
    up = _steady_uptrend(60, start=100.0, step=1.0)  # to 159
    peak = up[-1]
    down = [peak * (1 - 0.02 * (k + 1)) for k in range(15)]  # ~ -30%
    hist = _bars(up + down)
    res = ds.compute_signals(hist)
    assert "drawdown_from_high" in res["fired"], res["fired"]


def test_volume_divergence_fires():
    # 가격 상승 + 거래량 급감 → volume_divergence
    closes = _steady_uptrend(60, start=100.0, step=0.5)
    vols = [2000.0] * 50 + [400.0] * 10   # 최근 10일 거래량 급감
    hist = _bars(closes, vols=vols)
    res = ds.compute_signals(hist)
    assert "volume_divergence" in res["fired"], (res["fired"], res["signals"][-1])


def test_deadcross_proximity_fires():
    # 상승 후 횡보/하락으로 단기선이 장기선에 근접/역전 → deadcross_proximity
    up = _steady_uptrend(80, start=100.0, step=1.0)
    flat_down = [up[-1] - 1.0 * k for k in range(40)]  # 단기선 빠르게 내려옴
    hist = _bars(up + flat_down)
    res = ds.compute_signals(hist)
    assert "deadcross_proximity" in res["fired"], res["fired"]


def test_ma_trend_weakening_fires_on_rollover():
    up = _steady_uptrend(60, start=100.0, step=1.0)
    roll = [up[-1] - 0.8 * k for k in range(20)]  # 20일선 기울기 음전
    hist = _bars(up + roll)
    res = ds.compute_signals(hist)
    assert "ma_trend_weakening" in res["fired"], res["fired"]


# ============================================================
# 위험점수 단조성
# ============================================================
def test_risk_score_monotonic_with_drop_severity():
    up = _steady_uptrend(60, start=100.0, step=1.0)
    peak = up[-1]
    mild = _bars(up + [peak * (1 - 0.01 * (k + 1)) for k in range(10)])   # -10%
    severe = _bars(up + [peak * (1 - 0.025 * (k + 1)) for k in range(15)])  # ~-37%
    s_mild = ds.compute_signals(mild)["risk_score"]
    s_sev = ds.compute_signals(severe)["risk_score"]
    assert s_sev > s_mild, (s_mild, s_sev)


def test_rsi_helper_bounds():
    # 단조 상승 → RSI 100, 단조 하락 → RSI 0
    up = list(range(1, 60))
    down = list(range(60, 1, -1))
    assert ds.rsi([float(x) for x in up], 14) == 100.0
    assert ds.rsi([float(x) for x in down], 14) == 0.0


# ============================================================
# 데이터 부족 안전성
# ============================================================
def test_not_enough_data_raises():
    try:
        ds.compute_signals(_bars(_steady_uptrend(5)))
        assert False, "should raise NotEnoughData"
    except ds.NotEnoughData:
        pass


def test_empty_history_safe():
    try:
        ds.compute_signals([])
        assert False
    except ds.NotEnoughData:
        pass


def test_scan_skips_no_data_instrument():
    # 데이터 없는 종목은 skip(예외 아님) — 집합 스캔 계속
    out = scan_mod.scan([{"instrument_code": "EMPTY", "history": _bars(_steady_uptrend(5))}])
    assert out["ok"], out
    assert out["summary"]["analyzed"] == 0 and out["summary"]["skipped_no_data"] == 1, out["summary"]
    assert out["auto_order_created"] is False


# ============================================================
# 보수적 전환 제안 (읽기 전용, 주문 0)
# ============================================================
def test_conservative_proposal_on_high_risk_set():
    up = _steady_uptrend(60, start=100.0, step=1.0)
    peak = up[-1]
    crash = _bars(up + [peak * (1 - 0.025 * (k + 1)) for k in range(15)])
    insts = [{"instrument_code": f"RISK{i}", "sector": "반도체", "history": crash} for i in range(3)]
    out = scan_mod.scan(insts, account_index=99, current_cash_band={"min": 10.0, "max": 30.0})
    assert out["proposal"] is not None, out["summary"]
    prop = out["proposal"]
    assert prop["action"] == "shift_conservative"
    assert prop["auto_order_created"] is False
    # cash_band 상향 권고(읽기 전용)
    assert prop["suggested_cash_band"]["min"] > 10.0, prop["suggested_cash_band"]
    # confidence 별 판단 강도: 기술축만 가용(coverage 낮음)이면 신뢰도↓ → 단정 금지(후보로만).
    # 강한 조언(reduce_risk_assets=True)은 신뢰도가 충분(judgment.assert_ok)할 때만 나온다.
    cj = prop["confidence_judgment"]
    if cj["allowed_strength"] == "candidate_only":
        assert prop["reduce_risk_assets"] is False, prop  # 신뢰도 낮으면 단정 금지
        assert prop["strength"] == "candidate", prop
    else:
        assert prop["reduce_risk_assets"] is True, prop
    # 섹터 집계
    assert out["by_sector"] and out["by_sector"][0]["sector"] == "반도체"


def test_no_proposal_on_calm_set():
    calm = _bars(_steady_uptrend(260, step=0.2))
    insts = [{"instrument_code": f"CALM{i}", "history": calm} for i in range(3)]
    out = scan_mod.scan(insts, current_cash_band={"min": 10.0, "max": 30.0})
    assert out["proposal"] is None, out["summary"]


# ============================================================
# price_history 저장/seed
# ============================================================
def test_price_history_upsert_idempotent():
    bars = _bars(_steady_uptrend(30))
    rows = [{"trade_date": b["date"], **{k: b[k] for k in ("open", "high", "low", "close", "volume")}} for b in bars]
    r1 = ph.upsert_bars("TESTCODE", rows, source="test")
    r2 = ph.upsert_bars("TESTCODE", rows, source="test")  # 재실행 — 중복 없어야
    assert r1["written"] == 30 and r2["written"] == 30, (r1, r2)
    loaded = ph.load_history("TESTCODE")
    assert len(loaded) == 30, len(loaded)
    assert loaded[0]["date"] < loaded[-1]["date"]  # 오래된→최신


def test_seed_from_quotes_approx():
    conn = store_db.connect()
    try:
        for i in range(5):
            conn.execute("INSERT INTO quotes(ticker, price, source, captured_at) VALUES(?,?,?,?)",
                         ("SEEDQ", 100.0 + i, "test", f"2025-03-0{i+1} 09:00:00"))
        conn.commit()
    finally:
        conn.close()
    res = ph.seed_from_quotes("SEEDQ")
    assert res["ok"] and res["written"] == 5, res
    loaded = ph.load_history("SEEDQ")
    assert loaded[0]["close"] == 100.0 and loaded[0]["open"] == loaded[0]["close"], loaded[0]


def test_kis_fetcher_implemented_not_stub():
    # 일봉 fetcher 는 더이상 stub 아님(실구현). 키 없으면 NotImplementedError 가 아니라
    # 자격증명 오류(KisConfigError) 로 안전 실패해야 한다(가짜 성공 금지).
    import os
    from main_mission.portfolio_os.broker.kis_client import KisConfigError
    # 키 비우기(이 테스트 한정) — 실제 .env 키가 있어도 빈 account_index 키는 없음
    f = ph.KisDailyBarFetcher(account_index=49)  # 존재하지 않는 계좌 index
    raised = None
    try:
        f.fetch_daily("005930")
    except KisConfigError as e:
        raised = e
    except NotImplementedError:
        assert False, "stub 이면 안 됨 — 실구현이어야 함"
    assert raised is not None, "키 없으면 KisConfigError 로 안전 실패해야 함"


# ============================================================
# backtest + 노하우 누적
# ============================================================
def test_backtest_labels_declines():
    up = _steady_uptrend(60, start=100.0, step=1.0)
    peak = up[-1]
    down = [peak * (1 - 0.015 * (k + 1)) for k in range(20)]  # -30%
    recover = [down[-1] * (1 + 0.01 * (k + 1)) for k in range(20)]
    hist = _bars(up + down + recover)
    out = bt_mod.backtest("BTCODE", history=hist, decline_pct=10.0)
    assert out["ok"], out
    assert out["event_count"] >= 1, out
    assert out["decline_events"][0]["drawdown_pct"] < -10.0, out["decline_events"][0]


def test_backtest_preceding_signals_present():
    up = _steady_uptrend(80, start=100.0, step=1.2)
    peak = up[-1]
    down = [peak * (1 - 0.02 * (k + 1)) for k in range(20)]
    hist = _bars(up + down)
    out = bt_mod.backtest("BTCODE2", history=hist, decline_pct=10.0)
    assert out["ok"] and out["event_count"] >= 1, out
    # 사건 직전에 어떤 신호든 선행 기록
    assert "signal_lead_rate" in out


def test_accumulate_knowhow_writes_candidate():
    up = _steady_uptrend(80, start=100.0, step=1.2)
    peak = up[-1]
    down = [peak * (1 - 0.02 * (k + 1)) for k in range(20)]
    hist = _bars(up + down)
    before = lessons_mod.overview()["candidates"]
    res = bt_mod.accumulate_knowhow("KHCODE", history=hist)
    assert res["ok"], res
    # candidate 가 쌓였는지 (lesson_candidates instrument scope)
    conn = store_db.connect()
    try:
        row = conn.execute("SELECT scope, ref, source FROM lesson_candidates WHERE ref='KHCODE'").fetchone()
    finally:
        conn.close()
    assert row and row["scope"] == "instrument" and row["source"] == "decline_backtest", row


# ============================================================
# 자동주문 미생성 — 어떤 스캔 경로도 orders 테이블에 쓰지 않음
# ============================================================
def test_no_orders_created_anywhere():
    conn = store_db.connect()
    try:
        before = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    finally:
        conn.close()
    up = _steady_uptrend(60, start=100.0, step=1.0)
    peak = up[-1]
    crash = _bars(up + [peak * (1 - 0.025 * (k + 1)) for k in range(15)])
    scan_mod.scan([{"instrument_code": "X", "history": crash}], account_index=1,
                  current_cash_band={"min": 10.0, "max": 30.0})
    bt_mod.accumulate_knowhow("X", history=crash)
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
    print(f"ALL {len(fns)} DECLINE-SIGNAL TESTS PASSED")
