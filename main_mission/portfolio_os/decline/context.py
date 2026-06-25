"""축 컨텍스트 빌더 — DB(축별 테이블)에서 각 축이 읽을 데이터를 모아 context dict 구성.

composite/axes 는 순수(부수효과 없음)이므로, DB 접근은 여기서만 한다.
데이터가 없는 축은 키를 비워둔다(빈 리스트/None) → 축이 정직하게 data_available=False.

읽기 전용. 자동주문 0. 비밀 없음.
"""
from __future__ import annotations

from datetime import date

from .. import price_history as ph
from ..store import db as store_db

# 거시/심리 지표: indicator key → context dict key (composite axes 가 기대하는 이름)
_MACRO_LATEST_KEYS = ["policy_rate", "yield_10y", "yield_2y", "cpi_yoy",
                      "credit_growth_yoy", "fx_usdkrw", "fx_usdkrw_change_1m",
                      "policy_rate_change_3m"]
_SENTIMENT_KEYS = ["vix", "vkospi", "put_call_ratio", "margin_balance_change_1m",
                   "trading_value_change"]


def _latest_indicators(conn, table: str, keys: list[str], *,
                       drop_stale: bool = True) -> dict:
    """지표 테이블에서 각 indicator 최신값 1개씩 → {indicator: value}. 없으면 빈 dict.

    **freshness/stale 처리(정직)**: obs_date 가 stale(임계 경과)이면 그 지표는 제외한다
    (오래된 거시 지표를 최신처럼 쓰면 가짜 신호 — CLAUDE.md §11.8). macro_connect.freshness 사용.
    """
    from .. import macro_connect as mc
    out: dict[str, float] = {}
    for k in keys:
        row = conn.execute(
            f"SELECT value, obs_date FROM {table} WHERE indicator=? ORDER BY obs_date DESC LIMIT 1",
            (k,)).fetchone()
        if row is None:
            continue
        if drop_stale:
            fr = mc.freshness(row["obs_date"], indicator=k)
            if fr["stale"]:
                continue  # stale 지표 제외 — 거시축이 신선한 지표로만 정직하게 계산
        out[k] = float(row["value"])
    return out


def load_investor_flows(conn, instrument_code: str, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        "SELECT trade_date, foreign_net, institution_net, retail_net, volume "
        "FROM investor_flows WHERE instrument_code=? ORDER BY trade_date DESC LIMIT ?",
        (instrument_code, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def load_policy_events(conn, sector: str | None, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT event_date, sector, stance, severity, title, source FROM policy_events "
        "ORDER BY event_date DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def load_market_events(conn, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT event_date, name, impact, region, source FROM market_events "
        "ORDER BY event_date DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def build_context(instrument_code: str, *, sector: str | None = None,
                  history: list[dict] | None = None, as_of_date: str | None = None,
                  conn=None) -> dict:
    """한 종목에 대한 6축 context. history 직접 주면 DB 가격조회 생략(테스트/순수).

    축별 데이터가 DB 에 없으면 해당 키는 비어 그 축은 data_available=False(정직).
    """
    as_of = as_of_date or date.today().isoformat()
    own = conn is None
    conn = conn or store_db.connect()
    try:
        hist = history if history is not None else ph.load_history(instrument_code)
        flows = load_investor_flows(conn, instrument_code)
        macro = _latest_indicators(conn, "macro_indicators", _MACRO_LATEST_KEYS)
        sentiment = _latest_indicators(conn, "sentiment_index", _SENTIMENT_KEYS)
        market_events = load_market_events(conn)
        policy_events = load_policy_events(conn, sector)
    finally:
        if own:
            conn.close()

    return {
        "instrument_code": instrument_code,
        "sector": sector,
        "as_of_date": as_of,
        "history": hist,
        "investor_flows": flows or None,
        "macro_indicators": macro or None,
        "sentiment_index": sentiment or None,
        "market_events": market_events or None,
        "policy_events": policy_events or None,
    }
