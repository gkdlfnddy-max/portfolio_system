"""account_memory — 계좌별 메모리/정책/판단맥락 **통합 조회**.

CEO 지시(계좌 최우선): 같은 사용자라도 계좌마다 목적이 다르다(계좌1 성장형 / 계좌2 방어형 /
자녀 교육자금 …). 따라서 한 자산/시장을 판단할 때 **그 계좌의 목적·확정 배분(selected_allocation)·
risk rule·과거 결정·lesson·견해(피드백)** 이 *공통 자산 지식보다 먼저* 와야 한다.

이 모듈은 흩어진 계좌별 소스를 한 번에 모은다(읽기 전용):
  - objective : investor_objective.get / criteria_for_account  (계좌 목적·"최선" 기준)
  - policy    : policy.compile_policy + policy_rules.effective_policy (계좌 한도/스타일/hard rule)
  - allocation: selection.current  (사람이 확정한 truth — 최우선 맥락)
  - views     : user_views.list_views (계좌 견해 = 사용자 피드백/1급 입력)
  - lessons   : lesson_runs (그 계좌 account_index 의 과거 판단·반응)
  - memory    : asset_memory (그 계좌 사용자 관점 — 격리)

설계 제약(불변):
- **계좌 격리.** account_context(N) 은 account_index=N 의 것만 모은다. 타 계좌 누수 0.
- **자동 적용 0.** 조회·요약만. 주문/policy/배분을 *바꾸지 않는다*.
- **지능 = Claude + 메모리 (Anthropic API 미사용 — import 없음).**
- **공통 자산 지식이 계좌 정책을 덮어쓰지 않는다.** 우선순위는 계좌 정책/목적이 위.

  python -m main_mission.portfolio_os.account_memory --account 1
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .store import db as store_db
from . import investor_objective as objective
from . import policy as policy_mod
from . import policy_rules
from . import selection
from . import user_views
from . import lesson_runs as lr


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _acct(account_index) -> int:
    n = int(account_index)
    if n < 1:
        raise ValueError("account_index 는 1 이상이어야 합니다(계좌 격리)")
    return n


# ============================================================
# 개별 조회 헬퍼 (모두 read-only · 계좌 격리)
# ============================================================
def account_objective(account_index: int) -> dict:
    """계좌 목적 + "최선" 기준(criteria). 미설정이면 정직하게 알림(가정 금지)."""
    acct = _acct(account_index)
    obj = objective.get(acct)
    crit = objective.criteria_for_account(acct)
    return {
        "is_set": bool(obj and obj.get("investment_goal")),
        "objective": obj,
        "criteria": crit,
    }


def account_policy(account_index: int) -> dict:
    """계좌 실효 정책(한도/스타일/hard rule) + compile_policy(현금밴드/채권/지역).

    effective_policy 가 우선 — 계좌 override·template·profile 반영. compile_policy 는
    현금밴드/채권/지역 등 의사결정 입력으로 함께 노출. hard rule 은 변경 불가로 표시.
    """
    acct = _acct(account_index)
    try:
        eff = policy_rules.effective_policy(acct)
    except Exception:  # noqa: BLE001 — 정책 조회 실패는 graceful(미설정 계좌 등)
        eff = None
    try:
        compiled = policy_mod.compile_policy(acct)
    except Exception:  # noqa: BLE001
        compiled = None
    return {
        "effective": eff,
        "compiled": compiled,
        "hard_rules": list(policy_rules.HARD_RULES),
    }


def account_allocation(account_index: int) -> dict | None:
    """사람이 확정한 selected allocation(truth). 없으면 None."""
    acct = _acct(account_index)
    cur = selection.current(acct)
    if not cur:
        return None
    out = dict(cur)
    # allocation 은 JSON TEXT 로 저장 — 파싱해서 제공(원본도 유지).
    try:
        out["allocation_rows"] = json.loads(cur["allocation"]) if cur.get("allocation") else []
    except (ValueError, TypeError):
        out["allocation_rows"] = []
    return out


def account_views(account_index: int) -> list[dict]:
    """계좌 견해(active) = 사용자 피드백/1급 입력. 계좌 격리."""
    acct = _acct(account_index)
    return user_views.list_views(acct, status="active")


def account_lessons(account_index: int, *, limit: int = 30) -> list[dict]:
    """그 계좌 account_index 의 과거 lesson_run(판단·반응). 계좌 격리.

    주의: reliability 자체는 scope(자산/시장) 공통 노하우라 계좌 무관이지만, *이 계좌가
    내렸던 판단 이력* 은 account_index 로 격리해 본다(다른 계좌 결정은 보이지 않음).
    """
    acct = _acct(account_index)
    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT id, scope_type, scope_key, signal_summary, suggested_action, "
            "user_action, hit_or_miss, reliability_after, created_at "
            "FROM lesson_runs WHERE account_index=? "
            "ORDER BY datetime(created_at) DESC LIMIT ?",
            (acct, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ============================================================
# account_context — 통합 (계좌 최우선 맥락)
# ============================================================
def account_context(account_index: int) -> dict:
    """한 계좌의 판단 맥락을 통합 조회 — **계좌 정책/목적이 공통 지식보다 먼저**.

    반환 순서가 곧 우선순위(자동 적용 아님 · 계좌 격리):
      ① allocation  (확정 배분 = truth)
      ② objective   (계좌 목적 + "최선" 기준)
      ③ policy      (계좌 한도/스타일 + hard rule)
      ④ views       (계좌 견해 = 사용자 피드백)
      ⑤ lessons     (이 계좌의 과거 판단·반응)
    """
    acct = _acct(account_index)
    allocation = account_allocation(acct)
    obj = account_objective(acct)
    pol = account_policy(acct)
    views = account_views(acct)
    lessons = account_lessons(acct)

    notes: list[str] = []
    if allocation:
        notes.append("확정 배분(selected_allocation) 존재 — 이 맥락이 최우선 truth.")
    else:
        notes.append("확정 배분 없음 — 주문 후보 금지(selected_allocation_required).")
    if obj["is_set"]:
        g = obj["objective"]["investment_goal"]
        notes.append(f"계좌 목적={g} → '최선'은 {obj['criteria'].get('headline')}.")
    else:
        notes.append("계좌 목적 미설정 — 기준을 가정하지 않음(먼저 입력 권장).")
    if views:
        notes.append(f"계좌 견해 {len(views)}건(사용자 피드백·1급 입력).")
    if lessons:
        notes.append(f"이 계좌 과거 판단 {len(lessons)}건(계좌 격리).")

    return {
        "account_index": acct,
        # 우선순위 순서 — 계좌가 공통보다 먼저
        "selected_allocation": allocation,   # ①
        "objective": obj,                    # ②
        "policy": pol,                       # ③
        "views": views,                      # ④
        "lessons": lessons,                  # ⑤
        "priority": [
            "selected_allocation", "objective", "policy", "views", "lessons",
        ],
        "notes": notes,
        # 안전 단언
        "isolated": True,         # account_index 한정 — 타 계좌 누수 없음
        "advisory_only": True,    # 자동 적용 아님
        "applied": False,
        "generated_at": _now(),
    }


# ============================================================
# CLI
# ============================================================
def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="account_memory — 계좌별 통합 판단 맥락(계좌 최우선)")
    p.add_argument("--account", type=int, required=True)
    a = p.parse_args(argv)
    try:
        out = account_context(a.account)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
