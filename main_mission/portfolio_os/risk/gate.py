"""Risk gate (T7) — 주문 전 hard-block.

순수 함수: 거래 리스트 + 현재 비중 + 한도 → pass/fail + 위반 목록.
risk-chief 만 이 게이트를 통과/차단할 권한을 가진다(roles.md 권한 매트릭스).
세부: docs/portfolio/safety_rules.md
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class RiskLimits:
    """risk_limits 테이블(SSOT)에서 로드. 기본값 = safety_rules.md B."""
    cash_min_pct: Decimal = Decimal(10)
    single_name_max_pct: Decimal = Decimal(20)
    short_total_max_pct: Decimal = Decimal(10)
    leverage_total_max_pct: Decimal = Decimal(15)
    single_order_max_pct: Decimal = Decimal(5)
    max_orders_per_session: int = 20

    @classmethod
    def from_effective(cls, effective: dict | None) -> "RiskLimits":
        """계좌별 실효 정책(policy_rules.effective_policy)의 limits 로 주문시점 게이트 한도 구성.

        effective 는 effective_policy() 전체 dict 또는 그 안의 limits dict 둘 다 허용.
        값이 없는 한도는 기본값 유지. hard rule(시장가 금지/승인 등)은 여기서 다루지 않는다.
        """
        base = cls()
        if not effective:
            return base
        lim = effective.get("limits") if isinstance(effective.get("limits"), dict) else effective

        def _d(key, fallback):
            v = lim.get(key)
            return Decimal(str(v)) if v is not None else fallback

        return cls(
            cash_min_pct=_d("cash_min_pct", base.cash_min_pct),
            single_name_max_pct=_d("single_name_max_pct", base.single_name_max_pct),
            short_total_max_pct=_d("inverse_max_pct", base.short_total_max_pct),
            leverage_total_max_pct=_d("leverage_max_pct", base.leverage_total_max_pct),
            single_order_max_pct=_d("one_order_cap_pct", base.single_order_max_pct),
            max_orders_per_session=base.max_orders_per_session,
        )


@dataclass(frozen=True)
class Violation:
    limit: str
    observed: Decimal
    threshold: Decimal
    detail: str


@dataclass
class RiskResult:
    passed: bool
    violations: list[Violation] = field(default_factory=list)


@dataclass(frozen=True)
class PostTradeWeights:
    """거래 적용 후 예상 비중(%) — portfolio-chief 가 계산해 전달."""
    cash_pct: Decimal
    single_name_max_pct: Decimal      # 가장 큰 단일 종목 비중
    short_total_pct: Decimal
    leverage_total_pct: Decimal
    largest_order_pct: Decimal        # 총자산 대비 최대 1주문
    order_count: int


def check_trades(weights: PostTradeWeights, limits: RiskLimits) -> RiskResult:
    """모든 hard 한도를 적용. 하나라도 위반이면 passed=False.

    fail 이면 호출측은 주문 후보를 생성하지 않고 CEO 에 사유를 제시한다(safety_rules T7).
    """
    v: list[Violation] = []

    if weights.cash_pct < limits.cash_min_pct:
        v.append(Violation("cash_min_pct", weights.cash_pct, limits.cash_min_pct,
                           "현금이 최소 비중 아래로 떨어지는 매수"))
    if weights.single_name_max_pct > limits.single_name_max_pct:
        v.append(Violation("single_name_max_pct", weights.single_name_max_pct,
                           limits.single_name_max_pct, "단일 종목 비중 초과"))
    if weights.short_total_pct > limits.short_total_max_pct:
        v.append(Violation("short_total_max_pct", weights.short_total_pct,
                           limits.short_total_max_pct, "인버스/숏 총합 초과"))
    if weights.leverage_total_pct > limits.leverage_total_max_pct:
        v.append(Violation("leverage_total_max_pct", weights.leverage_total_pct,
                           limits.leverage_total_max_pct, "레버리지 총합 초과"))
    if weights.largest_order_pct > limits.single_order_max_pct:
        v.append(Violation("single_order_max_pct", weights.largest_order_pct,
                           limits.single_order_max_pct, "1주문 규모 초과(슬리피지)"))
    if weights.order_count > limits.max_orders_per_session:
        v.append(Violation("max_orders_per_session", Decimal(weights.order_count),
                           Decimal(limits.max_orders_per_session), "세션 주문 수 초과"))

    return RiskResult(passed=not v, violations=v)
