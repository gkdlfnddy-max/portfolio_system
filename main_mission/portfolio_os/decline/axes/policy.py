"""정책/규제축(policy) — 정부 정책 불리·규제 발표 → 섹터 조정.

특정 섹터에 불리한 정책/규제(예: 부동산 대출 규제, 플랫폼 규제, 반도체 수출통제)는
해당 섹터 조정을 유발. 뉴스/DART 공시에서 부정적 정책 이벤트를 집계.

데이터: policy_events (시장 이벤트와 별도 — 규제/정책 전용).
  context["policy_events"] = [{event_date, sector, stance("adverse|favorable|neutral"),
                              severity(0~1), title, source}, ...]
  context["as_of_date"] = "YYYY-MM-DD" (최근성 가중)
  context["sector"]     = (선택) 종목 섹터 — 일치 섹터 정책만 강하게 반영
  ⚠️ **ingestion 지점**: 뉴스/DART → 정책 이벤트 분류(미연동 — policy_events 테이블).
     이벤트 없으면 data_available=False.

정직: 정책 'stance/severity' 분류는 사람/Claude+메모리 판단 결과를 저장하는 것이며,
      Anthropic API 자동분류가 아니다(규칙 + 메모리 성장).

신호:
  adverse_policy — 최근 불리한 정책/규제 이벤트(섹터 일치 시 가중)
"""
from __future__ import annotations

from datetime import date

from .base import axis_result, clamp, sig

AXIS = "policy"

THRESHOLDS = {
    "recent_days": 21,        # 최근 3주 이내 정책 이벤트만 영향
    "max_days": 60,
}


def _parse_date(s) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def score(context: dict) -> dict:
    events = context.get("policy_events")
    as_of = _parse_date(context.get("as_of_date"))
    if not events or not isinstance(events, list):
        return axis_result(AXIS, data_available=False,
                           detail="정책/규제 이벤트 미연동 — policy 데이터 없음 (뉴스/DART)")

    t = THRESHOLDS
    sector = context.get("sector")
    adverse = []
    for e in events:
        if str(e.get("stance", "")).lower() != "adverse":
            continue
        ed = _parse_date(e.get("event_date"))
        # 날짜 없으면 최근으로 간주(보수적 — recency=1)
        recency = 1.0
        if ed is not None and as_of is not None:
            days = (as_of - ed).days
            if days < 0 or days > t["max_days"]:
                continue
            recency = clamp(1.0 - days / float(t["max_days"]))
        sev0 = float(e.get("severity", 0.5) or 0.5)
        # 섹터 일치 시 가중 1.0, 불일치(또는 미지정)면 0.5 (시장 전반 영향만)
        sector_match = 1.0
        if sector and e.get("sector") and str(e["sector"]) != str(sector):
            sector_match = 0.5
        adverse.append({"event": e, "weighted_sev": clamp(sev0 * recency * sector_match)})

    if not adverse:
        return axis_result(AXIS, risk_0_100=0.0, signals=[],
                           data_available=True, confidence=0.5,
                           detail="최근 불리한 정책/규제 이벤트 없음")

    top = max(adverse, key=lambda a: a["weighted_sev"])
    sev = top["weighted_sev"]
    fired = sev > 0.0
    ev = top["event"]
    signals = [sig("adverse_policy", fired, round(sev, 2), sev,
                   f"{ev.get('title','정책/규제')} ({ev.get('sector','전반')})"
                   + (f" — {ev.get('source')}" if ev.get("source") else ""))]
    risk = sev * 100.0
    conf = 0.45 + 0.1 * min(3, len(adverse)) / 3.0  # 이벤트 분류는 주관 — 보수적
    detail = f"정책축 위험 {risk:.0f}: 불리 이벤트 {len(adverse)}건, 최고 {ev.get('title','')}"
    return axis_result(AXIS, risk_0_100=risk, signals=signals,
                       data_available=True, confidence=conf, detail=detail)
