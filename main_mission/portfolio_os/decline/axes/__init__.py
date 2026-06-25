"""6축 scorer 패키지 — 각 축은 공통 인터페이스 `score(context) -> AxisResult`.

축 목록(AXES):
  technical    기술   — decline_signals 래핑(이미 구현·재사용)
  distribution 분산   — 거래량 급증 + 개인 순매수 / 외국인·기관 순매도 (KR 투자자별 매매동향)
  macro        거시   — 과열·금리·신용 팽창·인플레·환율 (ECOS/FRED)
  event        이벤트 — FOMC·금통위·CPI·고용 발표 캘린더 → 발표 전후 변동성/관망
  sentiment    심리   — VIX·풋콜비율·신용잔고 → 공포 조정
  policy       정책   — 정부 정책 불리·규제 발표 → 섹터 조정 (뉴스/DART)

정직: technical 외 5축은 **실데이터 있으면 계산, 없으면 data_available=False**.
가짜 점수 금지(거짓 경보 방지). 데이터 ingestion 지점은 각 모듈 docstring + schema 테이블.
"""
from __future__ import annotations

from .base import AxisResult, axis_result, clamp
from . import technical, distribution, macro, event, sentiment, policy

# 등록된 축 scorer (이름 → score 함수). composite 가 순회.
AXES: dict[str, object] = {
    "technical": technical.score,
    "distribution": distribution.score,
    "macro": macro.score,
    "event": event.score,
    "sentiment": sentiment.score,
    "policy": policy.score,
}

# 사람이 보는 축 라벨(한글)
AXIS_LABELS: dict[str, str] = {
    "technical": "기술",
    "distribution": "분산",
    "macro": "거시",
    "event": "이벤트",
    "sentiment": "심리",
    "policy": "정책/규제",
}

__all__ = ["AxisResult", "axis_result", "clamp", "AXES", "AXIS_LABELS"]
