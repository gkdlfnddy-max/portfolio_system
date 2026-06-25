"""데이터 가용성 표준(SSOT) — Agent 2 개선 3.

모든 커넥터·분석기·후보평가·evidence 가 '데이터 없음'을 **동일하게** 처리하도록 단일 기준을 둔다.

금지(디렉티브):
  - 가짜 점수
  - placeholder 를 실데이터처럼 표시
  - 미연동 축/소스를 연결된 것처럼 표시
  - 데이터 부족인데 강한 추천

표준:
  - data_available=False → 점수/확신/카운트는 0 (절대 추정·가짜 채움 금지).
  - 강한 조언(strong advice)은 data_available=True 이고 confidence ≥ STRONG_ADVICE_MIN 일 때만.
    (실제 판정은 guards.strong_advice_allowed 가 단일 SSOT.)
"""
from __future__ import annotations

from typing import Any

from .candidate import CONFIDENCE_BANDS

# 강한 조언 허용 임계(= confidence mid). guards.strong_advice_allowed 와 동일 기준.
STRONG_ADVICE_MIN: float = CONFIDENCE_BANDS["mid"]


def honest_confidence(data_available: bool, confidence: Any) -> float:
    """data 없으면 0.0, 있으면 0~1 clamp(비숫자도 0.0). 가짜 확신 금지."""
    if not data_available:
        return 0.0
    try:
        return max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        return 0.0


def honest_count(data_available: bool, count: Any) -> int:
    """data 없으면 0, 있으면 음수 방지 정수. 가짜 카운트 금지."""
    if not data_available:
        return 0
    try:
        return max(0, int(count))
    except (TypeError, ValueError):
        return 0
