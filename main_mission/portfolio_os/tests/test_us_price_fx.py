"""미국(Yahoo) 가격 소스 + FX 환산 — 파서/라우팅/build_split_plan 통화 환산.

네트워크 미사용: Yahoo JSON 파서·is_krx_code·환율 환산을 순수 단위로 검증.
build_split_plan 의 USD 수량 환산(잘못된 폭증 방지)과 환율 미연동 스킵을 고정.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import price_history as ph
from main_mission.portfolio_os import exec_plan


def setup_function():
    store_db.init()


def test_is_krx_code():
    assert ph.is_krx_code("005930") is True      # 6자리 숫자 = KRX
    assert ph.is_krx_code("000660") is True
    assert ph.is_krx_code("SPY") is False         # 알파벳 = 해외
    assert ph.is_krx_code("BOTZ") is False
    assert ph.is_krx_code("12345") is False       # 5자리 → KRX 아님


def test_yahoo_parse_bars():
    # Yahoo chart JSON 형태(타임스탬프 + OHLCV). close 없는 행은 제외.
    t0 = int(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp())
    t1 = t0 + 86400
    data = {"chart": {"result": [{
        "timestamp": [t0, t1],
        "indicators": {"quote": [{
            "open": [700.0, None], "high": [710.0, 720.0], "low": [690.0, 700.0],
            "close": [705.0, None], "volume": [1000, 2000],
        }]},
    }]}}
    bars = ph._yahoo_parse_bars(data)
    assert len(bars) == 1                  # close=None 행 제외
    assert bars[0]["trade_date"] == "2026-01-05"
    assert bars[0]["close"] == 705.0


def test_yahoo_parse_empty():
    assert ph._yahoo_parse_bars({"chart": {"result": []}}) == []
    assert ph._yahoo_parse_bars({}) == []


_CONFIRMED = [
    {"kind": "cash", "ref": None, "weight_pct": 40.0},
    {"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": 40.7},
    {"kind": "tilt", "ref": "로봇", "weight_pct": 8.8},
    {"kind": "tilt", "ref": "반도체", "weight_pct": 8.8},
    {"kind": "hedge", "ref": "반도체 인버스", "weight_pct": 1.8},
]


def _seed(acc=1):
    conn = store_db.connect()
    try:
        conn.execute("INSERT OR IGNORE INTO accounts(account_index, alias, mode, broker) VALUES(?,?,?,?)",
                     (acc, "fx-test", "mock", "kis"))
        conn.execute("INSERT INTO allocation_selections(account_index, variant, allocation, status, "
                     "selected_by, selected_at) VALUES(?,?,?,?,?,?)",
                     (acc, "conservative", json.dumps(_CONFIRMED), "active", "user",
                      datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()


def test_build_split_plan_usd_fx_conversion():
    _seed()
    # USD ETF: 현실적 달러 가격. KRW 예산을 환율로 환산해 수량 계산(폭증 방지).
    picks = {"robotics": ["BOTZ"]}            # robotics 8.8%
    prices = {"BOTZ": 40.0}                    # $40
    markets = {"BOTZ": ("US", "USD")}
    cash = 9_900_000                            # ₩
    fx = {"USD": 1500.0}                        # 1달러=1500원
    plan = exec_plan.build_split_plan(1, picks, prices=prices, cash_krw=cash,
                                      rounds=1, markets=markets, fx_rates=fx)
    assert plan["ok"]
    steps = plan["steps"]
    assert len(steps) == 1
    s = steps[0]
    assert s["currency"] == "USD" and s["fx_rate"] == 1500.0
    # 회차 예산 = min(8.8%, one_order_cap 5%)*9.9M = 495,000원 = $330 / limit($40*0.98=$39.2) = 8주.
    #   (환산 없으면 495,000 / 39.2 = 12,627주로 폭증 — FX 환산이 이를 막는다.)
    assert s["qty"] == 8
    assert abs(s["limit_price"] - 39.2) < 0.01   # USD 소수 호가


def test_build_split_plan_skips_usd_when_fx_missing():
    _seed()
    # fx_rates 가 제공됐는데 USD 환율이 없으면 → 정직하게 스킵(잘못된 수량 방지).
    plan = exec_plan.build_split_plan(1, {"robotics": ["BOTZ"]}, prices={"BOTZ": 40.0},
                                      cash_krw=9_900_000, rounds=1,
                                      markets={"BOTZ": ("US", "USD")}, fx_rates={})
    assert plan["ok"] and plan["step_count"] == 0
    assert any("환율 미연동" in sk.get("reason", "") for sk in plan["skipped"])


def test_fx_rates_for_markets():
    # KRW only → None(환산 불필요). USD 있으면 환율 조회(monkeypatch 로 네트워크 회피).
    assert exec_plan.fx_rates_for_markets({"005930": ("KRX", "KRW")}) is None
    orig = ph.fetch_fx_usdkrw
    try:
        ph.fetch_fx_usdkrw = lambda: 1400.0
        r = exec_plan.fx_rates_for_markets({"SPY": ("US", "USD")})
        assert r == {"USD": 1400.0}
        ph.fetch_fx_usdkrw = lambda: None      # 조회 실패 → 빈 dict(스킵 유도)
        assert exec_plan.fx_rates_for_markets({"SPY": ("US", "USD")}) == {}
    finally:
        ph.fetch_fx_usdkrw = orig
