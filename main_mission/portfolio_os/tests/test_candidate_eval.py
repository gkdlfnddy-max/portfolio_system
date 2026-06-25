"""CandidateEvaluation SSOT 스키마 테스트.

검증:
  - 표준 17필드 존재 + dict/json/attr 호환
  - 안전 불변식 하드: approval_required=True · auto_order_created=False · auto_applied=False (우회 불가)
  - data_available=false 표준: 미정 비중 None(가짜 숫자 금지) · confidence 0
  - confidence clamp
"""
from __future__ import annotations

import json

from main_mission.portfolio_os.candidate import (
    CANDIDATE_FIELDS, CONFIDENCE_BANDS, CandidateEvaluation,
    candidate_evaluation, recommendation_strength)


def test_all_standard_fields_present():
    c = candidate_evaluation("etf", "069500")
    for k in CANDIDATE_FIELDS:
        assert k in c, k
    assert set(c.keys()) == set(CANDIDATE_FIELDS)


def test_dict_json_attr_compatible():
    c = candidate_evaluation("treasury", "148070", display_name="KOSEF 국고채10년",
                             confidence=0.7, suggested_weight=0.1, max_weight=0.2)
    assert isinstance(c, dict)
    assert c["candidate_id"] == "148070"          # dict 접근
    assert c.confidence == 0.7                      # attr 접근
    assert json.loads(json.dumps(c, ensure_ascii=False))["display_name"] == "KOSEF 국고채10년"
    assert dict(c)["suggested_weight"] == 0.1       # dict(c)


def test_safety_invariants_are_hard():
    # 호출자가 자동주문/자동적용/무승인을 주입하려 해도 막힌다(생성자가 받지 않음).
    c = candidate_evaluation("stock", "005930")
    assert c["approval_required"] is True
    assert c["auto_order_created"] is False
    assert c["auto_applied"] is False
    # kwargs 로 우회 시도 → TypeError(받지 않는 인자)
    import pytest
    with pytest.raises(TypeError):
        candidate_evaluation("stock", "005930", auto_order_created=True)
    with pytest.raises(TypeError):
        candidate_evaluation("stock", "005930", approval_required=False)


def test_no_fake_numbers_when_unknown():
    c = candidate_evaluation("inverse", "252670")
    assert c["suggested_weight"] is None          # 미정 → None(가짜 숫자 금지)
    assert c["max_weight"] is None
    assert c["confidence"] == 0.0
    assert c["data_quality"]["available"] is False
    assert c["display_name"] == "252670"          # 이름 없으면 id


def test_confidence_clamped():
    assert candidate_evaluation("etf", "X", confidence=5.0)["confidence"] == 1.0
    assert candidate_evaluation("etf", "X", confidence=-1.0)["confidence"] == 0.0


# --------------------------------------------------------------------------- 개선 3: 공통 추천 강도
def test_recommendation_strength_thresholds():
    assert recommendation_strength(0.29)["level"] == "watch"
    assert recommendation_strength(CONFIDENCE_BANDS["low"])["level"] == "weak"   # 0.3
    assert recommendation_strength(0.59)["level"] == "weak"
    assert recommendation_strength(CONFIDENCE_BANDS["mid"])["level"] == "moderate"  # 0.6
    assert recommendation_strength(None)["level"] == "watch"      # 미상 → 보수적
    assert recommendation_strength("nope")["level"] == "watch"    # 비숫자 → 보수적
    # 어느 강도든 항상 사용자 승인 필요
    for c in (0.1, 0.4, 0.9):
        assert recommendation_strength(c)["approval_required"] is True


def test_candidate_carries_recommendation_strength():
    c = candidate_evaluation("etf", "069500", confidence=0.7)
    assert c["recommendation_strength"]["level"] == "moderate"
    weak = candidate_evaluation("stock", "005930", confidence=0.4)
    assert weak["recommendation_strength"]["level"] == "weak"
    watch = candidate_evaluation("inverse", "252670")  # confidence 0 → watch
    assert watch["recommendation_strength"]["level"] == "watch"
    assert watch["recommendation_strength"]["approval_required"] is True


def test_bands_are_ceo_rule():
    assert CONFIDENCE_BANDS == {"low": 0.3, "mid": 0.6}


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} CANDIDATE-EVAL TESTS PASSED")
