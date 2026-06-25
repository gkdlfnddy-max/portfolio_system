"""표준 task 상태머신 + provenance.

모든 Agent 작업은 task 1행으로 추적된다: 어떤 정책/선택안/스냅샷을 기준으로(=prehook),
무엇을 했고(outcome), 다음에 뭘 해야 하며(next_action), 미해결 위험(unresolved_risk)은 무엇인가.
중단돼도 재개 가능하도록 status 전이를 표준화한다.

상태: open → running → (done | blocked | failed | cancelled)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..store import db as store_db

VALID_STATUS = {"open", "running", "done", "blocked", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_task(agent: str, task_type: str, *, account_index: int | None = None,
              policy_version: int | None = None, selected_allocation_id: int | None = None,
              account_snapshot_id: int | None = None, prehook: dict | None = None,
              status: str = "running", conn=None) -> int:
    own = conn is None
    conn = conn or store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO tasks(account_index, agent, task_type, status, policy_version, "
            "selected_allocation_id, account_snapshot_id, prehook, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (account_index, agent, task_type, status, policy_version, selected_allocation_id,
             account_snapshot_id, json.dumps(prehook, ensure_ascii=False) if prehook is not None else None,
             _now(), _now()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        if own:
            conn.close()


def link_memory(task_id: int, items: list[dict], conn=None) -> int:
    """task가 참조한 memory provenance 적재.
    items: [{memory_kind, memory_id?, scope?, ref?, relevance?, note?}]"""
    if not items:
        return 0
    own = conn is None
    conn = conn or store_db.connect()
    try:
        rows = [(task_id, it.get("memory_kind", "lesson"), it.get("memory_id"), it.get("scope"),
                 it.get("ref"), it.get("relevance"), it.get("note"), _now()) for it in items]
        conn.executemany(
            "INSERT INTO task_memory_links(task_id, memory_kind, memory_id, scope, ref, relevance, note, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)", rows,
        )
        conn.commit()
        return len(rows)
    finally:
        if own:
            conn.close()


def update_task(task_id: int, *, status: str | None = None, outcome: dict | None = None,
                next_action: str | None = None, unresolved_risk: str | None = None,
                block_reason: str | None = None, conn=None) -> dict:
    if status is not None and status not in VALID_STATUS:
        raise ValueError(f"invalid status {status!r}")
    own = conn is None
    conn = conn or store_db.connect()
    try:
        sets, args = ["updated_at=?"], [_now()]
        if status is not None:
            sets.append("status=?"); args.append(status)
        if outcome is not None:
            sets.append("outcome=?"); args.append(json.dumps(outcome, ensure_ascii=False))
        if next_action is not None:
            sets.append("next_action=?"); args.append(next_action)
        if unresolved_risk is not None:
            sets.append("unresolved_risk=?"); args.append(unresolved_risk)
        if block_reason is not None:
            sets.append("block_reason=?"); args.append(block_reason)
        args.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", args)
        conn.commit()
        return {"ok": True, "task_id": task_id, "status": status}
    finally:
        if own:
            conn.close()


def get_task(task_id: int, conn=None) -> dict | None:
    own = conn is None
    conn = conn or store_db.connect()
    try:
        r = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("prehook", "outcome"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (ValueError, TypeError):
                    pass
        links = conn.execute("SELECT memory_kind, memory_id, scope, ref, relevance FROM task_memory_links WHERE task_id=?", (task_id,)).fetchall()
        d["memory_links"] = [dict(x) for x in links]
        return d
    finally:
        if own:
            conn.close()
