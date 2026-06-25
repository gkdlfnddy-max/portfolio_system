"""중앙 SSOT 모듈(guards, markers, store.constants) 단위 회귀 테스트.

Phase 2 keystone — 흩어진 stale/approval-flag/STALE_HOURS 정의를 한 곳으로 모은 뒤,
그 동작이 기존과 동일함을 고정한다. (구조 개선·중복 제거, 동작 무변경)
DB 불필요 — 순수 함수. Anthropic API 미사용.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_guards_markers.sqlite3")

from main_mission.portfolio_os import guards, markers
from main_mission.portfolio_os.store.constants import STALE_HOURS


# ── STALE_HOURS SSOT: 모든 소비자가 같은 값을 본다 ──
def test_stale_hours_is_single_source():
    from main_mission.portfolio_os import decision, selection
    from main_mission.portfolio_os.growth import prehooks
    assert STALE_HOURS == 24.0
    assert decision.STALE_HOURS is STALE_HOURS
    assert selection.STALE_HOURS is STALE_HOURS
    # prehooks 는 직접 정의를 제거하고 guards/constants 로 위임 — 직접 상수는 더 이상 없다.
    assert not hasattr(prehooks, "STALE_HOURS") or prehooks.STALE_HOURS == 24.0


# ── markers.PROPOSAL_FLAGS: 자동적용 금지/승인 필요 의미 고정 ──
def test_proposal_flags_values():
    assert markers.PROPOSAL_FLAGS == {"auto_applied": False, "requires_user_approval": True}


def test_mark_proposal_sets_flags_in_place():
    d = {"ok": True}
    out = markers.mark_proposal(d)
    assert out is d
    assert d["auto_applied"] is False and d["requires_user_approval"] is True


# ── guards.snapshot_stale: 기존 prehook 판정과 동일 ──
def test_snapshot_stale_none_is_stale():
    assert guards.snapshot_stale(None) is True


def test_snapshot_stale_flag_wins():
    fresh_ts = datetime.now(timezone.utc).isoformat()
    assert guards.snapshot_stale({"is_stale": True, "captured_at": fresh_ts}) is True


def test_snapshot_fresh_is_not_stale():
    fresh_ts = datetime.now(timezone.utc).isoformat()
    assert guards.snapshot_stale({"captured_at": fresh_ts}) is False


def test_snapshot_old_is_stale():
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS + 1)).isoformat()
    assert guards.snapshot_stale({"captured_at": old_ts}) is True


def test_snapshot_bad_ts_is_stale():
    assert guards.snapshot_stale({"captured_at": "not-a-date"}) is True


def test_snapshot_naive_ts_treated_as_utc():
    # 'YYYY-MM-DD HH:MM:SS' (tz 없음) 도 UTC 로 처리.
    fresh_naive = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    assert guards.snapshot_stale({"captured_at": fresh_naive}) is False


# ── guards.snapshot_stale 가 prehooks 위임과 일치(델리게이션 증명) ──
def test_prehook_delegates_to_guards():
    from main_mission.portfolio_os.growth import prehooks
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS + 5)).isoformat()
    snap = {"captured_at": old_ts}
    assert prehooks._snapshot_stale(snap) == guards.snapshot_stale(snap) is True


# ── guards.has_selected_allocation / account_bound_ok ──
def test_has_selected_allocation():
    assert guards.has_selected_allocation(None) is False
    assert guards.has_selected_allocation({}) is False
    assert guards.has_selected_allocation({"id": None}) is False
    assert guards.has_selected_allocation({"id": 7}) is True


def test_account_bound_ok():
    assert guards.account_bound_ok(None, account_required=False) is True
    assert guards.account_bound_ok(None, account_required=True) is False
    assert guards.account_bound_ok(3, account_required=True) is True


# ── guards.HARD_RULES 재노출: policy_rules 와 동일 ──
def test_hard_rules_reexport_matches_policy_rules():
    from main_mission.portfolio_os import policy_rules
    assert guards.HARD_RULES == policy_rules.HARD_RULES
    assert "no_market_buy" in guards.HARD_RULES
    assert "human_approval_required" in guards.HARD_RULES


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
