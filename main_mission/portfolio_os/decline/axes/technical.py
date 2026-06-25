"""기술축(technical) — 기존 decline_signals 래핑(재사용, 읽기만).

이미 구현된 `decline_signals.compute_signals` (이격·RSI·변동성·MA둔화·데드크로스·낙폭·
거래량 다이버전스)를 6축 공통 인터페이스로 감싼다. **본문은 건드리지 않는다.**

데이터: context["history"] = 가격이력 [{date, close, high, low, volume}] (오래된→최신).
  history 없거나 부족(NotEnoughData)이면 data_available=False (가짜 점수 금지).

confidence: 데이터 포인트 수가 충분할수록↑ (60일 미만이면 보수적으로 낮춤).
"""
from __future__ import annotations

from ... import decline_signals as ds
from .base import axis_result

AXIS = "technical"

# 데이터 포인트 수 → confidence: MIN_DATA_POINTS 에서 0.5, FULL 이상에서 1.0
_FULL_POINTS = 200.0


def _confidence_from_points(n: int) -> float:
    if n < ds.MIN_DATA_POINTS:
        return 0.0
    # 20개=0.5, 200개+=1.0 사이 선형
    span = _FULL_POINTS - ds.MIN_DATA_POINTS
    return 0.5 + 0.5 * min(1.0, (n - ds.MIN_DATA_POINTS) / span)


def score(context: dict) -> dict:
    history = context.get("history")
    if not history:
        return axis_result(AXIS, data_available=False,
                           detail="가격이력 없음 — 기술축 미연동")
    try:
        res = ds.compute_signals(history)
    except ds.NotEnoughData as e:
        return axis_result(AXIS, data_available=False,
                           detail=f"가격이력 부족({e}) — 기술축 계산 불가")

    n = res["data_points"]
    conf = _confidence_from_points(n)
    fired = res["fired"]
    detail = (f"기술 위험점수 {res['risk_score']:.0f} ({res['risk_level']}), "
              f"발화 {len(fired)}개" + (f": {', '.join(fired[:3])}" if fired else ""))
    return axis_result(
        AXIS, risk_0_100=res["risk_score"], signals=res["signals"],
        data_available=True, confidence=conf, detail=detail,
    )
