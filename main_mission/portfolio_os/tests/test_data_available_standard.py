"""데이터 가용성 표준(Agent 2 개선 3) — **교차 스키마 회귀 테스트**.

모든 SSOT 스키마가 '데이터 없음'을 동일하게 처리함을 한곳에서 못박는다(드리프트 방지·CLAUDE.md §11.5).
대상: AxisResult · CandidateEvaluation · ConnectorResult · EvidenceRecord · guards.strong_advice_allowed.
금지: 가짜 점수/카운트/비중 · 미연동인데 강한 추천.
"""
from __future__ import annotations

from main_mission.portfolio_os import guards
from main_mission.portfolio_os.data_availability import (
    STRONG_ADVICE_MIN, honest_confidence, honest_count)
from main_mission.portfolio_os.connectors import connector_result
from main_mission.portfolio_os.candidate import candidate_evaluation
from main_mission.portfolio_os.evidence_record import EvidenceRecord
from main_mission.portfolio_os.decline.axes.base import axis_result


def test_helper_zeroes_when_unavailable():
    assert honest_confidence(False, 0.9) == 0.0
    assert honest_confidence(True, "x") == 0.0
    assert honest_confidence(True, 0.7) == 0.7
    assert honest_count(False, 50) == 0
    assert honest_count(True, -3) == 0
    assert honest_count(True, 12) == 12


def test_axis_result_unavailable_is_zero():
    r = axis_result("macro", data_available=False, risk_0_100=80, confidence=0.9)
    assert r["data_available"] is False
    assert r["risk_0_100"] == 0.0 and r["confidence"] == 0.0


def test_connector_result_unavailable_is_zero():
    r = connector_result("dart", data_available=False, confidence=0.9, count=99)
    assert r["confidence"] == 0.0 and r["count"] == 0


def test_candidate_no_fake_weight_or_confidence():
    c = candidate_evaluation("etf", "X")          # 미정
    assert c["suggested_weight"] is None and c["max_weight"] is None
    assert c["confidence"] == 0.0
    assert c["recommendation_strength"]["level"] == "watch"


def test_evidence_record_confidence_clamped():
    assert EvidenceRecord(confidence=9)["confidence"] == 1.0
    assert EvidenceRecord()["confidence"] == 0.0


def test_strong_advice_gate_uniform():
    # 미연동 → 강한 조언 불가
    assert guards.strong_advice_allowed(False, 0.99) is False
    # 임계 미만 → 불가
    assert guards.strong_advice_allowed(True, STRONG_ADVICE_MIN - 0.01) is False
    # 데이터+임계 이상 → 허용
    assert guards.strong_advice_allowed(True, STRONG_ADVICE_MIN) is True


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} DATA-AVAILABLE-STANDARD TESTS PASSED")
