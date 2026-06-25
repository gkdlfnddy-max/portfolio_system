"""prehook — 작업 전 안전 점검 + 관련 memory 로드 + task provenance 개시.

단순 로딩이 아니라 "게이트"다: 잘못된 전제로 작업이 시작되지 않게 막는다.
  - decision: selected allocation 없으면 hard-block, stale snapshot이면 hard-block
  - risk/allocation: 정책 없으면 block
  - advisor(theme/view/consult): block 없음(조언은 안전) — 단 memory/feedback는 항상 로드
모든 prehook 결과(게이트·점검·참조 memory)는 tasks 행에 provenance로 저장된다.
"""
from __future__ import annotations

from ..store import db as store_db
from .. import guards
from . import memory, tasks

# account_index 필수(계좌 귀속) task_type — account_id 없으면 hard-block (CEO: 계좌별 실행 분리).
# Track B: 핵심 실행 경로(decision_compute/daily_portfolio_review/order_submit/broker_sync)도
#          계좌 귀속이면 account_id 없을 때 hard-block 되도록 등록.
ACCOUNT_BOUND = {
    "decision", "selection", "risk_check", "allocation_generate",
    "decision_compute", "daily_portfolio_review", "order_submit", "broker_sync",
    "allocation_generation",
}

# task_type별 안전 요구사항. (needs_policy, needs_selected_allocation, needs_fresh_snapshot)
REQUIRE: dict[str, dict] = {
    "decision":            {"policy": True,  "selected_allocation": True,  "fresh_snapshot": True},
    "risk_check":          {"policy": True,  "selected_allocation": False, "fresh_snapshot": False},
    "selection":           {"policy": True,  "selected_allocation": False, "fresh_snapshot": False},
    "allocation_generate": {"policy": True,  "selected_allocation": False, "fresh_snapshot": False},
    "policy_compile":      {"policy": False, "selected_allocation": False, "fresh_snapshot": False},
    "profile_save":        {"policy": False, "selected_allocation": False, "fresh_snapshot": False},
    "theme_advice":        {"policy": False, "selected_allocation": False, "fresh_snapshot": False},
    "view_coach":          {"policy": False, "selected_allocation": False, "fresh_snapshot": False},
    "consult":             {"policy": False, "selected_allocation": False, "fresh_snapshot": False},
    "sync":                {"policy": False, "selected_allocation": False, "fresh_snapshot": False},
    # --- Track B: 핵심 실행 경로 ---
    # decision_compute: decision.compute 의 자체 가드와 의미 일치 — selected allocation 필수 +
    #   신선 스냅샷 필수. **정책은 요구 금지**(본문이 정책 미존재 시 compile_policy 로 폴백하므로).
    "decision_compute":    {"policy": False, "selected_allocation": True,  "fresh_snapshot": True},
    # daily_portfolio_review: 관망(watch)도 정상 결과 → 게이트로 막지 않는다(스냅샷/선택안 없음도
    #   review 가 watch 로 정직하게 보고해야 함). account_id 만 필수(ACCOUNT_BOUND).
    "daily_portfolio_review": {"policy": False, "selected_allocation": False, "fresh_snapshot": False},
    # order_submit: 모드/health/idempotency/live-lock 은 order_service 본문이 SSOT 로 유지.
    #   prehook 은 account_id 귀속만 게이트(정책/선택안 요구는 본문 책임).
    "order_submit":        {"policy": False, "selected_allocation": False, "fresh_snapshot": False},
    # broker_sync: 읽기 전용 수집 — 동기화로 신선 스냅샷을 *만드는* 작업이므로 fresh_snapshot 요구 금지.
    "broker_sync":         {"policy": False, "selected_allocation": False, "fresh_snapshot": False},
    # 추가 wrap 대상 — 본문이 정책 미존재 시 compile_policy 로 폴백하므로 policy 요구 금지(기존 동작 보존).
    "allocation_generation": {"policy": False, "selected_allocation": False, "fresh_snapshot": False},
}


def _parse_ts(s):
    # SSOT: guards.parse_ts (호환용 thin wrapper).
    return guards.parse_ts(s)


def _latest_policy(account_index):
    try:
        from .. import policy as policy_mod
        return policy_mod.latest(account_index)
    except Exception:
        return None


def _current_selection(account_index):
    try:
        from .. import selection as selection_mod
        return selection_mod.current(account_index)
    except Exception:
        return None


def _latest_snapshot(conn, account_index):
    if account_index is None:
        return None
    r = conn.execute(
        "SELECT id, is_stale, captured_at FROM account_snapshots WHERE account_index=? ORDER BY captured_at DESC, id DESC LIMIT 1",
        (account_index,),
    ).fetchone()
    return dict(r) if r else None


