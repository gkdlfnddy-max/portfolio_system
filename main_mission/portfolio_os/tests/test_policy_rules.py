"""Dynamic Policy — default/hard rule 분리 + hard rule override 불가 검증."""
from __future__ import annotations

from main_mission.portfolio_os import policy_rules as pr


def test_soft_override_applies():
    out = pr.apply_overrides(user_overrides={"cash_min_pct": 35.0, "single_name_max_pct": 40.0})
    assert out["effective"]["cash_min_pct"] == 35.0
    assert out["effective"]["single_name_max_pct"] == 40.0  # 개별주 집중형도 가능(자유도)


def test_hard_rule_override_ignored():
    out = pr.apply_overrides(user_overrides={"no_market_buy": False, "human_approval_required": False})
    assert "no_market_buy" in out["ignored_overrides"]
    assert "human_approval_required" in out["ignored_overrides"]
    # effective 에 hard rule 변경이 새지 않음
    assert "no_market_buy" not in out["effective"]
    assert set(out["hard_rules"]) >= {"no_market_buy", "live_order_blocked_by_default", "selected_allocation_required"}


def test_hard_rule_cannot_be_disabled():
    # login_and_rbac_required = pin_required_for_accounts 폐기 후 대체 hard rule.
    out = pr.apply_overrides(disabled_rules=["no_market_buy", "login_and_rbac_required", "sector_max_pct"])
    assert "no_market_buy" in out["blocked_disables"]
    assert "login_and_rbac_required" in out["blocked_disables"]
    assert "sector_max_pct" in out["soft_disabled"]  # soft 규칙은 끌 수 있음


def test_template_applies_without_touching_hard_rules():
    out = pr.apply_overrides(template="cash_defensive")
    assert out["template_applied"] == "cash_defensive"
    assert out["effective"]["cash_min_pct"] == 30.0       # 방어형 템플릿
    assert out["effective"]["allow_themes"] is False
    assert out["effective"]["use_individual_stocks"] is True  # default 유지
    # 템플릿이 hard rule 을 건드리지 않음
    assert pr.is_hard_rule("no_market_buy") and "no_market_buy" not in out["effective"]


def test_all_templates_exist():
    for t in ("single_stock_focus", "etf_diversified", "cash_defensive", "growth_theme", "dividend_income", "custom"):
        assert t in pr.TEMPLATES, t


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} POLICY-RULES TESTS PASSED")
