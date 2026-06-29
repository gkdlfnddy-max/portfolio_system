"""관심 분야 AI 후보 제안 (중전제 확장) — **자동 투자 추천 아님**.

CEO 원칙(불변):
  사용자가 입력한 관심 분야(예: 로봇·바이오·양자컴퓨터·반도체)를 기준으로
  **인접/보완/분산/헤지/관찰** 후보를 제안한다. 후보는 *neutral* 이며 절대 자동으로
  policy/allocation/주문에 들어가지 않는다.

  흐름:  제안(suggested) → 사용자 선택 → 조사(added_to_research) → 방향 분류 →
         임시 반영(applied_to_draft) → 저장(saved_to_policy) → allocation 재계산.

지능 = 규칙 + 인접맵 + evidence + memory 뿐. **Anthropic / LLM API 미사용 (CLAUDE.md §17).**

핵심 안전 규칙:
  - 후보 direction 기본값은 **unknown_direction** (자동 long 금지 — CEO 지시).
  - 이미 관심에 있는 테마는 제외.
  - 반복적으로 무시된(user_action=ignored 다수) 후보는 confidence 하향 + 후순위.
  - "현재 시장 기준" 후보는 evidence.recall_evidence(freshness) 로만 근거 표시
    (오래되면 stale, 입장 엇갈리면 conflicting) — 근거 없는 강한 추천 금지.
  - 모든 suggest/record 는 growth.middleware.run_task('theme_suggestion','theme-agent') 경유
    (prehook 과거 무시이력·memory 조회, posthook 제안/무시 기록).

  python -m main_mission.portfolio_os.theme_suggestions --account 1 --suggest
  python -m main_mission.portfolio_os.theme_suggestions --account 1 --record --candidate-id 3 --user-action ignored
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .store import db as store_db
from . import profile as profile_mod
from . import evidence as evidence_mod
from . import personalization as personalization_mod
from .profile import THEME_KEYWORDS
from .growth import middleware as growth_mw

AGENT = "theme-agent"
TASK_TYPE = "theme_suggestion"

VALID_TYPES = {"adjacent", "complement", "diversify", "hedge", "watch"}
VALID_ROLES = {"core", "growth_tilt", "hedge", "defensive", "watch"}
VALID_ACTIONS = {"suggested", "added_to_research", "ignored",
                 "applied_to_draft", "saved_to_policy", "rejected"}

# 후보 direction 기본값 — neutral. 자동 long 절대 금지 (CEO 불변 원칙).
DEFAULT_DIRECTION = "unknown_direction"

# 반복 무시 억제 파라미터: ignored 1건당 confidence 를 곱으로 감쇠, 임계 이상이면 후순위.
_IGNORE_DECAY = 0.6      # ignored 횟수당 곱(0.6^n)
_IGNORE_FLOOR = 0.08     # 감쇠 하한
_HEAVY_IGNORE = 2        # 이 횟수 이상 무시되면 "반복 무시"로 후순위


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_theme(h: str) -> str:
    h = (h or "").strip()
    for label, kws in THEME_KEYWORDS.items():
        if h in label or any(h in k or k in h for k in kws):
            return label
    return h


# ============================================================
# 분류 맵 (규칙 + 인접맵 — API 미사용)
# ============================================================

# adjacent(인접): 입력 테마와 가치사슬·기술적으로 인접·보완.
# 테마 도메인 데이터(인접/보완/분산/헤지/관찰/성격) — **단일 원본 config/portfolio/themes.json**.
# 코드 하드코딩 금지(CEO). 추가·수정은 설정 파일만 고친다. (튜닝 상수 _IGNORE_*/_TYPE_ROLE 만 코드.)
from . import configs as _cfg
_themes = _cfg.load("themes")
_ADJACENCY: dict = _themes["adjacency"]               # {테마:[[후보,사유],...]}
_COMPLEMENT_HIGHVOL = _themes["complement_highvol"]    # [[후보,slug,사유],...]
_COMPLEMENT_GROWTH = _themes["complement_growth"]
_DIVERSIFIERS = _themes["diversifiers"]                # [[후보,사유],...]
_HEDGES = _themes["hedges"]
_WATCH: dict = _themes["watch"]

# 후보 type → 기본 suggested_role (neutral — 자동 적용 아님).
_TYPE_ROLE = {
    "adjacent": "growth_tilt",
    "complement": "defensive",
    "diversify": "core",
    "hedge": "hedge",
    "watch": "watch",
}

# 변동성 성격 — 보완 분기 판단용.
_HIGHVOL_THEMES = set(_themes["highvol_themes"])   # 설정 파일
_GROWTH_THEMES = set(_themes["growth_themes"])


# ============================================================
# evidence / freshness 표시
# ============================================================

def _evidence_for(theme: str, account_index: int) -> dict:
    """테마에 대한 현재 시장 근거 요약 — recall_evidence(freshness decay).

    반환: {evidence_ids, freshness_label(fresh|stale|conflicting|none), source, top_eff_conf}
    근거 없으면 none — 근거 없는 강한 추천 금지(confidence 보정에 사용).
    """
    try:
        evs = evidence_mod.recall_evidence(theme=theme, account_index=account_index, limit=5)
    except Exception:
        evs = []
    if not evs:
        return {"evidence_ids": [], "freshness_label": "none", "source": None, "top_eff_conf": 0.0}
    ids = [e["id"] for e in evs]
    stances = {e.get("stance") for e in evs}
    top = evs[0]
    # 입장이 엇갈리면 conflicting.
    pos = stances & {"long_support"}
    neg = stances & {"short_support", "risk_warning"}
    if pos and neg:
        label = "conflicting"
    else:
        age = top.get("age_days")
        label = "stale" if (age is not None and age > evidence_mod.HALF_LIFE_DAYS) else "fresh"
    return {"evidence_ids": ids, "freshness_label": label,
            "source": top.get("source_title") or top.get("source_type"),
            "top_eff_conf": top.get("eff_confidence", 0.0)}


def _ignore_history(account_index: int, conn) -> dict[str, int]:
    """candidate_theme 별 과거 무시 횟수(user_action=ignored). 반복 무시 억제용."""
    rows = conn.execute(
        "SELECT candidate_theme, COUNT(*) AS n FROM theme_suggestion_candidates "
        "WHERE account_index=? AND user_action='ignored' GROUP BY candidate_theme",
        (account_index,),
    ).fetchall()
    return {_norm_theme(r["candidate_theme"]): int(r["n"]) for r in rows}


# ============================================================
# 후보 생성 (순수 함수 — DB 기록은 suggest() 가)
# ============================================================

def _build_candidates(account_index: int, interests: list[str], *, posture: str | None,
                      ignore_hist: dict[str, int], conn) -> list[dict]:
    have = {_norm_theme(t) for t in interests}
    out: list[dict] = []
    seen: set[str] = set()

    def add(candidate_theme: str, ctype: str, *, source_theme: str, reason: str,
            relationship: str, base_conf: float):
        norm = _norm_theme(candidate_theme)
        # 이미 관심에 있는 것 제외.
        if norm in have:
            return
        key = norm
        if key in seen:
            return
        seen.add(key)
        ev = _evidence_for(candidate_theme, account_index)
        # 근거 없으면 강한 추천 금지 → confidence 상한.
        conf = base_conf
        if ev["freshness_label"] == "none":
            conf = min(conf, 0.45)
        elif ev["freshness_label"] == "stale":
            conf = min(conf, 0.5)
        elif ev["freshness_label"] == "conflicting":
            conf = min(conf, 0.4)
        # 반복 무시 억제 — confidence 감쇠 + 후순위 플래그.
        n_ign = ignore_hist.get(norm, 0)
        if n_ign:
            conf = max(_IGNORE_FLOOR, conf * (_IGNORE_DECAY ** n_ign))
        out.append({
            "source_theme": source_theme,
            "candidate_theme": candidate_theme,
            "candidate_type": ctype,
            "reason": reason,
            "relationship": relationship,
            "suggested_role": _TYPE_ROLE.get(ctype, "watch"),
            "direction": DEFAULT_DIRECTION,           # neutral — 자동 long 금지
            "confidence": round(conf, 3),
            "freshness_at": _now(),
            "evidence_ids": ev["evidence_ids"],
            "evidence_freshness": ev["freshness_label"],
            "evidence_source": ev["source"],
            "ignored_count": n_ign,
            "deprioritized": n_ign >= _HEAVY_IGNORE,
        })

    # 1) adjacent (인접)
    for t in interests:
        norm = _norm_theme(t)
        for cand, why in _ADJACENCY.get(norm, []) or _ADJACENCY.get(norm.lower(), []):
            add(cand, "adjacent", source_theme=t, reason=why,
                relationship=f"{t}와(과) 인접·보완", base_conf=0.62)

    # 2) complement (보완) — 포트폴리오 성격에 따라.
    highvol = [t for t in interests if _norm_theme(t) in _HIGHVOL_THEMES]
    growthy = [t for t in interests if _norm_theme(t) in _GROWTH_THEMES]
    if highvol:
        for cand, _slug, why in _COMPLEMENT_HIGHVOL:
            add(cand, "complement", source_theme="고변동 테마 다수",
                reason=why, relationship=f"고변동({', '.join(highvol)}) 완충", base_conf=0.55)
    if growthy:
        for cand, _slug, why in _COMPLEMENT_GROWTH:
            add(cand, "complement", source_theme="성장 테마 다수",
                reason=why, relationship=f"성장({', '.join(growthy)}) 균형", base_conf=0.52)

    # 3) diversify (분산)
    for cand, why in _DIVERSIFIERS:
        add(cand, "diversify", source_theme="전체", reason=why,
            relationship="관심분야와 낮은 상관(분산)", base_conf=0.5)

    # 4) hedge (헤지) — 과열 우려 시. neutral 후보(자동 숏 아님).
    overheated = (posture and any(k in posture for k in ("과열", "고점", "버블", "거품", "고평가")))
    hedge_conf = 0.5 if overheated else 0.38
    for cand, why in _HEDGES:
        add(cand, "hedge", source_theme="전체(과열 대비)" if overheated else "전체",
            reason=why + ("" if not overheated else " — 입력 견해에 과열 신호"),
            relationship="시장 과열 대비 헤지 후보(검토용)", base_conf=hedge_conf)

    # 5) watch (관찰)
    for t in interests:
        norm = _norm_theme(t)
        for cand, why in _WATCH.get(norm, []) or _WATCH.get(norm.lower(), []):
            add(cand, "watch", source_theme=t, reason=why,
                relationship=f"{t} 관련 — 검증 전 관찰", base_conf=0.45)

    # 반복 무시 후보는 후순위(deprioritized=True 를 뒤로), 그 안에서 confidence 내림차순.
    out.sort(key=lambda c: (c["deprioritized"], -c["confidence"]))
    return out


# ============================================================
# suggest / record_action — middleware 경유
# ============================================================

def _apply_personalization(account_index: int, cands: list[dict]) -> list[dict]:
    """계좌별 개인화 가중을 후보 confidence 에 곱해 표시순서만 재정렬.

    - scope='candidate_type'(adjacent/hedge/...) 와 scope='theme'(candidate_theme) 두 축의
      계좌 가중을 곱한다 → 반복 무시한 유형/테마는 하향, 선호는 상향.
    - **자동 long/policy/주문 아님** — confidence 원본 보존, personalized_score/weight 만 부가.
    - deprioritized(반복무시) 후보는 여전히 뒤로 (1차 정렬키 유지).
    - 계좌 격리: account_index 의 가중치만 사용(타 계좌 미반영).
    """
    type_w = personalization_mod.weights_map(account_index, "candidate_type")
    theme_w = personalization_mod.weights_map(account_index, "theme")
    if not type_w and not theme_w:
        # 개인화 이력이 없으면 기존 정렬 그대로(중립).
        for c in cands:
            c.setdefault("personalization_weight", 1.0)
            c.setdefault("personalized_score", c["confidence"])
        return cands
    for c in cands:
        w = type_w.get(c["candidate_type"], 1.0) * theme_w.get(_norm_theme(c["candidate_theme"]), 1.0)
        c["personalization_weight"] = round(w, 4)
        c["personalized_score"] = round(c["confidence"] * w, 6)
    # deprioritized 는 여전히 후순위, 그 안에서는 개인화 점수 내림차순(동점은 confidence).
    cands.sort(key=lambda c: (c["deprioritized"], -c["personalized_score"], -c["confidence"]))
    return cands


def _suggest_impl(account_index: int, conn) -> dict:
    prof = profile_mod.get(account_index) or {}
    raw = [s.strip() for s in (prof.get("interests_text") or "").replace("/", ",").replace("·", ",").split(",") if s.strip()]
    interests: list[str] = []
    for t in raw:
        if t not in interests:
            interests.append(t)
    posture = ((prof.get("posture_text") or "") + " " + (prof.get("views_text") or "")).strip()

    if not interests:
        return {"ok": True, "account_index": account_index, "interests": [], "candidates": [],
                "note": "관심 분야가 비어 있습니다 — 먼저 관심 테마를 입력하면 인접/보완/분산/헤지/관찰 후보를 제안합니다."}

    ignore_hist = _ignore_history(account_index, conn)
    cands = _build_candidates(account_index, interests, posture=posture or None,
                              ignore_hist=ignore_hist, conn=conn)
    # 개인화 가중(계좌별·표시순서만): 반복 무시한 candidate_type/테마는 하향, 선호는 상향.
    # candidate_type(예: hedge 선호) × theme(예: 반도체 hedge 수용) 가중을 곱해 적용.
    cands = _apply_personalization(account_index, cands)

    # INSERT — user_action='suggested'. applied_to_research_queue/applied_to_policy=0 (자동반영 금지).
    saved: list[dict] = []
    now = _now()
    for c in cands:
        cur = conn.execute(
            "INSERT INTO theme_suggestion_candidates("
            "account_index, source_theme, candidate_theme, candidate_type, reason, relationship, "
            "suggested_role, direction, confidence, freshness_at, evidence_ids, user_action, "
            "applied_to_research_queue, applied_to_policy, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_index, c["source_theme"], c["candidate_theme"], c["candidate_type"],
             c["reason"], c["relationship"], c["suggested_role"], c["direction"],
             c["confidence"], c["freshness_at"], json.dumps(c["evidence_ids"], ensure_ascii=False),
             "suggested", 0, 0, now, now),
        )
        item = dict(c)
        item["id"] = int(cur.lastrowid)
        item["user_action"] = "suggested"
        item["applied_to_policy"] = 0
        item["applied_to_research_queue"] = 0
        saved.append(item)
    conn.commit()

    by_type: dict[str, int] = {}
    for c in saved:
        by_type[c["candidate_type"]] = by_type.get(c["candidate_type"], 0) + 1

    return {"ok": True, "account_index": account_index, "interests": interests,
            "candidates": saved, "count": len(saved), "by_type": by_type,
            "disclaimer": "후보는 neutral(방향 미정)입니다 — 자동으로 policy/allocation/주문에 반영되지 않습니다. "
                          "[조사 후보로 추가] 후 방향을 정하고 저장해야 반영됩니다."}


def suggest(account_index: int | None) -> dict:
    """관심 분야 기반 후보 제안 — middleware(run_task) 경유. 후보는 DB에 user_action='suggested'로 INSERT.

    **자동 투자 추천 아님**: 모든 후보 direction=unknown_direction, applied_to_policy=0.
    """
    if account_index is None:
        return {"ok": False, "error": "account_id 없음 — 후보 제안은 계좌 귀속이 필수입니다(hard-block).",
                "gate": "block"}

    def _fn(_input, ctx):
        conn = store_db.connect()
        try:
            res = _suggest_impl(account_index, conn)
        finally:
            conn.close()
        # posthook 산출물: 제안 개수/유형 기록. 반복 무시 후보는 feedback 으로 남김.
        ignored_cands = [c for c in res.get("candidates", []) if c.get("deprioritized")]
        feedback = [{"kind": "negative", "detail": f"반복 무시 후보 후순위: {c['candidate_theme']}",
                     "account_index": account_index, "agent": AGENT, "scope": "sector",
                     "ref": c["candidate_theme"]} for c in ignored_cands]
        return {
            "result": res,
            "outcome": {"count": res.get("count", 0), "by_type": res.get("by_type", {}),
                        "note": "neutral 후보 제안 — policy/allocation 자동반영 없음"},
            "feedback": feedback,
            "next_action": "사용자가 후보를 [조사 후보로 추가]하면 방향 분류 → 임시반영 → 저장 흐름으로 진행",
        }

    run = growth_mw.run_task(TASK_TYPE, AGENT, _fn, account_index=account_index)
    if not run.get("ok"):
        return {"ok": False, "error": "; ".join(run.get("reasons", [])) or "후보 제안 실패",
                "blocked": run.get("blocked", False), "task_id": run.get("task_id")}
    out = dict(run["result"])
    out["task_id"] = run.get("task_id")
    return out


def _record_impl(candidate_id: int, account_index: int, user_action: str, conn) -> dict:
    row = conn.execute(
        "SELECT * FROM theme_suggestion_candidates WHERE id=? AND account_index=?",
        (candidate_id, account_index),
    ).fetchone()
    if row is None:
        return {"ok": False, "error": f"후보 {candidate_id} 없음(또는 다른 계좌) — 계좌 격리"}

    # applied_to_research_queue 는 added_to_research 일 때만 1. applied_to_policy 는
    # saved_to_policy 일 때만 1 (그 외엔 절대 자동 1 금지 — 후보는 neutral).
    applied_research = 1 if user_action in ("added_to_research", "applied_to_draft", "saved_to_policy") else int(row["applied_to_research_queue"] or 0)
    applied_policy = 1 if user_action == "saved_to_policy" else int(row["applied_to_policy"] or 0)
    conn.execute(
        "UPDATE theme_suggestion_candidates SET user_action=?, applied_to_research_queue=?, "
        "applied_to_policy=?, updated_at=? WHERE id=? AND account_index=?",
        (user_action, applied_research, applied_policy, _now(), candidate_id, account_index),
    )
    conn.commit()

    # 끊긴 고리 연결: [조사 후보로 추가] 시 candidate_theme 을 계좌 관심 분야에 **방향 미정**으로 올린다.
    # → strategy 페이지의 '관심 테마별 정리'에 '방향 미정(미반영)'으로 등장 → 사용자가 방향을 정할 수 있음.
    # **자동 long/policy/주문 반영은 없음** (interests 목록 등재일 뿐, 방향 미지정이면 allocation 미반영).
    interest_added = False
    if user_action == "added_to_research":
        try:
            r = profile_mod.add_interest(account_index, row["candidate_theme"])
            interest_added = bool(r.get("added"))
        except Exception:
            interest_added = False  # 관심 등재 실패는 행동 기록 자체를 막지 않음

    return {"ok": True, "candidate_id": candidate_id, "user_action": user_action,
            "applied_to_research_queue": applied_research, "applied_to_policy": applied_policy,
            "candidate_theme": row["candidate_theme"],
            "candidate_type": row["candidate_type"],
            "added_to_interests": interest_added,
            "direction": DEFAULT_DIRECTION}  # neutral — 자동 long 금지


# user_action → 개인화 피드백 action 매핑. (선택 신호 / 무시 신호 / 수정 신호)
_FEEDBACK_ACTION = {
    "added_to_research": "accepted",   # 조사 후보로 채택 = 선호
    "applied_to_draft": "accepted",
    "saved_to_policy": "accepted",
    "ignored": "ignored",
    "rejected": "ignored",
}


def _record_personalization(account_index: int, res: dict, user_action: str) -> None:
    """후보 행동을 계좌별 개인화 가중에 반영 — candidate_type·theme 두 축(표시순서만).

    **계좌 격리**: account_index 의 가중만 갱신(타 계좌 미반영).
    **공통 lessons 와 분리**: personalization_weights 에만 기록(agent memory 아님).
    **자동 주문/policy 0**: 다음 제안의 *표시 순서* 만 바뀐다.
    """
    fb = _FEEDBACK_ACTION.get(user_action)
    if not fb or not res.get("ok"):
        return
    ctype = res.get("candidate_type")
    theme = res.get("candidate_theme")
    reason = f"theme_suggestion:{user_action}"
    try:
        if ctype:
            personalization_mod.record_feedback(account_index, "candidate_type", ctype, fb, reason=reason)
        if theme:
            personalization_mod.record_feedback(account_index, "theme", _norm_theme(theme), fb, reason=reason)
    except Exception:
        pass  # 개인화 기록 실패가 행동 기록 자체를 막지 않음


def record_action(candidate_id: int, account_index: int | None, user_action: str) -> dict:
    """후보에 대한 사용자 행동 기록 — middleware 경유.

    user_action ∈ suggested|added_to_research|ignored|applied_to_draft|saved_to_policy|rejected.
    **[조사 후보로 추가]는 policy 직접 반영이 아니다** — applied_to_policy 는 saved_to_policy 일 때만 1.
    """
    if account_index is None:
        return {"ok": False, "error": "account_id 없음(hard-block).", "gate": "block"}
    if user_action not in VALID_ACTIONS:
        return {"ok": False, "error": f"잘못된 user_action: {user_action}; 허용 {sorted(VALID_ACTIONS)}"}

    def _fn(_input, ctx):
        conn = store_db.connect()
        try:
            res = _record_impl(candidate_id, account_index, user_action, conn)
        finally:
            conn.close()
        # 통합 개인화 루프(Track A): 선택/무시/수정 → 계좌별 가중 갱신(다음 제안 표시순서).
        _record_personalization(account_index, res, user_action)
        feedback = []
        if user_action == "ignored" and res.get("ok"):
            # 무시는 negative feedback — 다음 제안에서 덜 제안되도록(반복 무시 억제 학습).
            feedback.append({"kind": "negative", "detail": f"후보 무시: {res.get('candidate_theme')}",
                             "account_index": account_index, "agent": AGENT, "scope": "sector",
                             "ref": res.get("candidate_theme")})
        return {"result": res, "outcome": {"user_action": user_action, "candidate_id": candidate_id},
                "feedback": feedback, "success": res.get("ok", False)}

    run = growth_mw.run_task(TASK_TYPE, AGENT, _fn, account_index=account_index)
    if not run.get("ok"):
        return {"ok": False, "error": "; ".join(run.get("reasons", [])) or "행동 기록 실패"}
    return run["result"]


def main() -> int:
    ap = argparse.ArgumentParser(description="관심 분야 AI 후보 제안 — neutral, 자동반영 없음(API 미사용)")
    ap.add_argument("--account", type=int, help="계좌 인덱스 (필수 — 없으면 hard-block)")
    ap.add_argument("--suggest", action="store_true", help="후보 제안")
    ap.add_argument("--record", action="store_true", help="사용자 행동 기록")
    ap.add_argument("--candidate-id", dest="candidate_id", type=int)
    ap.add_argument("--user-action", dest="user_action")
    args = ap.parse_args()

    try:
        if args.record:
            if args.account is None or args.candidate_id is None or not args.user_action:
                out = {"ok": False, "error": "--record 에는 --account --candidate-id --user-action 필요"}
            else:
                out = record_action(args.candidate_id, args.account, args.user_action)
        elif args.account is None:
            out = {"ok": False, "error": "account_id 없음 — 후보 제안은 계좌가 필수입니다(hard-block).",
                   "gate": "block"}
        else:
            out = suggest(args.account)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
