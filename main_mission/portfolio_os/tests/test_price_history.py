"""price_history KIS 일봉 fetcher 테스트 (네트워크/실키 없이 결정론).

검증:
  - KIS output2 응답 파싱(stck_bsop_date/oprc/hgpr/lwpr/clpr/acml_vol → OHLCV, 거래량 포함)
  - 날짜 윈도우 페이징(100건 상한 가정 — 여러 페이지 누적, 무한루프 없음)
  - 멱등 upsert(source='kis_daily') + load_history 와 호환(decline 입력 형태)
  - 빈/비거래일 행 skip(가짜 데이터 미생성)
  - rt_cd != 0 → RuntimeError(가짜 성공 금지)
  - read-only: place_order 등 주문 경로 미호출(주문 호출 0 증거)
  - 키 없을 때 안전 실패(KisConfigError, NotImplementedError 아님)
  - 적재된 실(=합성 일봉) 로 decline_backtest 실행 가능(노하우 candidate 누적)
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_pricehist.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import price_history as ph
from main_mission.portfolio_os import decline_backtest as bt_mod
from main_mission.portfolio_os.broker import kis_endpoints as ep


def setup():
    store_db.init()


# ------------------------------------------------------------------
# Fake adapter — KIS 응답을 흉내(네트워크/키 없음). 주문 경로는 정의조차 안 함.
# ------------------------------------------------------------------
class _FakeKisAdapter:
    """get_daily_bars 만 흉내. place_order 를 일부러 두지 않아 read-only 강제.

    KIS 100건 상한을 흉내: 윈도우(start~end)에 들어오는 일봉만, 최대 100건 반환.
    """

    def __init__(self, all_bars: list[dict]):
        # all_bars: [{trade_date 'YYYY-MM-DD', open, high, low, close, volume}] 오래된→최신
        self.all_bars = all_bars
        self.calls: list[tuple[str, str]] = []

    def get_daily_bars(self, ticker, *, start, end, adjusted=True):
        self.calls.append((start, end))
        s = f"{start[0:4]}-{start[4:6]}-{start[6:8]}"
        e = f"{end[0:4]}-{end[4:6]}-{end[6:8]}"
        win = [b for b in self.all_bars if s <= b["trade_date"] <= e]
        win.sort(key=lambda b: b["trade_date"])
        # KIS 는 최신부터 100건 — 윈도우 안에서 최신 100건만
        return win[-ep.DAILY_CHART_MAX_PER_CALL:]


def _synth_bars(n=260):
    """합성 일봉 — 상승 후 큰 하락(백테스트가 사건을 잡도록).

    fetcher 는 now() 기준 과거로 페이징하므로, 데이터는 '오늘'에서 n일 전부터
    오늘까지로 둔다(현실적: 최근 구간이어야 윈도우가 닿는다).
    """
    from datetime import date, timedelta
    d0 = date.today() - timedelta(days=n - 1)
    closes = [100.0 + 0.8 * i for i in range(180)]          # 상승
    peak = closes[-1]
    closes += [peak * (1 - 0.02 * (k + 1)) for k in range(40)]  # -~55% 하락
    closes += [closes[-1] * (1 + 0.01 * (k + 1)) for k in range(n - len(closes))]
    out = []
    for i, c in enumerate(closes):
        out.append({
            "trade_date": (d0 + timedelta(days=i)).isoformat(),
            "open": round(c, 2), "high": round(c * 1.01, 2),
            "low": round(c * 0.99, 2), "close": round(c, 2),
            "volume": 1000.0 + i,
        })
    return out


# ============================================================
# 파싱 / OHLCV+거래량
# ============================================================
def test_adapter_parses_kis_output2():
    from main_mission.portfolio_os.broker.kis_adapter import KisPaperAdapter

    class _FakeClient:
        is_healthy = True
        def get(self, path, tr_id, params, timeout=10):
            assert path == ep.PATH_DOMESTIC_DAILY_CHART
            assert tr_id == ep.TRID_DOMESTIC_DAILY_CHART
            assert params["FID_PERIOD_DIV_CODE"] == "D"
            assert params["FID_COND_MRKT_DIV_CODE"] == "J"
            return {"rt_cd": "0", "output2": [
                {"stck_bsop_date": "20240105", "stck_oprc": "100", "stck_hgpr": "110",
                 "stck_lwpr": "95", "stck_clpr": "105", "acml_vol": "12345"},
                {"stck_bsop_date": "20240104", "stck_oprc": "98", "stck_hgpr": "101",
                 "stck_lwpr": "97", "stck_clpr": "99", "acml_vol": "9000"},
                {"stck_bsop_date": "", "stck_clpr": "0", "acml_vol": "0"},  # 빈 행 skip
            ]}

    adapter = KisPaperAdapter(client=_FakeClient())
    bars = adapter.get_daily_bars("005930", start="20240101", end="20240110")
    assert len(bars) == 2, bars  # 빈 행 제거
    assert bars[0]["trade_date"] == "2024-01-04" and bars[1]["trade_date"] == "2024-01-05"
    assert bars[1]["close"] == 105.0 and bars[1]["volume"] == 12345.0  # 거래량 적재
    assert bars[1]["high"] == 110.0 and bars[1]["low"] == 95.0


def test_adapter_raises_on_error_rtcd():
    from main_mission.portfolio_os.broker.kis_adapter import KisPaperAdapter

    class _ErrClient:
        is_healthy = True
        def get(self, *a, **k):
            return {"rt_cd": "1", "msg1": "잘못된 종목", "output2": []}

    adapter = KisPaperAdapter(client=_ErrClient())
    raised = False
    try:
        adapter.get_daily_bars("XXXX", start="20240101", end="20240110")
    except RuntimeError:
        raised = True
    assert raised, "rt_cd!=0 이면 RuntimeError(가짜 성공 금지)"


# ============================================================
# 페이징 + upsert
# ============================================================
def test_fetcher_pages_and_stores():
    all_bars = _synth_bars(260)
    fake = _FakeKisAdapter(all_bars)
    fetcher = ph.KisDailyBarFetcher(account_index=1, adapter=fake)
    res = fetcher.fetch_and_store("PAGECODE", count=200)
    assert res["ok"] and res["written"] >= 180, res
    assert res["source"] == "kis_daily"
    # 100건 상한이라 1페이지로는 부족 → 여러 페이지 호출됐는지
    assert len(fake.calls) >= 2, fake.calls
    # load_history 와 호환(오래된→최신, 거래량 포함)
    loaded = ph.load_history("PAGECODE", limit=400)
    assert len(loaded) >= 180
    assert loaded[0]["date"] < loaded[-1]["date"]
    assert loaded[-1]["volume"] is not None


def test_fetcher_idempotent():
    all_bars = _synth_bars(120)
    fake = _FakeKisAdapter(all_bars)
    f = ph.KisDailyBarFetcher(account_index=1, adapter=fake)
    r1 = f.fetch_and_store("IDEMP", count=120)
    r2 = ph.KisDailyBarFetcher(account_index=1, adapter=_FakeKisAdapter(all_bars)).fetch_and_store("IDEMP", count=120)
    n1 = len(ph.load_history("IDEMP", limit=400))
    n2 = len(ph.load_history("IDEMP", limit=400))
    assert n1 == n2, (n1, n2)  # 재실행해도 행 수 동일(멱등)


def test_fetcher_empty_response_no_fake_success():
    fake = _FakeKisAdapter([])  # KIS 빈 응답
    res = ph.KisDailyBarFetcher(account_index=1, adapter=fake).fetch_and_store("EMPTYX", count=200)
    assert res["ok"] is False and res["reason"] == "no_bars_returned", res
    assert ph.load_history("EMPTYX") == []  # 가짜 데이터 미생성


# ============================================================
# read-only 증거 — 주문 경로 미호출
# ============================================================
def test_fetcher_is_read_only_no_order_method_used():
    # FakeKisAdapter 에는 place_order 가 아예 없다 → fetcher 가 그것을 부르면 AttributeError.
    fake = _FakeKisAdapter(_synth_bars(120))
    fetcher = ph.KisDailyBarFetcher(account_index=1, adapter=fake)
    fetcher.fetch_and_store("ROCODE", count=100)  # 예외 없이 완료 = 주문 경로 미호출
    assert not hasattr(fake, "place_order")


def test_no_orders_table_writes():
    conn = store_db.connect()
    try:
        before = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    finally:
        conn.close()
    fake = _FakeKisAdapter(_synth_bars(120))
    ph.KisDailyBarFetcher(account_index=1, adapter=fake).fetch_and_store("NOORDERS", count=100)
    conn = store_db.connect()
    try:
        after = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    finally:
        conn.close()
    assert after == before, (before, after)


# ============================================================
# 키 없을 때 안전 실패
# ============================================================
def test_no_keys_safe_failure():
    from main_mission.portfolio_os.broker.kis_client import KisConfigError
    f = ph.KisDailyBarFetcher(account_index=48)  # 없는 계좌 index → 키 없음
    raised = None
    try:
        f.fetch_daily("005930")
    except KisConfigError as e:
        raised = e
    except NotImplementedError:
        assert False, "stub 이면 안 됨"
    assert raised is not None


# ============================================================
# 적재된 일봉으로 실 백테스트(노하우 누적)
# ============================================================
def test_backtest_on_fetched_bars():
    fake = _FakeKisAdapter(_synth_bars(260))
    ph.KisDailyBarFetcher(account_index=1, adapter=fake).fetch_and_store("BTLIVE", count=240)
    bt = bt_mod.backtest("BTLIVE", decline_pct=10.0)  # DB(load_history)에서 읽음
    assert bt["ok"], bt
    assert bt["event_count"] >= 1, bt  # 큰 하락 사건 라벨링
    kh = bt_mod.accumulate_knowhow("BTLIVE", bt)
    assert kh["ok"], kh
    conn = store_db.connect()
    try:
        row = conn.execute(
            "SELECT source FROM lesson_candidates WHERE ref='BTLIVE'").fetchone()
    finally:
        conn.close()
    assert row and row["source"] == "decline_backtest", row


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} PRICE-HISTORY TESTS PASSED")
