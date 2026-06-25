"""Growth Middleware — 모든 Agent 작업이 **강제로** prehook → 실행 → posthook → lesson 루프를 타게 하는 공통 실행기.

CEO 원칙(CLAUDE.md §11): 작업 전 과거를 읽고, 작업 중 판단하고, 작업 후 배운 것을 저장하고, 다음엔 더 나은 상태로 시작.
이 미들웨어를 거치지 않은 작업은 "성장하지 않는 작업"이며 금지 대상이다.

강제 규칙:
  - task_type 필수 · agent_name 필수 (없으면 ValueError — task_type/agent 없는 작업 금지).
  - account-bound task 인데 account_index 없으면 prehook 이 hard-block (계좌 작업인데 account_id 없는 실행 금지).
  - prehook gate=block 이면 **fn 실행 안 함** → blocked 로 마감 (잘못된 전제로 작업 시작 금지).
  - fn 실행 후 **반드시 posthook(finalize)** → lesson candidate/feedback/scoped memory 저장 (posthook 없는 DONE 금지).
  - posthook 저장 실패 시 done 금지(예외 전파).

fn 계약:  fn(input, ctx) -> dict | any
  ctx = {task_id, gate, checks, memory, account_memory, common_memory, feedback, refs, account_index, task_type, agent}
  반환이 dict 이고 아래 키가 있으면 posthook 으로 전달:
    result, outcome, lesson_candidates, feedback, scoped_memories, validations, success, failure_reason, next_action, unresolved_risk
  dict 가 아니면 그 값 자체를 result 로 보고 자동 outcome 생성.
"""
from __future__ import annotations

from ..store import db as store_db
from . import prehooks, posthooks, tasks


def _split_artifacts(ret) -> dict:
    """fn 반환에서 result + 성장 산출물 분리. dict 가 아니면 result 로만 취급."""
    if isinstance(ret, dict) and any(
        k in ret for k in ("result", "lesson_candidates", "feedback", "scoped_memories",
                            "outcome", "validations", "success", "failure_reason")
    ):
        return {
            "result": ret.get("result", {k: v for k, v in ret.items() if k not in _ARTIFACT_KEYS}),
            "outcome": ret.get("outcome"),
            "lesson_candidates": ret.get("lesson_candidates") or [],
            "feedback": ret.get("feedback") or [],
            "scoped_memories": ret.get("scoped_memories") or [],
            "validations": ret.get("validations") or [],
            "success": ret.get("success", True),
            "failure_reason": ret.get("failure_reason"),
            "next_action": ret.get("next_action"),
            "unresolved_risk": ret.get("unresolved_risk"),
        }
    return {"result": ret, "outcome": None, "lesson_candidates": [], "feedback": [],
            "scoped_memories": [], "validations": [], "success": True, "failure_reason": None,
            "next_action": None, "unresolved_risk": None}


_ARTIFACT_KEYS = {"outcome", "lesson_candidates", "feedback", "scoped_memories", "validations",
                  "success", "failure_reason", "next_action", "unresolved_risk"}


def run_task(task_type: str, agent_name: str, fn, *, account_index: int | None = None,
             refs: list[str] | None = None, input=None, record_failure: bool = True) -> dict:
    """성장 루프를 강제하며 task 를 실행한다. 반환:
      {ok, gate, task_id, blocked, reasons, result, posthook, success}
    """
    if not task_type:
        raise ValueError("run_task: task_type 필수 (task_type 없는 작업 금지 — CLAUDE.md §11)")
    if not agent_name:
        raise ValueError("run_task: agent_name 필수 (agent 없는 작업 금지)")

    conn = store_db.connect()
    try:
        # 1) PREHOOK — 게이트 + memory 로드 + task provenance 개시.
        pre = prehooks.prepare(agent_name, task_type, account_index=account_index, refs=refs, conn=conn)
        task_id = pre.get("task_id")
        ctx = {
            "task_id": task_id, "gate": pre.get("gate"), "checks": pre.get("checks", []),
            "memory": pre.get("memory", []), "account_memory": pre.get("account_memory", []),
            "common_memory": pre.get("common_memory", []), "feedback": pre.get("feedback", []),
            "refs": refs or [], "account_index": account_index, "task_type": task_type, "agent": agent_name,
        }

        # 2) GATE — block 이면 fn 실행하지 않고 마감 (잘못된 전제 차단).
        if pre.get("gate") == "block":
            reasons = pre.get("reasons", [])
            posthooks.finalize(task_id, status="blocked",
                               outcome={"blocked": True, "task_type": task_type},
                               block_reason="; ".join(reasons) or "prehook gate=block", conn=conn)
            return {"ok": False, "gate": "block", "task_id": task_id, "blocked": True,
                    "reasons": reasons, "result": None, "success": False}

        # 3) 실행 — fn 은 prehook ctx 를 받아 과거(memory/feedback)를 참조해 판단.
        try:
            ret = fn(input, ctx)
        except Exception as e:  # noqa: BLE001 — 실패도 자산: posthook 으로 실패 패턴 기록.
            if record_failure:
                _record_failure(conn, task_type, agent_name, account_index, str(e))
                posthooks.finalize(task_id, status="failed",
                                   outcome={"error": str(e), "task_type": task_type},
                                   block_reason=f"실행 예외: {e}", conn=conn)
            return {"ok": False, "gate": "pass", "task_id": task_id, "blocked": False,
                    "reasons": [f"실행 예외: {e}"], "result": None, "success": False}

        art = _split_artifacts(ret)

        # 4) validation 실패 → 실패 패턴 + regression 후보로 기록(반복 실패 = 다음 작업의 자산).
        failed_validations = [v for v in art["validations"] if isinstance(v, dict) and not v.get("ok", True)]
        success = art["success"] and not failed_validations
        if failed_validations and record_failure:
            for v in failed_validations:
                _record_failure(conn, task_type, agent_name, account_index,
                                v.get("detail") or v.get("name") or "validation 실패")

        # 5) POSTHOOK — 반드시 실행 (lesson candidate/feedback/scoped memory 저장). 실패 시 done 금지.
        ph = posthooks.finalize(
            task_id,
            status="done" if success else "failed",
            outcome=art["outcome"] or {"task_type": task_type, "validations": art["validations"]},
            lesson_candidates=art["lesson_candidates"], feedback=art["feedback"],
            scoped_memories=art["scoped_memories"], next_action=art["next_action"],
            unresolved_risk=art["unresolved_risk"],
            block_reason=art["failure_reason"], conn=conn,
        )
        if not ph.get("ok"):
            raise RuntimeError("posthook 저장 실패 — DONE 금지(성장 기록 누락 방지)")

        return {"ok": True, "gate": "pass", "task_id": task_id, "blocked": False, "reasons": [],
                "result": art["result"], "posthook": ph, "success": success,
                "validations": art["validations"]}
    finally:
        conn.close()


def _record_failure(conn, task_type, agent_name, account_index, detail: str) -> None:
    """task_failure_patterns 적재 — 반복 실패는 이후 prehook 에서 금지 rule/regression 으로 로드."""
    try:
        conn.execute(
            "INSERT INTO task_failure_patterns(task_type, agent_name, account_index, detail, created_at) "
            "VALUES(?,?,?,?, datetime('now'))",
            (task_type, agent_name, account_index, detail[:500]),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — 실패 기록 실패가 작업을 막지 않게.
        pass
