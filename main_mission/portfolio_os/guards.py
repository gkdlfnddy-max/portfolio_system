"""중앙 guard SSOT — 흩어진 안전 점검 로직의 단일 정의 (CLAUDE.md §11.2, safety_rules).

CEO 목표(구조 개선): 같은 안전 규칙이 모듈마다 따로 구현되면 리팩토링 때 한쪽만 바뀌어
규칙이 *조용히* 약해진다. 순수 함수로 한 곳에 모으고 각 모듈이 호출한다.

현재 수용 범위(동작 무변경, 점진 통합):
  - snapshot_stale(snap)        : 스냅샷 staleness 판정 (prehook 의 기존 구현을 이리로 이동)
  - has_selected_allocation(sel): selected allocation 존재 여부 (확정안 = SSOT, [[confirmed-allocation-truth]])
  - account_bound_ok(...)       : 계좌 귀속 task 의 account_id 필수 여부 (계좌 격리)
  - HARD_RULES                  : policy_rules.HARD_RULES 재노출(불변 규칙 단일 참조점)

값/판정은 기존과 동일 — 정의 위치만 통합.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from .candidate import CONFIDENCE_BANDS
from .store.constants import STALE_HOURS

# 불변 규칙(hard rule)의 단일 참조점 — 정의는 policy_rules 가 SSOT, 여기선 재노출만.
try:  # 순환 import 방어 (policy_rules 는 의존 없음이라 보통 안전).
    from .policy_rules import HARD_RULES  # noqa: F401
except Exception:  # pragma: no cover
    HARD_RULES = {}

# CEO 디렉티브(Agent 1 개선 2)가 요구한 8개 안전 규칙 → 중앙 HARD_RULES 의 canonical key.
# 이름만 다를 뿐 동일 규칙. 이 매핑으로 "8개 규칙이 모두 중앙에 존재"를 보장/검증한다.
DIRECTIVE_RULES: dict[str, str] = {
    "no_auto_order": "no_auto_order",
    "no_auto_policy_change": "no_auto_policy_change",
    "approval_required": "human_approval_required",
    "account_memory_isolation": "account_memory_isolation",
    "data_available_required_for_strong_advice": "evidence_for_strong_advice",
    "no_fake_data": "no_fake_data",
    "no_placeholder_as_real": "no_placeholder_as_real",
    "live_hard_lock": "live_order_blocked_by_default",
}


def live_locked(confirm: str | None = None) -> bool:
    """live 주문 하드락 — KIS_LIVE_CONFIRM=I_UNDERSTAND 가 아니면 True(차단).

    live_hard_lock 규칙의 **단일 predicate**. broker/factory 가 이 함수로 차단을 강제한다(안전 §6/§15).
    """
    val = confirm if confirm is not None else os.getenv("KIS_LIVE_CONFIRM", "")
    return val.strip() != "I_UNDERSTAND"


def strong_advice_allowed(data_available: bool, confidence) -> bool:
    """강한 조언 허용 여부 — data_available 필수 + confidence ≥ mid 임계.

    data_available_required_for_strong_advice 규칙의 단일 판정.
    데이터 미연동이거나 confidence 가 임계 미만/비숫자면 False(관망·약한 제안만).
    """
    if not data_available:
        return False
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return False
    return c >= CONFIDENCE_BANDS["mid"]


def parse_ts(s):
    """스냅샷 timestamp 파서 — ISO('T' 포함) 또는 'YYYY-MM-DD HH:MM:SS'. 실패 시 None."""
    if not s:
        return None
    try:
        if "T" in s:
            return datetime.fromisoformat(s)
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def snapshot_stale(snap, *, now: datetime | None = None) -> bool:
    """스냅샷이 stale 인가? (안전 §11: 낡은 잔고로 주문/decision 금지)

    True(=stale) 조건: snap 없음 · is_stale 플래그 · captured_at 파싱 실패 · STALE_HOURS 초과.
    prehook 의 기존 _snapshot_stale 와 동일 판정(SSOT 로 이동).
    """
    if not snap:
        return True
    if snap.get("is_stale"):
        return True
    ts = parse_ts(snap.get("captured_at"))
    if ts is None:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    return (ref - ts) > timedelta(hours=STALE_HOURS)


def has_selected_allocation(sel) -> bool:
    """확정안(selected allocation)이 존재하는가? (확정안 없으면 주문/decision 금지)"""
    return bool(sel) and (sel.get("id") is not None)


def account_bound_ok(account_index: int | None, account_required: bool) -> bool:
    """계좌 귀속 task 인데 account_id 가 있는가? (계좌 격리 — account 작업엔 account_id 필수)"""
    if not account_required:
        return True
    return account_index is not None
