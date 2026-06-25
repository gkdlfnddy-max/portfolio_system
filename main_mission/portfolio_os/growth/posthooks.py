"""posthook — 작업 후 정리.

원칙:
  - 배운 점은 **lesson candidate**로만 남긴다(즉시 lessons 승격 금지 — 반복 검증 후 promote()).
  - 사용자가 거절/수정한 내용은 **negative feedback memory**로 남긴다(다음 조언에서 회피).
  - task에 next_action / unresolved_risk를 기록해 중단·재개·후속이 추적되게 한다.
  - 실패한 작업도 사유(block_reason)와 함께 종료해 원인이 남게 한다.
"""
from __future__ import annotations

from ..store import db as store_db
from .. import lessons as lessons_mod
from . import memory, tasks


def finalize(task_id: int, *, status: str = "done", outcome: dict | None = None,
             lesson_candidates: list[dict] | None = None, next_action: str | None = None,
             unresolved_risk: str | None = None, feedback: list[dict] | None = None,
             scoped_memories: list[dict] | None = None,
             block_reason: str | None = None, conn=None) -> dict:
    """task를 마감하며 산출물을 정리.

    lesson_candidates: [{scope, title, body, ref?, account_index?, evidence_ref?, outcome?, confidence?, agent?}]
    feedback:          [{kind, detail, account_index?, agent?, scope?, ref?, source_ref?}]
    scoped_memories:   [{scope_type, title, body, account_index?, agent_name?, theme?, sector?,
                         confidence?, source?, task_type?, evidence_ids?, policy_version_id?, decision_id?}]
                       — CEO memory scope. account/user/agent 는 durable, task 는 휘발성(remember가 archived 처리).
    """
    own = conn is None
    conn = conn or store_db.connect()
    try:
        cand_ids, fb_ids, mem_ids = [], [], []
        for c in (lesson_candidates or []):
            res = lessons_mod.add_candidate(
                c["scope"], c["title"], c["body"], ref=c.get("ref"),
                account_index=c.get("account_index"), evidence_ref=c.get("evidence_ref"),
                outcome=c.get("outcome"), confidence=float(c.get("confidence", 0.0)),
                source=c.get("source", "posthook"), agent=c.get("agent"),
            )
            cand_ids.append(res["candidate_id"])
        for f in (feedback or []):
            res = memory.record_feedback(
                f["kind"], f["detail"], account_index=f.get("account_index"), agent=f.get("agent"),
                scope=f.get("scope"), ref=f.get("ref"), source_ref=f.get("source_ref"), conn=conn,
            )
            fb_ids.append(res["feedback_id"])
        for m in (scoped_memories or []):
            res = memory.remember(
                m["scope_type"], m.get("title", ""), m.get("body", ""),
                account_index=m.get("account_index"), agent_name=m.get("agent_name") or m.get("agent"),
                theme=m.get("theme"), sector=m.get("sector"), confidence=float(m.get("confidence", 0.0)),
                source=m.get("source", "posthook"), task_type=m.get("task_type"),
                evidence_ids=m.get("evidence_ids"), policy_version_id=m.get("policy_version_id"),
                decision_id=m.get("decision_id"), scope_id=m.get("scope_id"), conn=conn,
            )
            mem_ids.append({"id": res["memory_id"], "scope_type": res["scope_type"]})

        tasks.update_task(
            task_id, status=status, outcome=outcome, next_action=next_action,
            unresolved_risk=unresolved_risk, block_reason=block_reason, conn=conn,
        )
        # candidate → task provenance 역링크 (어떤 task가 어떤 후보를 남겼는가).
        links = [{"memory_kind": "lesson_candidate", "memory_id": cid} for cid in cand_ids]
        links += [{"memory_kind": "feedback", "memory_id": fid} for fid in fb_ids]
        links += [{"memory_kind": f"scoped:{m['scope_type']}", "memory_id": m["id"]} for m in mem_ids]
        tasks.link_memory(task_id, links, conn=conn)

        return {"ok": True, "task_id": task_id, "status": status,
                "lesson_candidates": cand_ids, "feedback": fb_ids,
                "scoped_memories": mem_ids}
    finally:
        if own:
            conn.close()
