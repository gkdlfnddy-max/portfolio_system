"""Agent별 memory scope 레지스트리 — prehook이 "이 Agent는 어떤 scope를 우선 읽나"를 결정.

memory scope 분리 원칙(CEO §6): Agent별/Task별로 읽는 메모리가 다르다.
  - theme-sector-advisor : 섹터/테마/시장 메모리 우선 (개인 정책 메모리는 부차)
  - view-coach           : 전제(premise)/의사결정 메모리 우선 (+ profile history는 prehook이 별도 로드)
  - risk-chief           : risk/decision 메모리만
DB(agent_memory_scope)에 선언하여 하드코딩이 아니라 데이터로 운영. seed()는 멱등.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..store import db as store_db

# (agent, [(scope, priority, note), ...]) — priority 작을수록 먼저 검색.
DEFAULT_SCOPES: dict[str, list[tuple[str, int, str]]] = {
    "theme-sector-advisor": [
        ("sector", 10, "관심 테마/섹터 해석의 1차 근거"),
        ("instrument", 20, "테마 대표 종목/ETF 메모리"),
        ("market", 30, "시장 국면(과열/변동성)"),
        ("economy", 40, "금리/환율 등 매크로"),
    ],
    "view-coach": [
        ("premise", 10, "대전제/중전제 코칭의 1차 근거"),
        ("decision", 20, "과거 의사결정 패턴"),
        ("risk", 30, "성향과 위험허용도 정합성"),
    ],
    "risk-chief": [
        ("risk", 10, "반복 차단 사유 등 risk 메모리"),
        ("decision", 20, "결정 시점 위반 이력"),
    ],
    "portfolio-strategy-chief": [
        ("premise", 10, ""), ("sector", 20, ""), ("decision", 30, ""), ("market", 40, ""),
    ],
    "broker-chief": [
        ("decision", 10, ""), ("risk", 20, ""), ("premise", 30, ""), ("sector", 40, ""), ("market", 50, ""),
    ],
    "research-chief": [
        ("market", 10, ""), ("economy", 20, ""), ("sector", 30, ""), ("instrument", 40, ""),
    ],
    "memory-lesson-chief": [
        ("market", 10, ""), ("economy", 10, ""), ("sector", 10, ""), ("instrument", 10, ""),
        ("premise", 10, ""), ("decision", 10, ""), ("risk", 10, ""),
    ],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed(conn=None) -> dict:
    """기본 scope를 멱등 upsert. 이미 있으면 note/priority만 갱신."""
    own = conn is None
    conn = conn or store_db.connect()
    n = 0
    try:
        for agent, scopes in DEFAULT_SCOPES.items():
            for scope, priority, note in scopes:
                conn.execute(
                    "INSERT INTO agent_memory_scope(agent, scope, priority, note, created_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(agent, scope) DO UPDATE SET priority=excluded.priority, note=excluded.note",
                    (agent, scope, priority, note, _now()),
                )
                n += 1
        conn.commit()
        return {"ok": True, "upserted": n, "agents": list(DEFAULT_SCOPES)}
    finally:
        if own:
            conn.close()


def scopes_for(agent: str, conn=None) -> list[str]:
    """Agent의 검색 scope를 priority 순으로. 미등록 agent는 빈 리스트(→ prehook이 전역 fallback)."""
    own = conn is None
    conn = conn or store_db.connect()
    try:
        rows = conn.execute(
            "SELECT scope FROM agent_memory_scope WHERE agent=? ORDER BY priority, scope", (agent,)
        ).fetchall()
        if not rows and agent in DEFAULT_SCOPES:
            # DB 미시드 상태 fallback — 코드 기본값으로라도 동작.
            return [s for s, _, _ in sorted(DEFAULT_SCOPES[agent], key=lambda x: x[1])]
        return [r["scope"] for r in rows]
    finally:
        if own:
            conn.close()
