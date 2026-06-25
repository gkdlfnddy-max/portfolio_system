"""관점별 포트폴리오 후보 (A/B/C안) — **하나의 정답 금지**.

CEO 원칙(불변):
  같은 데이터·같은 견해라도 **관점**에 따라 다르게 해석된다. 그래서 각 계좌에
  *정답 1개*가 아니라 **관점별 후보 3안**을 만든다.

    A안 = 사용자 **현재 관점**에 가장 충실한 best (user_views + investor_objective 반영).
    B안 = 조금 더 **방어적** (현금/채권↑·위험↓ — drawdown 보호 강화).
    C안 = 조금 더 **공격적** (테마 tilt·위험↑ — 단, 한도·risk gate 준수).

핵심 규칙(불변 — CLAUDE.md §2, §4):
  - **수익률 최대화 아님.** 목적(investor_objective)에 맞춘 최선이다. 목적이 '손실 축소'면
    drawdown↓ 가 우선 — C안조차 '목적 안에서의 공격'으로 절제된다.
  - **자동 금지.** A/B/C 는 전부 **draft 후보**다. 사람이 한 안을 골라 승인해야만
    policy/비중에 반영된다. auto_order_created=false, requires_user_approval=true.
  - 비중은 base 로직(allocation._variant)을 재사용 — **합계 100·섹터상한·인버스 한도** 동일 보호.
  - **정직.** investor_objective 미설정이면 "목적 미설정 — A안은 견해만 반영,
    목적 입력 시 정교화" 를 표기한다(가짜 목적 만들지 않음).
  - 지능 = 규칙 + Claude+메모리 (Anthropic API 미사용).

읽는 것(DB 읽기 전용 — 타 모듈 본문 의존 X):
  user_views(견해, Agent1) · investor_profile(성향/관심/현금밴드/지역/채권) ·
  investor_objective(목적/성향 — 병렬 A 에이전트가 생성 중; **없으면 graceful**).
  방향성 게이트는 field_advisors.resolve_theme_directions(롱→tilt, 숏/혼재→hedge) 재사용.

저장(draft 만): target_allocations(status='draft', variant=A|B|C).

  python -m main_mission.portfolio_os.perspective_variants --account 1 --generate
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from . import allocation as alloc_mod
from . import policy as policy_mod
from .store import db as store_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _themes(interests_text):
    if not interests_text:
        return []
    parts = re.split(r"[,/·]| 및 |\s{2,}", interests_text)
    return [s.strip() for s in parts if s.strip()][:8]


def _norm(s) -> str:
    return (s or "").strip().lower()


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


# ============================================================
# DB 읽기 (읽기 전용)
# ============================================================
def _load_profile(conn, account_index: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM investor_profile WHERE account_index=?", (account_index,)).fetchone()
    return dict(row) if row else None


def _load_views(conn, account_index: int) -> list[dict]:
    """이 계좌의 active 견해(계좌 격리 — 교차적용 금지)."""
    rows = conn.execute(
        "SELECT id, layer, theme, ticker, etf, stance, conviction, horizon, note "
        "FROM user_views WHERE account_index=? AND status='active' ORDER BY id DESC",
        (account_index,)).fetchall()
    return [dict(r) for r in rows]


def _load_objective(conn, account_index: int) -> dict | None:
    """투자 목적/성향 — `investor_objective.get()`(user_views layer='objective' 저장)로 읽는다.
    미설정/실패 시 None(graceful '목적 미설정'). A↔B 통합 배선 — 전용 테이블 아님."""
    try:
        from . import investor_objective as io
        return io.get(account_index)
    except Exception:  # noqa: BLE001 — 미설정/모듈 부재는 '목적 미설정'으로 정직 처리
        return None


# ============================================================
# 목적 해석 — 수익률 최대화가 아니라 '목적에 맞춘 최선'
# ============================================================
# investor_objective 의 컬럼명이 미확정이라 여러 후보 키를 너그럽게 본다(graceful).
_OBJECTIVE_KEYS = ("investment_goal", "objective", "objective_type", "goal", "goal_type",
                   "primary_goal", "kind")
_OBJECTIVE_TEXT_KEYS = ("objective_text", "goal_text", "description", "note", "summary")

# 목적 → 최적화 축(무엇을 우선하는가). '수익률 최대화'는 의도적으로 *기본값 아님*.
# investor_objective(A 에이전트) 의 investment_goal 8종 + 호환 별칭을 함께 매핑.
_GOAL_PROFILE = {
    # A 에이전트(investor_objective) 정식 값 8종
    "loss_reduction": {"label": "손실 축소", "optimize": "drawdown_min", "lean": "defensive"},
    "cash_preservation": {"label": "원금/현금 보존", "optimize": "drawdown_min", "lean": "defensive"},
    "volatility_reduction": {"label": "변동성 축소", "optimize": "stability", "lean": "defensive"},
    "dividend": {"label": "배당/수입", "optimize": "stability", "lean": "defensive"},
    "thesis_hold": {"label": "thesis 유지", "optimize": "balanced", "lean": "neutral"},
    "stable_operation": {"label": "안정 운용", "optimize": "stability", "lean": "neutral"},
    "growth": {"label": "장기 성장", "optimize": "growth", "lean": "aggressive"},
    "aggressive_growth": {"label": "공격적 성장", "optimize": "growth", "lean": "aggressive"},
    # 호환 별칭(다른 표기)
    "capital_preservation": {"label": "원금/손실 축소", "optimize": "drawdown_min", "lean": "defensive"},
    "loss_minimization": {"label": "손실 축소", "optimize": "drawdown_min", "lean": "defensive"},
    "income": {"label": "꾸준한 수입/배당", "optimize": "stability", "lean": "defensive"},
    "balanced": {"label": "균형(위험/수익)", "optimize": "balanced", "lean": "neutral"},
}


def _interpret_objective(objective: dict | None) -> dict:
    """investor_objective → 최적화 축. 미설정이면 정직하게 'unset' 표기.

    반환: {set: bool, label, optimize, lean, text, note}
      set=False  → 목적 미설정. A안은 견해만 반영(정교화 불가) 라고 알린다.
    """
    if not objective:
        return {"set": False, "label": None, "optimize": None, "lean": None,
                "text": None,
                "note": "목적 미설정 — A안은 견해(user_views)만 반영. "
                        "목적(investor_objective) 입력 시 더 정교화됩니다."}
    raw = None
    for k in _OBJECTIVE_KEYS:
        if objective.get(k):
            raw = _norm(objective[k])
            break
    text = None
    for k in _OBJECTIVE_TEXT_KEYS:
        if objective.get(k):
            text = str(objective[k]).strip()
            break
    prof = _GOAL_PROFILE.get(raw or "")
    if prof is None:
        # 목적 행은 있으나 분류 불가 → 균형으로 안전 폴백(정직 표기).
        return {"set": True, "label": (raw or "사용자 정의 목적"), "optimize": "balanced",
                "lean": "neutral", "text": text,
                "note": "목적이 표준 분류에 없어 균형(balanced)로 해석했습니다(정직)."}
    return {"set": True, "label": prof["label"], "optimize": prof["optimize"],
            "lean": prof["lean"], "text": text,
            "note": f"목적='{prof['label']}' → '{prof['optimize']}' 우선으로 해석."}


# ============================================================
# 견해 → 관점 요약 (대전제/중전제/소전제 흐름의 입력)
# ============================================================
_STANCE_SIGN = {"positive": 1, "negative": -1, "neutral": 0, "observe": 0}


def _views_summary(views: list[dict]) -> dict:
    """견해 묶음 → 관점 요약(롱 성향/숏 성향/혼재). 단정 아님 — 관점 프레임용."""
    long_pos = [v for v in views if _STANCE_SIGN.get(_norm(v.get("stance"))) == 1]
    neg = [v for v in views if _STANCE_SIGN.get(_norm(v.get("stance"))) == -1]
    observe = [v for v in views if _norm(v.get("stance")) == "observe"]
    convs = [float(v["conviction"]) for v in views if v.get("conviction") is not None]
    return {
        "count": len(views),
        "positive": len(long_pos),
        "negative": len(neg),
        "observe": len(observe),
        "avg_conviction": round(sum(convs) / len(convs), 3) if convs else None,
        "themes": sorted({v["theme"] for v in views if v.get("theme")}),
    }


# ============================================================
# 관점별 현금밴드 매핑 — A=현재, B=방어(현금↑), C=공격(현금↓)
# ============================================================
def _perspective_cash(perspective: str, band: dict, lean: str | None) -> float:
    """관점별 현금 수준 (방어 총량). base 검증(_variant)이 0~100 클램프·합계 보호.

    A = 목적/성향이 가리키는 현재 관점(lean 반영).
    B = 그보다 방어적(상한 쪽으로). C = 그보다 공격적(하한 쪽으로).
    목적이 '손실축소'면 A 자체가 이미 방어적이라 B 는 상한, C 도 과하게 공격하지 않는다."""
    cmin = band.get("min") if band.get("min") is not None else 10.0
    cmax = band.get("max") if band.get("max") is not None else 40.0
    cmid = band.get("target") if band.get("target") is not None else round((cmin + cmax) / 2, 1)
    # A: lean(목적/성향)에 따라 현재 관점 현금 — 방어=상한쪽, 공격=하한쪽, 중립=중간.
    a = {"defensive": round((cmid + cmax) / 2, 1),
         "aggressive": round((cmin + cmid) / 2, 1)}.get(lean or "", cmid)
    if perspective == "A":
        return a
    if perspective == "B":          # 더 방어적 — A 와 상한 사이
        return round((a + cmax) / 2, 1)
    # C 더 공격적 — A 와 하한 사이 (목적 한도 안에서)
    return round((a + cmin) / 2, 1)


# ============================================================
# 관점 프레이밍 — 각 안의 요약/이유/장점/위험/트리거/추가확인
# ============================================================
def _macro_view() -> dict:
    """거시(ECOS/FRED) → 포트폴리오 해석(후보). 미연동이면 connected=False(정직)."""
    try:
        from . import macro_connect as mc
        return mc.macro_to_portfolio()
    except Exception as e:  # noqa: BLE001 — 거시 실패가 관점 생성을 막지 않게(정직)
        return {"connected": False, "signals": [], "lean": None,
                "note": f"거시 미연동(조회 실패) — {e}"}


def _macro_note(perspective: str, macro: dict) -> str:
    """관점별 거시 해석 한 줄. 거시 우선(CEO) — 데이터 없으면 '거시 미연동' 정직 표기."""
    if not macro or not macro.get("connected"):
        return ("거시 미연동 — ECOS/FRED 키 설정 후 거시(금리/역전/달러/유가/VIX)가 "
                "이 안에 우선 반영됩니다(현재는 견해/성향만).")
    lean = macro.get("lean")
    tops = "; ".join(s["detail"] for s in macro.get("signals", [])[:3]) or "거시 특이신호 없음"
    base = f"거시 기울기={lean} (방어점수 {macro.get('defensive_score')}). 주요: {tops}."
    if perspective == "B" and lean == "defensive":
        return base + " 거시가 방어를 지지 — B안(방어)과 정합."
    if perspective == "C" and lean == "defensive":
        return base + " 거시는 방어를 가리킴 — C안(공격)은 거시와 역방향이라 절제·분할 권고."
    if perspective == "C" and lean == "aggressive":
        return base + " 거시도 위험선호 — C안(공격)과 정합(단 한도/risk gate 준수)."
    if perspective == "A":
        return base + " A안(현재 관점)은 거시를 참고신호로 본다(자동 반영 아님 — 사람 승인)."
    return base


def _frame(perspective: str, obj: dict, vsum: dict, weights: dict,
           themes: list[str], hedge_list: list[str], macro: dict | None = None) -> dict:
    """A/B/C 한 안의 서술 프레임. 같은 데이터를 관점에 맞게 *다르게 해석*한다."""
    risk_pct = weights["risk_assets"]
    cash_pct = weights["cash"]
    bond_pct = weights["bond"]
    optimize = obj.get("optimize")
    goal_label = obj.get("label") or "목적 미설정"

    if perspective == "A":
        summary = "현재 관점 best — 사용자 견해/성향에 가장 충실한 기준안."
        fit = ("사용자의 active 견해(" + (", ".join(vsum["themes"]) or "테마 미입력")
               + ") 와 성향을 그대로 반영했습니다."
               + ("" if obj.get("set") else " (목적 미설정 — 견해만 반영, 목적 입력 시 정교화)"))
        pros = ["사용자 관점과 가장 정합 — 납득/실행 용이.",
                "한도·합계 검증 통과(현 운용기준 유지)."]
        risks = ["관점이 한쪽으로 치우쳐 있으면 그 편향이 그대로 남는다.",
                 "시장이 견해와 반대로 가면 B/C 로의 전환 검토 필요."]
        triggers = ["견해(user_views)가 바뀌면 이 안은 재생성되어야 함.",
                    "목적(investor_objective)이 새로 설정/변경되면 재프레이밍."]
    elif perspective == "B":
        summary = "조금 더 방어적 — 현금/채권↑·위험자산↓ (drawdown 보호 강화)."
        fit = ("같은 견해를 더 보수적으로 해석합니다. 현금 " + f"{cash_pct}% / 채권 {bond_pct}% "
               + "로 방어를 늘려 하락 충격을 줄입니다.")
        pros = ["하락장 방어력↑ — 손실 폭(drawdown) 축소.",
                "현금 여력↑ — 무릎 지점 분할매수 탄약 확보."]
        risks = ["상승장에서 상대적 기회비용(덜 먹음).",
                 "방어가 과하면 목적(성장)일 때 미달 가능."]
        triggers = ["하락 신호 해소·변동성 정상화 시 A 로 복귀 검토.",
                    "현금이 과도해 목적 대비 비효율이면 재조정."]
    else:  # C
        summary = "조금 더 공격적 — 테마 tilt·위험자산↑ (단, 한도·risk gate 준수)."
        fit = ("같은 견해를 더 적극적으로 해석합니다. 위험자산 " + f"{risk_pct}% 로 테마("
               + (", ".join(themes) or "없음") + ")에 더 싣되, 섹터/인버스/레버리지 "
               + "한도와 risk gate 안에서만 움직입니다.")
        pros = ["상승 추세에서 수익 탄력↑(견해가 맞을 때).",
                "관심 테마 노출↑ — 구조적 성장 포착."]
        risks = ["하락 시 변동성/손실 폭↑.",
                 "한 테마 쏠림 위험 — 섹터 상한으로 제한되나 모니터링 필요.",
                 "**시장가 금지** — 진입은 지정가(예측 진입)로만."]
        triggers = ["하락 6축 신호 강화 시 B 로 후퇴 검토.",
                    "테마 노출이 한도에 닿으면 추가 tilt 중단."]

    # 목적이 '손실 축소'면 C 라도 절제 — 정직 표기.
    if optimize == "drawdown_min" and perspective == "C":
        fit += " (목적='손실 축소'이므로 공격은 목적 한도 안에서 절제됩니다 — 수익률 최대화 아님.)"
    if optimize == "growth" and perspective == "B":
        fit += " (목적='성장'이라 B 는 일시적 방어 — 추세 회복 시 A/C 복귀를 전제로 합니다.)"

    confirm = [
        "관심 테마별 방향성(롱/숏/관망) 재확인 — field_advisors.",
        "보유·관심 종목의 하락 6축 신호 — decline_scan.",
        "관련 자료(공시/뉴스/거시) 최신성 — evidence_summary.",
    ]
    if not obj.get("set"):
        confirm.insert(0, "투자 목적(investor_objective) 입력 — 안의 정교화에 필요.")

    return {
        "perspective": perspective,
        "label": {"A": "현재 관점 best", "B": "방어적", "C": "공격적"}[perspective],
        "objective": goal_label,
        "objective_optimize": optimize,
        "summary": summary,
        "why_fits_user": fit,
        "macro_reading": _macro_note(perspective, macro or {}),   # 거시 우선 해석(후보·정직)
        "macro_connected": bool((macro or {}).get("connected")),
        "weights": weights,
        "themes_long": themes,
        "hedge": hedge_list,
        "pros": pros,
        "risks": risks,
        "break_triggers": triggers,        # 언제 이 안이 깨지는가
        "more_to_confirm": confirm,        # 추가 확인할 자료
        "requires_user_approval": True,
        "auto_applied": False,
        "auto_order_created": False,
    }


def _weights_from_rows(rows: list[dict]) -> dict:
    """target rows → 버킷 요약(현금/채권/위험자산/테마/헤지). 합계 100 검증된 rows."""
    cash = bond = anchor = tilt = hedge = 0.0
    for r in rows:
        w = float(r["weight_pct"])
        k = r["kind"]
        if k == "cash":
            cash += w
        elif k == "bond":
            bond += w
        elif k == "anchor":
            anchor += w
        elif k == "tilt":
            tilt += w
        elif k == "hedge":
            hedge += w
    risk_assets = round(anchor + tilt, 1)   # 위험자산(앵커+테마, 헤지 제외)
    return {
        "cash": round(cash, 1),
        "bond": round(bond, 1),
        "defensive": round(cash + bond, 1),     # 방어 총량(순현금+국채) — 관점 방어강도 비교용
        "risk_assets": risk_assets,
        "anchor": round(anchor, 1),
        "theme_tilt": round(tilt, 1),
        "hedge": round(hedge, 1),
        "total": round(cash + bond + anchor + tilt + hedge, 1),
    }


# ============================================================
# 메인 — A/B/C 후보 생성 + draft 저장
# ============================================================
def generate(account_index: int, *, save_draft: bool = True) -> dict:
    """관점별 후보 3안(A/B/C) 생성. 전부 draft — 사람 선택·승인 전 미반영.

    - A=현재 관점 best, B=방어적, C=공격적. 같은 견해/데이터를 관점에 맞게 다르게 해석.
    - 목적(investor_objective) 있으면 최적화 축 반영, 없으면 정직하게 '미설정' 표기.
    - 비중은 allocation._variant(검증된 base 로직)로 생성 — 합계100·한도 동일 보호.
    """
    if int(account_index) < 1:
        return {"ok": False, "error": "account_index 는 1 이상이어야 합니다"}

    pol = policy_mod.latest(account_index)
    policy = pol["policy"] if pol else policy_mod.compile_policy(account_index)
    band = policy.get("cash_band", {})
    limits = policy.get("limits", {})
    sector_max = limits.get("sector_max_pct", 30.0)
    inverse_max = limits.get("inverse_max_pct", 10.0)

    conn = store_db.connect()
    try:
        prof = _load_profile(conn, account_index)
        views = _load_views(conn, account_index)
        objective = _load_objective(conn, account_index)
    finally:
        conn.close()

    obj = _interpret_objective(objective)
    vsum = _views_summary(views)
    macro = _macro_view()       # 거시 우선 — A/B/C 해석에 거시 기울기 반영(데이터 없으면 정직 미연동)

    # 테마/방향 — allocation 과 동일 게이트 재사용(롱→tilt, 숏/혼재→hedge). 자동 long 금지.
    all_themes = _themes(prof.get("interests_text") if prof else None)
    from .field_advisors import resolve_theme_directions
    dirs = resolve_theme_directions(account_index, all_themes)
    long_themes = [t for t in all_themes if dirs.get(t) in ("long_candidate", "mixed_swing")]
    dir_hedge = [t for t in all_themes if dirs.get(t) in ("short_or_hedge_candidate", "mixed_swing")]
    hedge_col = _themes(prof.get("hedge_themes") if prof else None)
    hedge_list = list(dict.fromkeys(hedge_col + dir_hedge))

    region_targets: dict = {}
    if prof and prof.get("region_targets"):
        try:
            region_targets = json.loads(prof["region_targets"]) or {}
        except (ValueError, TypeError):
            region_targets = {}
    bond_pct = float(prof["bond_target_pct"]) if (prof and prof.get("bond_target_pct") is not None) else 0.0
    duration = (prof.get("bond_duration_pref") if prof else None) or None

    proposal_id = f"persp-{account_index}-{_now()}"
    candidates: list[dict] = []
    for p in ("A", "B", "C"):
        cash_pct = _perspective_cash(p, band, obj.get("lean"))
        rows = alloc_mod.variant_for_perspective(
            account_index, p, cash_pct=cash_pct, sector_max=sector_max,
            inverse_max=inverse_max, long_themes=long_themes, hedge_list=hedge_list,
            bond_pct=bond_pct, region_targets=region_targets, duration=duration)
        weights = _weights_from_rows(rows)
        framed = _frame(p, obj, vsum, weights, long_themes, hedge_list, macro)
        framed["rows"] = rows
        candidates.append(framed)

    saved = 0
    if save_draft:
        conn = store_db.connect()
        try:
            for c in candidates:
                for r in c["rows"]:
                    conn.execute(
                        "INSERT INTO target_allocations(account_index, proposal_id, variant, "
                        "kind, ref, weight_pct, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (account_index, proposal_id, c["perspective"], r["kind"], r["ref"],
                         r["weight_pct"], "draft", _now()))
                    saved += 1
            conn.commit()
        finally:
            conn.close()

    return {
        "ok": True,
        "account_index": account_index,
        "proposal_id": proposal_id,
        "objective": obj,                       # set 여부 + 최적화 축 (정직)
        "macro": macro,                         # 거시→포트폴리오 해석(후보) 또는 미연동(정직)
        "views_summary": vsum,
        "themes_long": long_themes,
        "hedge_themes": hedge_list,
        "candidates": candidates,               # A/B/C
        "draft_rows_saved": saved,
        "requires_user_approval": True,
        "auto_applied": False,
        "auto_order_created": False,
        "note": ("관점별 후보 3안(A=현재 관점·B=방어·C=공격)입니다. 하나의 정답이 아니라 "
                 "관점에 따른 해석입니다. 전부 draft 이며 사람이 한 안을 골라 승인해야 "
                 "policy/비중에 반영됩니다. 수익률 최대화가 아니라 목적에 맞춘 최선입니다."),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--no-save", action="store_true", help="draft 저장 없이 후보만 출력")
    args = ap.parse_args()
    try:
        if args.generate:
            out = generate(args.account, save_draft=not args.no_save)
        else:
            out = {"ok": False, "error": "--generate"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
