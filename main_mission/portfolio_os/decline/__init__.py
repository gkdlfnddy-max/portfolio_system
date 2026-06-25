"""하락 징후 — 6축 종합 + 메타인지 + 성장 학습 층.

기존 `decline_signals.py`(기술축 결정론 규칙)를 1개 축으로 재사용하고, 나머지 5축
(분산·거시·이벤트·심리·정책)을 **데이터 있으면 계산, 없으면 정직하게 data_available=False**
로 표기하는 공통 인터페이스 위에 얹는다. `composite.py` 가 가용 축만 메타인지 가중합하고,
가중치 = 데이터 가용성 × 과거 예측 적중 신뢰도(track record, growth/lessons 누적)로 성장한다.

원칙(불변):
  - Anthropic API 미사용 — 지능 = 규칙 신호 + Claude+메모리 성장.
  - 자동주문 0 — 읽기 전용 제안만.
  - 데이터 없는 축은 가짜 점수 금지(data_available=False, confidence 낮춤).
"""
