"""이벤트축(event) — 주요 지표·금리 발표 캘린더 → 발표 전후 변동성/관망.

FOMC·한국 금통위·CPI·고용지표(NFP) 등 **고영향 발표 임박** 시 변동성↑·관망 권고.
하락 자체 예측이 아니라 "이벤트 리스크 구간"을 알려 노출을 보수적으로(관망) 유도.

데이터: 경제 캘린더 (market_events 테이블).
  context["market_events"] = [{event_date, name, impact("high|medium|low"), ...}, ...]
  context["as_of_date"]   = "YYYY-MM-DD" (오늘 — 며칠 남았는지 계산. 없으면 미연동 취급)
  ⚠️ **ingestion 지점**: 경제 캘린더 적재(미연동 — market_events 테이블).
     이벤트 없거나 as_of 없으면 data_available=False.

신호:
  imminent_high_impact_event — high impact 발표가 N일 이내 → 발표 전 변동성/관망
"""
from __future__ import annotations

from datetime import date

from .base import axis_result, clamp, sig

AXIS = "event"

THRESHOLDS = {
    "imminent_days": 3,        # 발표까지 3일 이내 → 임박
    "max_days_window": 7,      # 7일 밖이면 영향 미미
}


def _parse_date(s) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def score(context: dict) -> dict:
    events = context.get("market_events")
    as_of = _parse_date(context.get("as_of_date"))
    if not events or not isinstance(events, list) or as_of is None:
        return axis_result(AXIS, data_available=False,
                           detail="경제 캘린더 미연동 — event 데이터 없음 (as_of/events)")

    t = THRESHOLDS
    high_events = []
    for e in events:
        ed = _parse_date(e.get("event_date"))
        if ed is None:
            continue
        days = (ed - as_of).days
        if 0 <= days <= t["max_days_window"]:
            high_events.append({**e, "days_until": days})

    if not high_events:
        # 데이터는 있으나 임박 이벤트 없음 — data_available=True, 위험 0 (정직)
        return axis_result(AXIS, risk_0_100=0.0, signals=[],
                           data_available=True, confidence=0.6,
                           detail="가까운 고영향 발표 없음")

    # 가장 임박한 high impact 발표
    high = [e for e in high_events if str(e.get("impact", "")).lower() == "high"]
    target = min(high or high_events, key=lambda e: e["days_until"])
    days = target["days_until"]
    is_high = str(target.get("impact", "")).lower() == "high"

    fired = days <= t["imminent_days"] and is_high
    # severity: 발표 당일(0일) 만점 → imminent_days 에서 0
    sev = clamp(1.0 - days / float(t["imminent_days"])) if fired else 0.0
    signals = [sig(
        "imminent_high_impact_event", fired, days, sev,
        f"{target.get('name','발표')} D-{days}" + (" (고영향)" if is_high else ""))]

    risk = sev * 100.0 if fired else 0.0
    conf = 0.7  # 캘린더는 사실 기반이라 비교적 높음
    detail = (f"이벤트축 위험 {risk:.0f}: {target.get('name','발표')} D-{days}"
              + (" 발표 전 변동성/관망 권고" if fired else " (영향 제한적)"))
    return axis_result(AXIS, risk_0_100=risk, signals=signals,
                       data_available=True, confidence=conf, detail=detail)
