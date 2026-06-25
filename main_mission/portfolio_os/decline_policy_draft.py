"""하락 징후 보수적 전환 제안 → **policy draft**(사람 승인 전 운용기준 조정 후보).

흐름(불변):
  분석(decline_scan) → 제안(shift_conservative) → **draft 생성**(저장은 draft 상태)
    → (사람 검토·승인) → policy version 반영 → allocation 재계산.

핵심 규칙:
  - **자동 적용 절대 금지.** draft 는 `auto_applied:false, requires_user_approval:true`.
  - draft 는 policy version 을 **만들지 않는다**(미반영). 저장은 기존 `advice_items` 의
    status='open'(미승인) 행으로만 — `policy.compile_policy` 는 status='accepted' 만 읽으므로
    승인 전에는 어떤 정책/비중에도 영향이 없다(자연스러운 사람 승인 게이트 재사용).
  - draft 가 담는 것: **운용기준 조정 후보**(현금밴드 상향 후보·위험자산 축소 후보 등) — 주문 아님.
    "하락 확정"·매수/매도 단정 금지.
  - confidence 낮으면(판단 강도 candidate_only) draft 도 '관망/주의·데이터 추가' 수준으로만.

지능 = 규칙(decline_scan) + Claude+메모리 성장. **Anthropic API 미사용.**

  python -m main_mission.portfolio_os.decline_policy_draft --account 1            # 스캔→draft
  python -m main_mission.portfolio_os.decline_policy_draft --account 1 --list     # draft 목록
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import decline_scan as scan_mod
from . import markers
from .store import db as store_db

# draft 로 저장하는 advice_items 의 식별 title prefix(중복 갱신·조회용).
DRAFT_TITLE_PREFIX = "[하락징후 draft] "
DRAFT_SOURCE = "decline_signal"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_draft(proposal: dict | None, *, account_index: int,
                summary: dict | None = None) -> dict:
    """보수적 전환 proposal → policy draft 객체(저장 전 순수 변환).

    proposal 가 None(위험 낮음) 이면 draft 없음(ok:True, has_draft:False) — 거짓 경보 금지.
    draft 는 항상 requires_user_approval:true, auto_applied:false.
    """
    if not proposal:
        return {
            "ok": True, "account_index": account_index, "has_draft": False,
            **markers.PROPOSAL_FLAGS,
            "note": "보수적 전환 트리거 미충족 — 현 운용기준 유지(관망). draft 없음.",
        }

    judgment = proposal.get("confidence_judgment") or {}
    strength = proposal.get("strength", "moderate")
    suggested_band = proposal.get("suggested_cash_band")
    # 신뢰도 미달이면 단정 금지 — draft 는 '후보/주의' 수준으로만(현금밴드 변경값 비제시).
    candidate_only = (judgment.get("allowed_strength") == "candidate_only")

    changes: list[dict] = []
    if not candidate_only and suggested_band:
        # 현금밴드 상향 '후보' — suggested_field/value 는 사람 승인 시 적용될 후보값.
        changes.append({
            "field": "cash_band",
            "kind": "cash_band_raise_candidate",
            "current": suggested_band.get("from"),
            "candidate": {"min": suggested_band.get("min"), "max": suggested_band.get("max")},
            "note": "현금밴드 상향 후보 — 승인 시 profile/policy 저장 경로로만 반영(자동 적용 금지).",
        })
    if proposal.get("reduce_risk_assets") and not candidate_only:
        changes.append({"field": "risk_assets", "kind": "reduce_risk_assets_candidate",
                        "note": "위험자산 비중 축소 후보(주문 아님 — 운용기준 조정 후보)."})
    if proposal.get("consider_hedge"):
        changes.append({"field": "hedge", "kind": "consider_hedge_candidate",
                        "note": "헤지(인버스 한도 내) 검토 후보 — 강한 신호일 때만."})
    if candidate_only:
        changes.append({"field": "data", "kind": "collect_more_data",
                        "note": "신뢰도 낮음 — 단정 금지. 관망/주의 + 데이터 추가 수집(후보)."})

    draft = {
        "ok": True,
        "account_index": account_index,
        "has_draft": True,
        "status": "draft",                       # 미반영(policy version 아님)
        **markers.PROPOSAL_FLAGS,                 # 자동 적용 금지 + 사람 승인 필요 (SSOT)
        "strength": strength,
        "asserted": bool(proposal.get("asserted")),  # 단정 허용 여부(신뢰도 낮으면 False)
        "overall_confidence": proposal.get("overall_confidence"),
        "confidence_judgment": judgment,
        "allowed_actions": proposal.get("allowed_actions"),
        "rationale": proposal.get("rationale"),
        "proposed_changes": changes,             # 운용기준 조정 '후보' 묶음(주문 아님)
        "source_summary": summary,
        "flow": "분석 → 제안 → draft → (사람 승인) → policy version → allocation 재계산",
        "note": ("policy draft 입니다 — 자동 적용 안 됨. 사람이 검토·승인해야 policy version 으로 "
                 "반영됩니다. 매수/매도·하락 확정 단정이 아니라 운용기준 조정 '후보'입니다."),
    }
    return draft


def _draft_advice_rows(draft: dict) -> list[dict]:
    """draft 의 proposed_changes → advice_items 행 후보(미승인 open). 사람 승인 게이트 재사용."""
    rows: list[dict] = []
    severity = "important" if draft.get("asserted") else "suggest"
    for ch in draft.get("proposed_changes", []):
        suggested_field = None
        suggested_value = None
        if ch.get("kind") == "cash_band_raise_candidate":
            cand = ch.get("candidate") or {}
            # 승인 시 profile 의 현금밴드 필드로 반영(사람이 decide → accepted 후 적용 경로).
            suggested_field = "cash_min_pct,cash_max_pct"
            suggested_value = json.dumps({"cash_min_pct": cand.get("min"),
                                          "cash_max_pct": cand.get("max")}, ensure_ascii=False)
        rows.append({
            "title": DRAFT_TITLE_PREFIX + ch.get("kind", "change"),
            "detail": ch.get("note", ""),
            "severity": severity,
            "suggested_field": suggested_field,
            "suggested_value": suggested_value,
        })
    return rows


def save_draft(account_index: int, draft: dict) -> dict:
    """draft 를 **미승인(open)** advice_items 로 저장(중복 title 은 갱신). policy version 미생성.

    저장 후에도 policy.compile_policy 결과는 불변(accepted 만 읽으므로) — 자동 적용 차단 보장.
    """
    if not draft.get("has_draft"):
        return {"ok": True, "account_index": account_index, "saved": 0,
                **markers.PROPOSAL_FLAGS,
                "note": draft.get("note")}

    rows = _draft_advice_rows(draft)
    conn = store_db.connect()
    saved_ids: list[int] = []
    try:
        for r in rows:
            ex = conn.execute(
                "SELECT id, status FROM advice_items WHERE account_index=? AND title=?",
                (account_index, r["title"])).fetchone()
            if ex:
                # 거절되지 않은 기존 draft 만 갱신(거절 이력은 존중 — 반복 강요 금지).
                if ex["status"] == "rejected":
                    continue
                conn.execute(
                    "UPDATE advice_items SET detail=?, severity=?, suggested_field=?, "
                    "suggested_value=?, status='open' WHERE id=?",
                    (r["detail"], r["severity"], r["suggested_field"], r["suggested_value"], ex["id"]))
                saved_ids.append(int(ex["id"]))
            else:
                cur = conn.execute(
                    "INSERT INTO advice_items(account_index, title, detail, source, severity, "
                    "suggested_field, suggested_value, status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (account_index, r["title"], r["detail"], DRAFT_SOURCE, r["severity"],
                     r["suggested_field"], r["suggested_value"], "open", _now()))
                saved_ids.append(int(cur.lastrowid))
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True, "account_index": account_index, "saved": len(saved_ids),
        "advice_ids": saved_ids, "status": "draft",
        **markers.PROPOSAL_FLAGS,
        "note": ("draft 를 미승인(open) 상태로 저장했습니다 — policy version 미생성, 비중 영향 없음. "
                 "사람이 승인(accepted)해야 정책에 반영됩니다."),
    }


def list_drafts(account_index: int) -> list[dict]:
    """이 계좌의 하락징후 draft(미반영 후보) 목록(승인 전 = open)."""
    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT id, title, detail, severity, status, suggested_field, suggested_value, created_at "
            "FROM advice_items WHERE account_index=? AND source=? ORDER BY id DESC",
            (account_index, DRAFT_SOURCE)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# 포트폴리오 영향(portfolio_impact) 조정 후보 → draft. 별도 prefix/source 로 구분.
IMPACT_TITLE_PREFIX = "[영향분석 draft] "
IMPACT_SOURCE = "portfolio_impact"


def build_impact_draft(impact: dict, *, account_index: int) -> dict:
    """portfolio_impact.analyze_account 결과 → policy draft(미승인 후보).

    포트폴리오 차원 조정 후보(현금밴드/위험자산/헤지/리밸런싱 속도/신규매수)를 draft 로 변환.
    **자동 적용 절대 금지** — requires_user_approval, auto_applied:false. 매수/매도 단정 아님.
    """
    cands = (impact or {}).get("portfolio_candidates", [])
    real = [c for c in cands if c.get("kind") != "hold"]
    if not real:
        return {"ok": True, "account_index": account_index, "has_draft": False,
                **markers.PROPOSAL_FLAGS,
                "note": "포트폴리오 차원 조정 후보 없음(관망) — draft 없음."}
    changes = [{"field": c.get("kind"), "kind": c.get("kind"),
                "strength": c.get("strength"), "note": c.get("note")} for c in real]
    summary = impact.get("summary")
    asserted = any(c.get("strength") == "moderate" for c in real)
    return {
        "ok": True, "account_index": account_index, "has_draft": True, "status": "draft",
        **markers.PROPOSAL_FLAGS,
        "asserted": asserted, "source_summary": summary,
        "proposed_changes": changes,
        "flow": "영향분석 → 조정 후보 → draft → (사람 승인) → policy version → allocation 재계산",
        "note": ("포트폴리오 영향 분석 draft 입니다 — 자동 적용 안 됨. 사람 승인 후에만 반영. "
                 "매수/매도·하락 확정 단정이 아니라 운용기준 조정 '후보'입니다."),
    }


def save_impact_draft(account_index: int, impact_draft: dict) -> dict:
    """영향분석 draft 를 미승인(open) advice_items 로 저장(IMPACT_SOURCE). policy version 미생성.

    저장 후에도 compile_policy 결과는 불변(accepted 만 읽음) — 자동 적용 차단 보장.
    """
    if not impact_draft.get("has_draft"):
        return {"ok": True, "account_index": account_index, "saved": 0,
                **markers.PROPOSAL_FLAGS,
                "note": impact_draft.get("note")}
    severity = "important" if impact_draft.get("asserted") else "suggest"
    conn = store_db.connect()
    saved_ids: list[int] = []
    try:
        for ch in impact_draft.get("proposed_changes", []):
            title = IMPACT_TITLE_PREFIX + (ch.get("kind") or "change")
            detail = ch.get("note", "")
            ex = conn.execute(
                "SELECT id, status FROM advice_items WHERE account_index=? AND title=?",
                (account_index, title)).fetchone()
            if ex:
                if ex["status"] == "rejected":
                    continue  # 거절 이력 존중 — 반복 강요 금지
                conn.execute(
                    "UPDATE advice_items SET detail=?, severity=?, status='open' WHERE id=?",
                    (detail, severity, ex["id"]))
                saved_ids.append(int(ex["id"]))
            else:
                cur = conn.execute(
                    "INSERT INTO advice_items(account_index, title, detail, source, severity, "
                    "status, created_at) VALUES(?,?,?,?,?,?,?)",
                    (account_index, title, detail, IMPACT_SOURCE, severity, "open", _now()))
                saved_ids.append(int(cur.lastrowid))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "account_index": account_index, "saved": len(saved_ids),
            "advice_ids": saved_ids, "status": "draft",
            **markers.PROPOSAL_FLAGS,
            "note": ("영향분석 draft 를 미승인(open) 으로 저장 — policy version 미생성, 비중 영향 없음. "
                     "사람이 승인(accepted)해야 정책에 반영됩니다.")}


def generate_impact_draft_and_save(account_index: int) -> dict:
    """portfolio_impact 분석 → 조정 후보 draft 생성·저장(미승인). 자동 적용 0."""
    from . import portfolio_impact as impact_mod
    impact = impact_mod.analyze_account(account_index)
    draft = build_impact_draft(impact, account_index=account_index)
    saved = save_impact_draft(account_index, draft)
    return {"ok": True, "account_index": account_index,
            "impact_summary": impact.get("summary"), "draft": draft, "saved": saved,
            "auto_order_created": False, **markers.PROPOSAL_FLAGS}


def generate_and_save(account_index: int) -> dict:
    """계좌 유니버스 스캔 → 보수적 전환 제안 → draft 생성·저장(미승인). 자동 적용 0."""
    scan = scan_mod.scan_account_universe(account_index)
    draft = build_draft(scan.get("proposal"), account_index=account_index,
                        summary=scan.get("summary"))
    saved = save_draft(account_index, draft)
    return {
        "ok": True, "account_index": account_index,
        "scan_summary": scan.get("summary"),
        "draft": draft, "saved": saved,
        "auto_order_created": False, **markers.PROPOSAL_FLAGS,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--list", action="store_true", help="저장된 draft 목록")
    args = ap.parse_args()
    try:
        if args.list:
            out = {"ok": True, "drafts": list_drafts(args.account)}
        else:
            out = generate_and_save(args.account)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
