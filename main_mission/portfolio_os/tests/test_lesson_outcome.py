"""lesson_outcome 테스트 — 분석 이후 시장반응 자동 기록 + reliability 갱신.

핵심 검증(CEO 지시):
  1. lookahead 차단: 결과 평가는 **분석일(created_at) 이후** 일봉만 사용. 분석일 당일/이전 일봉을
     섞어도 결과가 바뀌지 않음(못박기).
  2. 방어/축소 제안인데 실제 하락 → hit → reliability ↑.
  3. 방어 제안인데 실제 상승 → false_alarm → reliability ↓.
  4. 미래 일봉 부족 → pending 유지(가짜 성장 금지).
  5. 자동주문/policy 변경 0.
  6. Anthropic API import 0.
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_lesson_outcome.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import lesson_runs
from main_mission.portfolio_os import lesson_outcome as lo


def setup():
    store_db.init()


def _reset():
    conn = store_db.connect()
    conn.execute("DELETE FROM lesson_runs")
    conn.execute("DELETE FROM price_history")
    conn.commit()
    conn.close()


def _insert_lesson(scope_key, suggested_action, created_at, scope_type="stock"):
    conn = store_db.connect()
    cur = conn.execute(
        "INSERT INTO lesson_runs(scope_type, scope_key, suggested_action, "
        "hit_or_miss, created_at) VALUES(?,?,?,?,?)",
        (scope_type, scope_key, suggested_action, "pending", created_at),
    )
    conn.commit()
    lid = cur.lastrowid
    conn.close()
    return lid


def _bars(start_close, deltas, start_date="2026-06-23"):
    """[{date, open, high, low, close}] 일봉 생성. deltas: 일별 종가 변화율(%) 리스트."""
    from datetime import date, timedelta
    d = date.fromisoformat(start_date)
    out = []
    c = start_close
    for i, delta in enumerate(deltas):
        c = c * (1 + delta / 100.0)
        out.append({"date": (d + timedelta(days=i)).isoformat(),
                    "open": c, "high": c * 1.01, "low": c * 0.99, "close": c})
    return out


def _store_bars(code, bars):
    import main_mission.portfolio_os.price_history as ph
    ph.upsert_bars(code, [{"trade_date": b["date"], "open": b["open"], "high": b["high"],
                           "low": b["low"], "close": b["close"], "volume": 1} for b in bars],
                   source="test")


# ------------------------------------------------------------------
# lookahead 차단 — 분석일 이후 일봉만 사용
# ------------------------------------------------------------------
def test_future_bars_excludes_analysis_date_and_before():
    """분석일(2026-06-22) 당일/이전 일봉은 future_bars 에서 제외돼야 한다."""
    hist = [
        {"date": "2026-06-20", "close": 100.0, "high": 101, "low": 99},  # 이전
        {"date": "2026-06-22", "close": 200.0, "high": 201, "low": 199},  # 분석일 당일
        {"date": "2026-06-23", "close": 105.0, "high": 106, "low": 104},  # 이후 ✓
        {"date": "2026-06-24", "close": 110.0, "high": 111, "low": 109},  # 이후 ✓
    ]
    fut = lo.future_bars("X", "2026-06-22", history=hist)
    dates = [b["date"] for b in fut]
    assert dates == ["2026-06-23", "2026-06-24"], dates
    # 분석일 당일(200) 이 섞이지 않았으므로 baseline 은 105 여야 함
    assert fut[0]["close"] == 105.0


def test_lookahead_pre_analysis_bars_do_not_change_result():
    """분석일 이전 일봉을 잔뜩 넣어도 결과가 동일해야 한다(lookahead 차단 못박기)."""
    _reset()
    analysis = "2026-06-22T09:00:00+00:00"
    lid = _insert_lesson("005930", "shift_conservative", analysis)
    # 이후: 5거래일 하락 -> 방어 hit 기대
    future = _bars(70000, [-2, -2, -1, -1, -2], start_date="2026-06-23")
    _store_bars("005930", future)
    r1 = lo.evaluate_lesson(lid, windows=[5])
    assert r1["status"] == "evaluated"
    ret_clean = r1["per_window"]["5d"]["return_pct"]

    # 이제 분석일 이전에 극단적 일봉을 추가하고 다시(새 lesson) 평가 — 동일해야 함
    _reset()
    lid2 = _insert_lesson("005930", "shift_conservative", analysis)
    pre = _bars(999999, [50, 50, 50], start_date="2026-06-01")  # 분석일 이전 노이즈
    _store_bars("005930", pre)
    _store_bars("005930", future)
    r2 = lo.evaluate_lesson(lid2, windows=[5])
    assert r2["status"] == "evaluated"
    assert r2["per_window"]["5d"]["return_pct"] == ret_clean


# ------------------------------------------------------------------
# hit / false_alarm 판정 + reliability 갱신
# ------------------------------------------------------------------
def test_defensive_hit_raises_reliability():
    """방어 제안 + 실제 하락 → hit → reliability 상승."""
    _reset()
    code = "TESTHIT"
    before = lesson_runs.reliability("stock", code)["reliability"]
    lid = _insert_lesson(code, "shift_conservative", "2026-06-22T00:00:00+00:00")
    _store_bars(code, _bars(50000, [-3, -2, -2, -1, -2]))  # 명확한 하락
    res = lo.evaluate_lesson(lid, windows=[5])
    assert res["status"] == "evaluated"
    assert res["hit_or_miss"] == "hit", res
    after = lesson_runs.reliability("stock", code)["reliability"]
    assert after > before, (before, after)


def test_defensive_false_alarm_lowers_reliability():
    """방어 제안인데 실제 상승 → false_alarm → reliability 하락."""
    _reset()
    code = "TESTFALSE"
    before = lesson_runs.reliability("stock", code)["reliability"]
    lid = _insert_lesson(code, "shift_conservative", "2026-06-22T00:00:00+00:00")
    _store_bars(code, _bars(50000, [2, 2, 2, 2, 2]))  # 상승 → 방어가 틀림
    res = lo.evaluate_lesson(lid, windows=[5])
    assert res["status"] == "evaluated"
    assert res["hit_or_miss"] == "false_alarm", res
    after = lesson_runs.reliability("stock", code)["reliability"]
    assert after < before, (before, after)


# ------------------------------------------------------------------
# 미래 일봉 부족 → pending 유지
# ------------------------------------------------------------------
def test_insufficient_future_bars_stays_pending():
    """미래 일봉이 window 보다 적으면 pending 유지(가짜 성장 금지)."""
    _reset()
    code = "TESTPEND"
    lid = _insert_lesson(code, "shift_conservative", "2026-06-22T00:00:00+00:00")
    _store_bars(code, _bars(50000, [-1, -1]))  # 2일치만 → 5d 평가 불가
    res = lo.evaluate_lesson(lid, windows=[5, 20, 60])
    assert res["status"] == "pending", res
    conn = store_db.connect()
    row = conn.execute("SELECT hit_or_miss FROM lesson_runs WHERE id=?", (lid,)).fetchone()
    conn.close()
    assert row["hit_or_miss"] == "pending"


def test_no_future_bars_stays_pending():
    """분석일 이후 일봉이 전무하면 pending."""
    _reset()
    code = "TESTNONE"
    lid = _insert_lesson(code, "shift_conservative", "2026-06-22T00:00:00+00:00")
    # 분석일 이전 일봉만 적재
    _store_bars(code, _bars(50000, [-1, -1, -1], start_date="2026-06-01"))
    res = lo.evaluate_lesson(lid, windows=[5])
    assert res["status"] == "pending"
    assert res["reason"] == "no_future_bars"


# ------------------------------------------------------------------
# evaluate_pending — 배치 + 자동주문/policy 0
# ------------------------------------------------------------------
def test_evaluate_pending_batch_records_market_reaction():
    """배치 평가: actual_outcome 에 return_5d/20d/60d·max_drawdown JSON 기록, auto_orders=0."""
    _reset()
    code = "BATCH1"
    lid = _insert_lesson(code, "shift_conservative", "2026-06-22T00:00:00+00:00")
    _store_bars(code, _bars(50000, [-1] * 60))  # 60일 하락
    out = lo.evaluate_pending(window_days=[5, 20, 60], scope_key=code)
    assert out["ok"] is True
    assert out["evaluated"] == 1
    assert out["auto_orders"] == 0
    assert out["policy_changes"] == 0

    conn = store_db.connect()
    row = conn.execute("SELECT actual_outcome, hit_or_miss, market_reaction_window "
                       "FROM lesson_runs WHERE id=?", (lid,)).fetchone()
    conn.close()
    import json
    actual = json.loads(row["actual_outcome"])
    assert "return_5d" in actual and "return_20d" in actual and "return_60d" in actual
    assert "max_drawdown" in actual
    assert actual["analysis_date"] == "2026-06-22"
    assert row["market_reaction_window"] == 60  # 가장 긴 확정 window
    assert row["hit_or_miss"] == "hit"


def test_evaluate_pending_skips_non_price_scope():
    """account/agent/task scope 는 시장 일봉으로 채점 불가 → 평가 후보에서 제외."""
    _reset()
    _insert_lesson("acct1", "shift_conservative", "2026-06-22T00:00:00+00:00",
                   scope_type="account")
    out = lo.evaluate_pending(window_days=[5])
    assert out["candidates"] == 0  # price scope 만 후보


def test_reliability_before_after_recorded():
    """reliability_before/after 가 lesson_run 에 기록된다."""
    _reset()
    code = "RELREC"
    lid = _insert_lesson(code, "shift_conservative", "2026-06-22T00:00:00+00:00")
    _store_bars(code, _bars(50000, [-2] * 5))
    lo.evaluate_lesson(lid, windows=[5])
    conn = store_db.connect()
    row = conn.execute("SELECT reliability_before, reliability_after FROM lesson_runs WHERE id=?",
                       (lid,)).fetchone()
    conn.close()
    assert row["reliability_before"] is not None
    assert row["reliability_after"] is not None
    assert row["reliability_after"] > row["reliability_before"]  # hit 이므로 상승


def test_drawdown_uses_lows_after_analysis_only():
    """max_drawdown 은 분석일 이후 구간의 low 만으로 계산(분석일 이전 low 무시)."""
    _reset()
    code = "DDONLY"
    lid = _insert_lesson(code, "shift_conservative", "2026-06-22T00:00:00+00:00")
    # 분석일 이전 폭락(무시돼야 함) + 이후 완만 하락
    _store_bars(code, _bars(1.0, [-90, -90], start_date="2026-06-10"))
    _store_bars(code, _bars(50000, [-2, -1, -1, -1, -1]))
    res = lo.evaluate_lesson(lid, windows=[5])
    assert res["status"] == "evaluated"
    dd = res["per_window"]["5d"]["drawdown_pct"]
    # 이후 구간 baseline 대비 낙폭은 ~ -10% 미만 수준, -90% 같은 값이면 안 됨
    assert dd > -20.0, dd


# ------------------------------------------------------------------
# Anthropic API 미사용
# ------------------------------------------------------------------
def test_no_anthropic_import():
    """Anthropic SDK/키 의존 0 — 코멘트의 정책 문구는 허용, 실제 import/사용만 차단."""
    import pathlib
    src = pathlib.Path(lo.__file__).read_text(encoding="utf-8")
    assert "import anthropic" not in src
    assert "from anthropic" not in src
    assert "ANTHROPIC_API_KEY" not in src
