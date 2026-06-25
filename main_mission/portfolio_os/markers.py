"""제안(draft) 표준 플래그 SSOT — "자동적용 금지 / 사람 승인 필요".

CEO 불변원칙(CLAUDE.md §2-5,6, safety_rules): 시스템 산출물은 *제안*일 뿐이며
사용자 승인 전에는 어떤 policy/목표비중/주문도 자동 반영되지 않는다.

이 플래그 쌍이 모듈마다 inline 리터럴로 흩어져 있었다(decline_policy_draft/bond_recommendation/
weight_allocator/portfolio_impact/daily_review/perspective_variants 등). 표준값을 여기서 단일
정의하고 각 모듈은 `**markers.PROPOSAL_FLAGS` 로 펼쳐 쓴다 → 의미가 한 곳에서만 바뀐다(동작 무변경).
"""
from __future__ import annotations

# 제안 산출물 표준 플래그. 절대 True/False 가 뒤집혀선 안 되는 안전 의미를 담는다.
PROPOSAL_FLAGS: dict[str, bool] = {
    "auto_applied": False,          # 자동 적용 절대 금지
    "requires_user_approval": True,  # 사람 승인 필요
}


def mark_proposal(obj: dict) -> dict:
    """dict 에 표준 제안 플래그를 세팅하고 그 dict 를 반환(in-place).

    기존 동작과 동일: requires_user_approval=True, auto_applied=False 를 보장.
    """
    obj.update(PROPOSAL_FLAGS)
    return obj
