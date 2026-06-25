"""운영 상수 SSOT — 여러 모듈에 흩어진 임계값/매직넘버를 한 곳에서 관리.

CEO 원칙(구조 개선·중복 제거): 같은 의미의 임계값이 모듈마다 따로 정의되면
리팩토링 때 한쪽만 바뀌어 *조용히* 어긋난다. 여기서 단일 정의하고 각 모듈은 import 한다.
값 자체는 기존과 동일 — 정의 위치만 통합(동작 무변경).
"""
from __future__ import annotations

# 스냅샷(잔고) 및 스냅샷 기반 decision 의 staleness 임계(시간).
# 이보다 오래된 잔고로 주문/decision 금지 (안전 §11: stale 로 주문 금지).
# 이전: decision.py / selection.py / growth/prehooks.py 가 각자 24.0 을 정의 → SSOT 로 통합.
STALE_HOURS = 24.0
