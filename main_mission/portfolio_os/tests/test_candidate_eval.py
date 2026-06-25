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
    CANDIDATE_FIELDS, CandidateEvaluation, candidate_evaluation)


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


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} CANDIDATE-EVAL TESTS PASSED")
