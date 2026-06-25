"""Agent 1 개선 2 — HARD_RULES / guards 중앙화 테스트.

검증:
  - 디렉티브 8개 규칙이 모두 중앙 HARD_RULES 에 존재(DIRECTIVE_RULES 매핑)
  - 신규 키(no_auto_order, no_placeholder_as_real) 존재 + 끌 수 없음(hard)
  - live_locked: KIS_LIVE_CONFIRM 단일 predicate (factory 와 동일 규칙)
  - strong_advice_allowed: data 없으면/비숫자/임계 미만 → False
"""
from __future__ import annotations

from main_mission.portfolio_os import guards, policy_rules
from main_mission.portfolio_os.candidate import CONFIDENCE_BANDS


def test_all_eight_directive_rules_present_centrally():
    for directive_name, canonical in guards.DIRECTIVE_RULES.items():
        assert canonical in policy_rules.HARD_RULES, (directive_name, canonical)
    assert len(guards.DIRECTIVE_RULES) == 8


def test_new_hard_rule_keys_present():
    assert "no_auto_order" in policy_rules.HARD_RULES
    assert "no_placeholder_as_real" in policy_rules.HARD_RULES


def test_hard_rules_cannot_be_disabled():
    res = policy_rules.apply_overrides(disabled_rules=["no_auto_order", "no_placeholder_as_real"])
    assert "no_auto_order" in res["blocked_disables"]
    assert "no_placeholder_as_real" in res["blocked_disables"]


def test_hard_rule_override_ignored():
    res = policy_rules.apply_overrides(user_overrides={"no_auto_order": "off"})
    assert "no_auto_order" in res["ignored_overrides"]


def test_live_locked_predicate():
    assert guards.live_locked("") is True            # 미설정 → 차단
    assert guards.live_locked("nope") is True
    assert guards.live_locked(" I_UNDERSTAND ") is False  # 정확값(공백 trim) → 해제


def test_strong_advice_requires_data_and_confidence():
    mid = CONFIDENCE_BANDS["mid"]
    assert guards.strong_advice_allowed(False, 0.9) is False     # 데이터 없음
    assert guards.strong_advice_allowed(True, mid - 0.01) is False  # 임계 미만
    assert guards.strong_advice_allowed(True, mid) is True
    assert guards.strong_advice_allowed(True, "nan") is False    # 비숫자
    assert guards.strong_advice_allowed(True, None) is False


def test_guards_reexports_same_hard_rules():
    assert guards.HARD_RULES == policy_rules.HARD_RULES


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} GUARDS-CENTRAL TESTS PASSED")