def _snapshot_stale(snap) -> bool:
    # SSOT: guards.snapshot_stale (호환용 thin wrapper).
    return guards.snapshot_stale(snap)


def prepare(agent: str, task_type: str, *, account_index: int | None = None,
            refs: list[str] | None = None, conn=None) -> dict:
    own = conn is None
    conn = conn or store_db.connect()
    try:
        req = REQUIRE.get(task_type, {"policy": False, "selected_allocation": False, "fresh_snapshot": False})
        checks: list[dict] = []
        reasons: list[str] = []

        policy = _latest_policy(account_index)
        policy_version = policy.get("version") if policy else None
        sel = _current_selection(account_index)
        selected_allocation_id = (sel or {}).get("id")
        snap = _latest_snapshot(conn, account_index)
        snap_id = (snap or {}).get("id")
        stale = _snapshot_stale(snap)

        def gate_check(name: str, ok: bool, need: bool, detail: str):
            checks.append({"name": name, "ok": ok, "required": need, "detail": detail})
            if need and not ok:
                reasons.append(detail)

        account_required = task_type in ACCOUNT_BOUND
        gate_check("account_id", account_index is not None, account_required,
                   "account_id 없음" if account_index is None else f"account #{account_index}")
        gate_check("policy", policy is not None, req["policy"],
                   "정책(policy)이 없음 — 먼저 profile 저장→policy compile 필요" if policy is None else f"policy v{policy_version}")
        gate_check("selected_allocation", selected_allocation_id is not None, req["selected_allocation"],
                   "selected allocation 없음 — 사람이 3안 중 확정해야 decision 생성 가능" if selected_allocation_id is None else f"selection #{selected_allocation_id}")
        gate_check("fresh_snapshot", not stale, req["fresh_snapshot"],
                   "계좌 스냅샷이 stale — 동기화 후 진행(낡은 잔고로 주문 금지)" if stale else "snapshot fresh")

        gate = "block" if reasons else "pass"

        mem = memory.recall(agent, account_index=account_index, refs=refs, conn=conn)
        fb = memory.recall_feedback(account_index=account_index, agent=agent, conn=conn)

        # CEO memory scope: scoped 메모리도 함께 로드 (공통 성장 + 계좌별 정책 분리).
        scoped = memory.recall_scoped(agent, account_index, conn=conn)
        account_memory = [m for m in scoped if m.get("scope_type") == "account"]
        common_memory = [m for m in scoped if m.get("scope_type") in ("user", "agent")]

        prehook_payload = {
            "gate": gate, "checks": checks, "reasons": reasons,
            "memory_count": len(mem), "feedback_count": len(fb),
            "account_memory_count": len(account_memory), "common_memory_count": len(common_memory),
            "refs": refs or [], "agent": agent, "task_type": task_type,
        }
        task_id = tasks.open_task(
            agent, task_type, account_index=account_index, policy_version=policy_version,
            selected_allocation_id=selected_allocation_id, account_snapshot_id=snap_id,
            prehook=prehook_payload, status=("running" if gate == "pass" else "blocked"), conn=conn,
        )

        links = [{"memory_kind": "lesson", "memory_id": m["id"], "scope": m.get("matched_scope"),
                  "ref": m.get("ref"), "relevance": m.get("eff_confidence")} for m in mem]
        links += [{"memory_kind": "feedback", "memory_id": f["id"], "scope": f.get("scope"), "ref": f.get("ref")} for f in fb]
        links += [{"memory_kind": f"scoped:{m.get('scope_type')}", "memory_id": m.get("id"),
                   "scope": m.get("scope_type"), "ref": m.get("theme") or m.get("sector"),
                   "relevance": m.get("confidence"), "note": m.get("source_label")} for m in scoped]
        if policy_version is not None:
            links.append({"memory_kind": "policy", "memory_id": policy_version, "note": "latest policy version"})
        if selected_allocation_id is not None:
            links.append({"memory_kind": "selected_allocation", "memory_id": selected_allocation_id})
        if snap_id is not None:
            links.append({"memory_kind": "snapshot", "memory_id": snap_id, "note": "stale" if stale else "fresh"})
        tasks.link_memory(task_id, links, conn=conn)

        return {
            "ok": True, "gate": gate, "task_id": task_id,
            "policy_version": policy_version, "selected_allocation_id": selected_allocation_id,
            "account_snapshot_id": snap_id, "snapshot_stale": stale,
            "checks": checks, "reasons": reasons,
            "memory": mem, "feedback": fb,
            "scoped_memory": scoped, "account_memory": account_memory, "common_memory": common_memory,
        }
    finally:
        if own:
            conn.close()
