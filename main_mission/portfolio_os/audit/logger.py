"""AuditLogger — 모든 중요 행위를 audit_logs 에 영속 (UTC 타임스탬프, 비밀값 차단)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..store import db as store_db
from .secrets_detector import scan


class AuditError(RuntimeError):
    pass


def record(
    action: str,
    *,
    actor: str | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    mode: str | None = None,
    level: str = "INFO",
    payload: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """감사로그 1건 기록. 비밀값 포함 시 AuditError (기록 안 됨). 반환: row id."""
    payload = payload or {}
    hit = scan(payload)
    if hit:
        raise AuditError(f"감사로그 기록 차단 — {hit} (자격증명은 .env 에만)")
    if level not in ("CRITICAL", "WARNING", "INFO"):
        level = "INFO"

    own = conn is None
    conn = conn or store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO audit_logs(actor, action, entity_type, entity_id, mode, level, payload, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                actor, action, entity_type, entity_id, mode, level,
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        if own:
            conn.commit()
        return int(cur.lastrowid)
    finally:
        if own:
            conn.close()
