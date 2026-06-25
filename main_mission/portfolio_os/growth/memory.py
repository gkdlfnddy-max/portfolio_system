"""scope/agent/freshness-가중 memory recall + negative feedback memory.

recall(): registry의 Agent scope를 따라 lessons를 decay-가중으로 불러온다.
  - archived 제외, freshness 갱신(touch), scope 태깅
  - "outdated memory를 계속 참조하지 않는가" → decay로 자동 후순위/archive
record_feedback()/recall_feedback(): 사용자의 거절/수정도 학습(negative memory).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .. import lessons as lessons_mod
from . import registry
from ..store import db as store_db

# scope_type → 한글 출처 라벨 (UI attribution).
_SCOPE_LABELS = {
    "account": "이 계좌 정책 메모리",
    "user": "CEO 공통 성향",
    "agent": "공통 Agent lesson",
    "evidence": "외부 evidence",
    "task": "현재 작업 맥락",
}
VALID_SCOPE_TYPES = {"account", "user", "agent", "task"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def recall(agent: str, *, account_index: int | None = None, refs: list[str] | None = None,
           per_scope: int = 5, touch: bool = True, conn=None) -> list[dict]:
    """Agent scope별로 lessons를 decay-가중 recall. 결과 각 항목에 scope/eff_confidence 포함.

    refs 주어지면 해당 ref 우선 + scope 일반 검색 병합. touch=True면 freshness 갱신.
    """
    scopes = registry.scopes_for(agent, conn=conn)
    if not scopes:
        scopes = ["premise", "sector", "decision", "risk", "market"]  # 미등록 agent 전역 fallback
    seen: dict[int, dict] = {}
    for scope in scopes:
        # ref 지정 검색 먼저(정밀), 이어 scope 일반 검색(광범위).
        queries = [(scope, r) for r in (refs or [])] + [(scope, None)]
        for sc, rf in queries:
            for item in lessons_mod.search(scope=sc, ref=rf, limit=per_scope):
                if item["id"] not in seen:
                    item["matched_scope"] = sc
                    item["matched_ref"] = rf
                    seen[item["id"]] = item
    items = sorted(seen.values(), key=lambda d: d.get("eff_confidence", 0), reverse=True)
    if touch and items:
        lessons_mod.touch([i["id"] for i in items])
    return items


def record_feedback(kind: str, detail: str, *, account_index: int | None = None,
                    agent: str | None = None, scope: str | None = None, ref: str | None = None,
                    source_ref: str | None = None, conn=None) -> dict:
    """negative memory 적재. kind: rejected_advice|user_edit|override|unsaved_consult."""
    own = conn is None
    conn = conn or store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO feedback_memory(account_index, agent, kind, scope, ref, detail, source_ref, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (account_index, agent, kind, scope, ref, detail, source_ref, _now()),
        )
        conn.commit()
        return {"ok": True, "feedback_id": cur.lastrowid}
    finally:
        if own:
            conn.close()


def recall_feedback(account_index: int | None = None, agent: str | None = None,
                    limit: int = 10, conn=None) -> list[dict]:
    """prehook이 "이전에 사용자가 거절/수정한 방향"을 읽어 회피하도록."""
    own = conn is None
    conn = conn or store_db.connect()
    try:
        sql = "SELECT id, account_index, agent, kind, scope, ref, detail, source_ref, created_at FROM feedback_memory WHERE 1=1"
        args: list = []
        if account_index is not None:
            sql += " AND (account_index=? OR account_index IS NULL)"; args.append(account_index)
        if agent:
            sql += " AND (agent=? OR agent IS NULL)"; args.append(agent)
        sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        if own:
            conn.close()


# ============================================================
# CEO memory scope 지시 — 통합 scoped 메모리 (agent_memories 테이블)
# "계좌별 실행은 분리, 전문 Agent 지식은 공통 성장, 최종 적용은 계좌별 정책 우선."
# ============================================================

def remember(scope_type: str, title: str, body: str, *, account_index: int | None = None,
             agent_name: str | None = None, theme: str | None = None, sector: str | None = None,
             confidence: float = 0.0, source: str = "agent", task_type: str | None = None,
             evidence_ids=None, policy_version_id: int | None = None, decision_id: int | None = None,
             scope_id: str | None = None, conn=None) -> dict:
    """scope_type 별 통합 메모리 적재.

    - account scope → account_index 를 채운다(계좌별 실행 분리).
    - user/agent scope → account_index = NULL (공통 성장). agent scope 는 promoted=1 이어야
      계좌 간 재사용(recall_scoped) 대상.
    - task scope → 휘발성. 적재는 하되 archived=1 로 즉시 표시(승격/계좌간 재사용 대상 아님).
    """
    if scope_type not in VALID_SCOPE_TYPES:
        raise ValueError(f"invalid scope_type {scope_type!r}")
    # 계좌 분리/공통 성장 원칙 강제: account 만 account_index 보유.
    if scope_type != "account":
        account_index = None
    promoted = 1 if scope_type in ("user",) else 0  # user 공통 성향은 항상 공통 적용.
    archived = 1 if scope_type == "task" else 0      # task 는 휘발성.
    if isinstance(evidence_ids, (list, tuple)):
        evidence_ids = json.dumps(list(evidence_ids), ensure_ascii=False)
    own = conn is None
    conn = conn or store_db.connect()
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO agent_memories(scope_type, scope_id, agent_name, task_type, account_index, "
            "theme, sector, title, body, confidence, freshness_at, source, promoted, archived, "
            "evidence_ids, policy_version_id, decision_id, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scope_type, scope_id, agent_name, task_type, account_index, theme, sector, title, body,
             float(confidence or 0.0), now, source, promoted, archived,
             evidence_ids, policy_version_id, decision_id, now, now),
        )
        conn.commit()
        return {"ok": True, "memory_id": cur.lastrowid, "scope_type": scope_type,
                "promoted": promoted, "archived": archived}
    finally:
        if own:
            conn.close()


# ============================================================
# 익명화 — agent scope 로 승격할 때 개인/계좌 식별정보 제거/일반화.
# 원칙(CEO): 전문 Agent 는 공통 성장(agent-scoped promoted lesson)하되,
#   개인/계좌 식별 정보가 promoted lesson 에 절대 섞이면 안 된다.
#   일반화된 투자 원칙(테마 방향·방어자산 계산·risk 패턴)은 보존한다.
# 적용 대상: title/body 텍스트. 적용 시점: promote(승격본) — account-scoped 원본은 불변.
# ============================================================

# 식별정보 → 일반화 토큰 치환 규칙. (순서 중요: 긴/구체 패턴 먼저)
_ANON_RULES: tuple[tuple[re.Pattern, str], ...] = (
    # 이메일 → [사용자]
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[사용자]"),
    # user_id / account_id 등 키=값 / 별칭 (user_a, user-12, account_3).
    # 한글 조사("의","는" 등)가 공백 없이 붙는 경우가 있어 trailing \b 를 쓰지 않는다.
    (re.compile(r"(?<![A-Za-z])user[_-]?[A-Za-z0-9]+", re.IGNORECASE), "[사용자]"),
    (re.compile(r"(?<![A-Za-z])account[_-]?(?:index|id|no|num)?[_-]?\d+", re.IGNORECASE), "[계좌]"),
    # "N번 계좌" / "계좌 N" / "N계좌" → [계좌]
    (re.compile(r"\d+\s*번?\s*계좌"), "[계좌]"),
    (re.compile(r"계좌\s*\d+\s*번?"), "[계좌]"),
    # 계좌번호: 하이픈 구분(110-22-334455 등) 먼저 → [계좌]
    (re.compile(r"\b\d{2,}(?:-\d{2,}){2,}\b"), "[계좌]"),
    # 계좌번호: 6자리 이상 연속 숫자 → [계좌]
    (re.compile(r"\b\d{6,}\b"), "[계좌]"),
    # 금액: 'N원' / 'N,NNN KRW' / 'N만원' / 'N억' → [금액]
    (re.compile(r"\d[\d,]*\s*(?:KRW|krw|원|만원|억원?|천원)"), "[금액]"),
    (re.compile(r"\d[\d,]*\s*(?:USD|usd|달러|\$)"), "[금액]"),
    # 보유 수량: 'N주' / 'N 주' → [수량]
    (re.compile(r"\d[\d,]*\s*주\b"), "[수량]"),
    (re.compile(r"\d[\d,]*\s*(?:계약|좌)\b"), "[수량]"),
)


def _anonymize(text: str | None) -> str:
    """promoted/agent-scope 로 올릴 때 개인·계좌 식별정보를 제거/일반화한다.

    제거/일반화: 계좌번호·금액·보유수량·user_id/email·"N번 계좌"·account_index·인명/별칭.
    보존: 일반화된 투자 원칙(테마 방향, 방어자산 계산, risk 패턴 등).

    예) "user_a의 1번 계좌는 삼성전자 500주 보유, 반도체 hedge 원함"
        → "[사용자]의 [계좌]는 삼성전자 [수량] 보유, 반도체 hedge 원함"
    (식별정보는 토큰화되고, 테마/hedge 원칙 텍스트는 살아남는다.)
    """
    if not text:
        return text or ""
    out = text
    for pat, repl in _ANON_RULES:
        out = pat.sub(repl, out)
    # 연속 공백 정리(치환 후 잔여 공백).
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    return out


def has_identifiers(text: str | None) -> bool:
    """텍스트에 식별정보 패턴이 남아있으면 True (테스트/안전장치용)."""
    if not text:
        return False
    return any(pat.search(text) for pat, _ in _ANON_RULES)


def promote_agent_memory(memory_id: int, conn=None) -> dict:
    """agent scope 메모리를 promoted=1 로 — 계좌 간 공통 재사용(공통 성장) 대상화.

    승격본은 반드시 익명화한다: title/body 에 `_anonymize` 적용 + account_index/scope_id
    제거(NULL). 이렇게 해야 개인/계좌 식별 정보가 공통 promoted lesson 에 섞이지 않는다.
    account-scoped 원본은 별도 row 이므로 건드리지 않는다(승격본만 익명화).
    """
    own = conn is None
    conn = conn or store_db.connect()
    try:
        row = conn.execute(
            "SELECT title, body FROM agent_memories WHERE id=? AND scope_type='agent'",
            (memory_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return {"ok": False, "memory_id": memory_id, "reason": "not_agent_scope_or_missing"}
        anon_title = _anonymize(row["title"])
        anon_body = _anonymize(row["body"])
        conn.execute(
            "UPDATE agent_memories SET promoted=1, title=?, body=?, account_index=NULL, "
            "scope_id=NULL, updated_at=? WHERE id=? AND scope_type='agent'",
            (anon_title, anon_body, _now(), memory_id),
        )
        conn.commit()
        return {"ok": True, "memory_id": memory_id, "title": anon_title, "body": anon_body}
    finally:
        if own:
            conn.close()


def _tag(row: dict) -> dict:
    d = dict(row)
    st = d.get("scope_type") or "agent"
    d["scope_type"] = st
    d["source_label"] = _SCOPE_LABELS.get(st, st)
    if d.get("evidence_ids"):
        try:
            d["evidence_ids"] = json.loads(d["evidence_ids"])
        except (ValueError, TypeError):
            pass
    return d


def recall_scoped(agent_name: str, account_index: int | None, *, theme: str | None = None,
                  sector: str | None = None, limit_per: int = 5, conn=None) -> list[dict]:
    """CEO §8 우선순위로 병합된 SINGLE 리스트 반환 (archived 제외).

    우선순위:
      1) account-scoped (이 account_index)        — 계좌별 정책 메모리
      2) user-scoped                              — CEO 공통 성향
      3) agent-scoped promoted=1                  — 공통 Agent lesson (계좌 간 공통 성장)
      4) related evidence (evidence_documents)    — theme/sector 매칭 외부 근거
      5) task-scoped 현재 맥락                     — (휘발성; archived 제외라 보통 비어있음)
    각 항목에 scope_type + source_label 태깅.
    """
    own = conn is None
    conn = conn or store_db.connect()
    try:
        out: list[dict] = []

        def q_mem(where: str, args: list) -> list[dict]:
            sql = ("SELECT * FROM agent_memories WHERE archived=0 AND " + where +
                   " ORDER BY promoted DESC, confidence DESC, id DESC LIMIT ?")
            return [_tag(dict(r)) for r in conn.execute(sql, args + [limit_per]).fetchall()]

        # 1) account-scoped (이 계좌만)
        if account_index is not None:
            out += q_mem("scope_type='account' AND account_index=?", [account_index])
        # 2) user-scoped (공통 성향)
        out += q_mem("scope_type='user'", [])
        # 3) agent-scoped promoted (공통 Agent lesson) — agent 일치 또는 전역(agent_name NULL)
        out += q_mem("scope_type='agent' AND promoted=1 AND (agent_name=? OR agent_name IS NULL)",
                     [agent_name])
        # 4) related evidence (theme/sector)
        ev_rows = []
        if theme or sector:
            esql = "SELECT id, scope, ref, title, body, confidence, affected_theme, affected_asset, freshness FROM evidence_documents WHERE 1=0"
            eargs: list = []
            conds = []
            if theme:
                conds.append("(affected_theme=? OR ref=?)"); eargs += [theme, theme]
            if sector:
                conds.append("(affected_asset=? OR ref=?)"); eargs += [sector, sector]
            if conds:
                esql = ("SELECT id, scope, ref, title, body, confidence, affected_theme, affected_asset, "
                        "freshness FROM evidence_documents WHERE " + " OR ".join(conds) + " ORDER BY id DESC LIMIT ?")
                ev_rows = conn.execute(esql, eargs + [limit_per]).fetchall()
        for r in ev_rows:
            d = dict(r)
            d["scope_type"] = "evidence"
            d["source_label"] = _SCOPE_LABELS["evidence"]
            d["theme"] = d.get("affected_theme")
            d["sector"] = d.get("affected_asset")
            out.append(d)
        # 5) task-scoped 현재 맥락 (archived 제외 → 보통 비지만, 미archive task 메모리가 있으면 포함)
        out += q_mem("scope_type='task' AND (agent_name=? OR agent_name IS NULL)", [agent_name])

        return out
    finally:
        if own:
            conn.close()


def resolve_conflicts(items: list[dict], policy: dict | None) -> tuple[list[dict], list[dict]]:
    """CEO §7 — 메모리 제안이 계좌 정책과 충돌하면 ACCOUNT POLICY WINS.

    규칙:
      - 메모리가 테마 tilt 를 함의(theme/sector 보유)하는데 정책이 테마를 불허
        (forbidden_assets 에 'themes'/'theme' 포함, 또는 themes 키가 빈/False) → 항목 억제(drop).
      - 메모리가 정책 cash_band.min 미만 현금을 제안 → clamp/annotate (드롭하지 않고 정책값으로 보정).
    반환: (kept_items, conflicts) — conflicts 각 항목 {memory, policy_rule, resolution:'account_policy_wins'}.
    """
    policy = policy or {}
    cash_band = policy.get("cash_band") or {}
    cash_min = cash_band.get("min")
    forbidden = set(policy.get("forbidden_assets") or [])
    # 테마 불허 판정: forbidden 에 명시되거나, themes 키가 명시적으로 비어있음.
    themes_allowed = True
    if "themes" in forbidden or "theme" in forbidden or "tilt" in forbidden:
        themes_allowed = False
    if "themes" in policy and not policy.get("themes"):
        themes_allowed = False
    if "themes_allowed" in policy and not policy.get("themes_allowed"):
        themes_allowed = False

    kept: list[dict] = []
    conflicts: list[dict] = []
    for it in items:
        item = dict(it)
        implies_theme = bool(item.get("theme") or item.get("sector"))
        suggested_cash = _suggested_cash(item)

        # (1) 테마 tilt 충돌 → 억제(drop).
        if implies_theme and not themes_allowed:
            conflicts.append({"memory": item, "policy_rule": "themes_forbidden",
                              "resolution": "account_policy_wins"})
            continue

        # (2) 현금 하한 충돌 → clamp + annotate (드롭 안 함).
        if suggested_cash is not None and cash_min is not None and suggested_cash < cash_min:
            item["conflict_note"] = (f"제안 현금 {suggested_cash}% < 정책 현금밴드 하한 {cash_min}% "
                                     f"→ 계좌 정책 우선(clamp {cash_min}%)")
            item["clamped_cash_pct"] = cash_min
            item["suggested_cash_pct"] = suggested_cash
            conflicts.append({"memory": dict(it), "policy_rule": f"cash_band.min={cash_min}",
                              "resolution": "account_policy_wins"})
            kept.append(item)
            continue

        kept.append(item)
    return kept, conflicts


def _suggested_cash(item: dict):
    """메모리 항목에서 제안 현금 비중(%) 추출. 구조화 필드 우선, 없으면 본문 파싱."""
    for k in ("suggest_cash_pct", "cash_pct", "suggested_cash"):
        v = item.get(k)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    import re
    text = f"{item.get('title') or ''} {item.get('body') or ''}"
    m = re.search(r"현금\s*(\d+(?:\.\d+)?)\s*%", text)
    if m:
        return float(m.group(1))
    return None


def explain_sources(items: list[dict], *, account_index: int | None = None,
                    policy_version=None, selected_allocation_id=None) -> str:
    """UI 한글 attribution 문자열 — 어떤 scope 들이 반영됐는지로 구성."""
    present = {it.get("scope_type") for it in (items or [])}
    parts: list[str] = []
    if "agent" in present:
        parts.append("공통 Agent lesson")
    if "user" in present:
        parts.append("CEO 공통 성향")
    if "account" in present:
        acc = f"{account_index}번 계좌 정책" if account_index is not None else "계좌 정책"
        # 현금밴드 충돌 보정이 있었으면 명시.
        if any(it.get("clamped_cash_pct") is not None for it in items):
            acc += "(현금밴드)"
        parts.append(acc)
    if "evidence" in present:
        parts.append("외부 evidence")
    if "task" in present:
        parts.append("현재 작업 맥락")
    if policy_version is not None:
        parts.append(f"policy v{policy_version}")
    if selected_allocation_id is not None:
        parts.append("최근 선택 allocation")
    if not parts:
        return "참조한 메모리가 없습니다."
    return "이 조언은 " + " + ".join(parts) + "을(를) 함께 반영했습니다."
