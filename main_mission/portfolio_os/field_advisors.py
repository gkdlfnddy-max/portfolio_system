"""필드별 전문 조언(FIELD-LEVEL AI advisors) — 중전제 입력 필드마다 전문가 1명.

각 advisor 는 텍스트를 다듬고, 정책 변수를 추출하고, 위험을 짚고, '그대로 적용' 제안을
만든다. **단, AI 조언은 절대 정책을 직접 바꾸지 않는다.** 사용자가 저장하기 전까지는 임시
제안일 뿐이다(field_consultations 기록 → 사용자 행동 field_advice_events 기록).

지능 = 규칙 + 프로젝트 메모리뿐. **Anthropic / LLM API 미사용 (CLAUDE.md §17).**

계좌 정책 우선(§7): 모든 메모리 제안은 effective_policy 로 만든 policy 를 통해
resolve_conflicts() 를 거친다 — 금지 테마/한도 초과 제안은 드롭/주석되지 절대 정책을
덮어쓰지 않는다.

  python -m main_mission.portfolio_os.field_advisors --account 1 --field interests --text "로봇, 바이오, 양자"
  python -m main_mission.portfolio_os.field_advisors --account 1 --field whole --interests "..." --views "..."
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from .store import db as store_db
from . import regionbond
from . import policy_rules
from . import profile as profile_mod
from . import lessons as lessons_mod
from .profile import THEME_KEYWORDS, _SECT
from .growth import memory as memory_mod
from .growth import prehooks, posthooks


# field_name → (agent_name, prehook task_type)
FIELD_AGENTS: dict[str, str] = {
    "interests": "theme-field-advisor",
    "views": "opinion-field-advisor",
    "region": "region-field-advisor",
    "defensive": "defensive-field-advisor",
    "pace": "pace-field-advisor",
    "whole": "whole-field-advisor",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# 공통 헬퍼
# ============================================================

def _norm_theme(h: str) -> str:
    h = (h or "").strip()
    for label, kws in THEME_KEYWORDS.items():
        if h in label or any(h in k or k in h for k in kws):
            return label
    return h


def _policy_for_conflict(eff: dict) -> dict:
    """effective_policy → resolve_conflicts() 가 이해하는 policy dict 로 변환.

    - cash_band: cash_min/max → {min, max}
    - forbidden_assets: allow_themes 가 False 면 'themes' 포함(테마 tilt 억제 트리거).
    """
    flags = (eff or {}).get("flags", {})
    limits = (eff or {}).get("limits", {})
    cash_band = {}
    if limits.get("cash_min_pct") is not None:
        cash_band["min"] = limits["cash_min_pct"]
    if limits.get("cash_max_pct") is not None:
        cash_band["max"] = limits["cash_max_pct"]
    forbidden: list[str] = []
    if flags.get("allow_themes") is False:
        forbidden.append("themes")
    return {"cash_band": cash_band, "forbidden_assets": forbidden,
            "themes_allowed": flags.get("allow_themes", True)}


def _recall_themes(agent: str, account_index: int, themes: list[str]) -> tuple[list[dict], list[dict]]:
    """테마별 scoped 메모리 recall + lessons → (sources, raw_items). 정책충돌은 호출측에서 처리."""
    sources: list[dict] = []
    raw_items: list[dict] = []
    seen = set()
    for th in themes:
        norm = _norm_theme(th)
        for m in memory_mod.recall_scoped(agent, account_index, theme=norm, sector=norm, limit_per=3):
            key = ("mem", m.get("id"), norm)
            if key in seen:
                continue
            seen.add(key)
            m = dict(m)
            m.setdefault("theme", norm)
            raw_items.append(m)
        for ln in lessons_mod.search(scope="sector", ref=norm, limit=2):
            key = ("lesson", ln["id"])
            if key in seen:
                continue
            seen.add(key)
            sources.append({"kind": "lesson", "id": ln["id"], "theme": norm,
                            "title": ln["title"], "note": ln["body"]})
    return sources, raw_items


def _struct(field_name: str, agent_name: str, advice_type: str, *, original_text: str,
            suggested_text: str, extracted_variables: dict, risk_warnings: list,
            missing_points: list, follow_up: list, sources: list, confidence: float) -> dict:
    return {
        "field_name": field_name,
        "agent_name": agent_name,
        "advice_type": advice_type,
        "original_text": original_text or "",
        "suggested_text": suggested_text or "",
        "extracted_variables": extracted_variables or {},
        "risk_warnings": risk_warnings or [],
        "missing_points": missing_points or [],
        "follow_up": follow_up or [],
        "sources": sources or [],
        "confidence": round(float(confidence), 3),
    }


# ============================================================
# 1) theme_advisor — 관심 분야 / 섹터 / 테마
# ============================================================

THEME_CLASS = {
    "양자컴퓨터": "고변동성", "바이오": "고변동성", "로봇": "장기성장", "AI": "장기성장",
    "반도체": "경기민감", "2차전지": "경기민감", "우주항공": "장기성장", "방산": "방어",
    "에너지": "경기민감",
}


# 관심 테마는 **neutral input**(분석 대상). 방향(롱/숏헤지/관망/제외/미정)은 견해에서 별도 추출.
# 절대 기본값으로 long 을 넣지 않는다 (CEO 지시).
_DIR_HINTS = {
    "avoid_or_exclude": ["안 사", "안사", "투자 안", "투자안", "제외", "피하", "하지 않", "안 할", "빼고싶", "안 담"],
    "short_or_hedge_candidate": ["고점", "과열", "버블", "숏", "short", "인버스", "inverse", "헤지", "hedge",
                                  "줄이", "하락", "떨어", "빠질", "고평가", "비싸", "거품", "공매"],
    "long_candidate": ["장기 성장", "장기성장", "장기적", "장기로", "장기 보유", "유망", "성장성", "좋게 본",
                        "사고싶", "사고 싶", "사는", "사도", "매수", "담고", "담을",
                        "분할 매수", "분할매수", "편입", "늘리", "모으", "비중 늘", "조금 사"],
    "watch_only": ["지켜", "관망", "관심만", "아직 모", "모르겠", "자료조사", "조사 필요", "보류", "지켜보"],
}
_DIR_LABEL = {
    "long_candidate": "롱 후보", "short_or_hedge_candidate": "숏/헤지 후보",
    "mixed_swing": "롱숏 혼재(스윙)",
    "watch_only": "관망", "avoid_or_exclude": "제외", "unknown_direction": "방향 미정",
}
# allocation 역할: 롱→tilt, 숏→hedge, 혼재→swing(코어 롱 + 전술 인버스 페어).
_DIR_ALLOC = {"long_candidate": "growth_tilt", "short_or_hedge_candidate": "hedge", "mixed_swing": "swing"}


def classify_direction(theme: str, views_text: str) -> dict:
    """견해 텍스트에서 테마의 방향 + 확신도 추출. 롱·숏 신호가 **동시**면 mixed_swing(혼재→스윙).
    미언급/불명확이면 unknown_direction (long 기본값 금지)."""
    t = views_text or ""
    aliases = [theme] + ([theme[:2]] if len(theme) >= 3 else [])
    sentences = [s for s in re.split(r"[.!?\n。,]+|\s+그리고\s+|하고\s+싶고|싶고|(?<=고)\s+", t) if s.strip()]
    relevant = [s.strip() for s in sentences if any(a in s for a in aliases)]
    if not relevant:
        return {"direction": "unknown_direction", "confidence": 0.2, "evidence_quote": None, "needs_clarification": True}
    joined = " ".join(relevant)
    quote = relevant[0]
    if any(h in joined for h in _DIR_HINTS["avoid_or_exclude"]):
        return {"direction": "avoid_or_exclude", "confidence": 0.6, "evidence_quote": quote, "needs_clarification": False}
    has_long = any(h in joined for h in _DIR_HINTS["long_candidate"])
    has_short = any(h in joined for h in _DIR_HINTS["short_or_hedge_candidate"])
    # 롱·숏 신호 공존 → 혼재(스윙 기회). 예: "장기는 롱이되 단기 과열엔 인버스".
    if has_long and has_short:
        return {"direction": "mixed_swing", "confidence": 0.7, "evidence_quote": quote, "needs_clarification": False}
    if has_short:
        return {"direction": "short_or_hedge_candidate", "confidence": 0.78, "evidence_quote": quote, "needs_clarification": False}
    if has_long:
        return {"direction": "long_candidate", "confidence": 0.78, "evidence_quote": quote, "needs_clarification": False}
    if any(h in joined for h in _DIR_HINTS["watch_only"]):
        return {"direction": "watch_only", "confidence": 0.6, "evidence_quote": quote, "needs_clarification": False}
    return {"direction": "unknown_direction", "confidence": 0.3, "evidence_quote": quote, "needs_clarification": True}


_VALID_DIR = {"long_candidate", "short_or_hedge_candidate", "mixed_swing", "watch_only", "avoid_or_exclude", "unknown_direction"}


def resolve_theme_directions(account_index: int, themes: list[str]) -> dict:
    """테마별 방향: 사용자 override(investor_profile.doc.theme_directions) > 견해 추출 > unknown.
    allocation 이 이걸로 게이트(롱만 tilt, 숏/헤지는 hedge, watch/unknown/avoid 미반영). 자동 long 금지."""
    import json as _json
    prof = profile_mod.get(account_index) or {}
    views = prof.get("views_text") or ""
    overrides: dict = {}
    try:
        doc = _json.loads(prof.get("doc") or "{}")
        overrides = doc.get("theme_directions") or {}
    except (ValueError, TypeError):
        overrides = {}
    out = {}
    for t in themes:
        ov = overrides.get(t)
        out[t] = ov if ov in _VALID_DIR else classify_direction(t, views)["direction"]
    return out


_THEME_ADJACENCY = {
    "로봇": ["AI 인프라", "산업 자동화", "2차전지"],
    "바이오": ["헬스케어", "제약", "의료기기"],
    "양자컴퓨터": ["AI 인프라", "사이버보안", "반도체 장비"],
    "반도체": ["AI 인프라", "반도체 장비", "메모리"],
    "2차전지": ["전기차", "신재생에너지", "소재"],
    "ai": ["AI 인프라", "데이터센터", "반도체"],
}
_DIVERSIFIERS = [
    ("방산", "지정학 리스크 헤지 + 정부 예산 모멘텀(성장과 낮은 상관)"),
    ("원자력·에너지", "AI 데이터센터 전력 수요 급증 + 에너지 안보"),
    ("금·원자재", "인플레·통화 약세 방어(주식과 낮은 상관)"),
    ("배당·리츠", "현금흐름 + 변동성 완충(방어적 위성)"),
    ("우주·항공", "장기 성장 테마 분산"),
]


def _recommend_additional_themes(existing: list[str], limit: int = 6) -> list[dict]:
    """관심 테마 **추가 추천**(중전제) — 인접/보완 + 분산. 규칙+인접맵 기반(API 미사용),
    시황·메모리는 카드 note 로 보강. 다양한 시야 제안이되 강요 아님(사용자가 +추가 선택)."""
    have = {_norm_theme(e) for e in existing}
    out: list[dict] = []
    seen: set = set()
    for e in existing:
        for adj in _THEME_ADJACENCY.get(_norm_theme(e), []):
            if _norm_theme(adj) not in have and adj not in seen:
                out.append({"theme": adj, "reason": f"{e}와(과) 인접·보완 테마", "kind": "adjacent"})
                seen.add(adj)
    for th, reason in _DIVERSIFIERS:
        if _norm_theme(th) not in have and th not in seen:
            out.append({"theme": th, "reason": reason, "kind": "diversify"})
            seen.add(th)
    return out[:limit]


def theme_advisor(account_index: int, text: str, advice_type: str = "improve") -> dict:
    agent = FIELD_AGENTS["interests"]
    eff = policy_rules.effective_policy(account_index)
    sector_max = (eff.get("limits", {}) or {}).get("sector_max_pct", 30.0)
    policy = _policy_for_conflict(eff)
    themes_allowed = policy["themes_allowed"]

    raw = [t.strip() for t in re.split(r"[,/·\n]+", text or "") if t.strip()]
    norm = []
    for t in raw:
        n = _norm_theme(t)
        if n not in norm:
            norm.append(n)

    # 인버스/헤지 의도(롱 tilt 와 분리).
    hedge = [h.strip() for h in (profile_mod.hedge_themes(text or "") or "").split(",") if h.strip()]
    # 방향성은 **견해(views) 텍스트**에서 추출 — 관심 테마는 neutral, 자동 롱 금지.
    _prof = profile_mod.get(account_index) or {}
    views_text = _prof.get("views_text") or ""

    classified = []
    for n in norm:
        if n in hedge:
            direction, conf_d, quote, needs = "short_or_hedge_candidate", 0.7, "hedge_themes 명시", False
        else:
            d = classify_direction(n, views_text)
            direction, conf_d, quote, needs = d["direction"], d["confidence"], d["evidence_quote"], d["needs_clarification"]
        # role: 롱 tilt 는 long_candidate 만, hedge 는 short_or_hedge 만. 그 외(관망/제외/미정)는 allocation 미반영.
        role = ("long" if direction == "long_candidate"
                else "hedge" if direction == "short_or_hedge_candidate"
                else "swing" if direction == "mixed_swing" else None)
        classified.append({"theme": n, "label": n, "class": THEME_CLASS.get(n, "관망"),
                           "direction": direction, "direction_label": _DIR_LABEL.get(direction, direction),
                           "role": role, "confidence": conf_d, "evidence_quote": quote,
                           "allocation_role": _DIR_ALLOC.get(direction),
                           "is_long_candidate": direction == "long_candidate",
                           "is_hedge_candidate": direction == "short_or_hedge_candidate",
                           "needs_clarification": needs})

    risk_warnings: list = []
    missing_points: list = []
    follow_up: list = []

    # 과집중: 테마 수가 많아 tilt 가 섹터 한도를 넘기 쉬움.
    n_long = sum(1 for c in classified if c["role"] == "long")
    if n_long >= 4:
        risk_warnings.append(
            f"롱 테마 {n_long}개 — 한 섹터에 쏠리면 섹터 한도 {sector_max}%를 쉽게 넘깁니다. "
            f"테마당 tilt 상한을 두고 분산하세요(과집중 경고).")
    # 중복(같은 정규화 테마가 원문에 여러 번).
    dup = [n for n in set(norm) if [_norm_theme(t) for t in raw].count(n) > 1]
    if dup:
        risk_warnings.append(f"테마 중복 감지: {', '.join(dup)} — 같은 노출이 겹쳐 분산 효과가 줄어듭니다.")
    highvol = [c["theme"] for c in classified if c["class"] == "고변동성"]
    if highvol:
        risk_warnings.append(f"고변동성 테마({', '.join(highvol)})는 ETF 코어로 묶어 개별 바이너리 리스크를 낮추세요.")

    if not norm:
        missing_points.append("관심 테마가 비어있습니다 — 한두 개라도 적어야 목표비중 tilt 를 만들 수 있습니다.")
    if n_long and not any(c["role"] == "hedge" for c in classified):
        follow_up.append("과열 섹터에 대한 헤지(인버스) 의도가 있나요? 있으면 롱과 분리해 적어주세요.")
    follow_up.append("각 테마를 어느 정도(%)까지 담고 싶은지 생각해두면 tilt 상한을 정하기 좋습니다.")

    # 테마당 tilt cap 제안 — 섹터 한도를 롱 테마 수로 균등 분배(과집중 방지).
    tilt_cap = round(sector_max / max(1, n_long), 1) if n_long else None
    per_theme_cap = {c["theme"]: tilt_cap for c in classified if c["role"] == "long"} if tilt_cap else {}

    # 메모리 recall + 정책 우선 충돌 해소.
    raw_sources, raw_items = _recall_themes(agent, account_index, [c["theme"] for c in classified if c["role"] == "long"])
    kept, conflicts = memory_mod.resolve_conflicts(raw_items, policy)
    sources = list(raw_sources)
    for m in kept:
        sources.append({"kind": f"memory:{m.get('scope_type', 'agent')}", "id": m.get("id"),
                        "theme": m.get("theme"), "title": m.get("title"),
                        "note": m.get("body"), "source_label": m.get("source_label")})
    suppressed = []
    for c in conflicts:
        mem = c.get("memory", {})
        suppressed.append(mem.get("theme") or mem.get("title"))
    if suppressed and not themes_allowed:
        risk_warnings.append(
            "계좌 정책이 테마 tilt 를 불허합니다 — 메모리의 테마 비중확대 제안은 "
            f"적용되지 않습니다(억제됨: {', '.join(str(s) for s in suppressed if s)}). 계좌 정책 우선.")

    # 방향 미정 테마는 단정하지 말고 보완 질문(자동 롱 금지).
    unknown = [c["theme"] for c in classified if c["direction"] == "unknown_direction"]
    if unknown:
        missing_points.append(
            f"방향 미정 테마: {', '.join(unknown)} — 관심만으로는 매수 대상이 아닙니다. "
            "롱 후보 / 숏·헤지 후보 / 관망 중 무엇인지 정해야 목표비중에 반영됩니다.")
        follow_up.append(f"{unknown[0]} 은(는) 롱으로 모으고 싶은가요, 과열이라 숏/헤지로 보나요, 아직 관망인가요?")

    # 개선안 = **깨끗한 테마 목록**(쉼표 구분). 적용 시 interests_text 에 그대로 들어가도 오염되지 않게.
    # 성격(class)/방향(direction)/tilt cap 은 카드(classified)·extracted 로만 표시하고 텍스트엔 안 섞는다.
    suggested = ", ".join(norm) if norm else (text or "")

    extracted = {
        "themes": norm,
        "classified": classified,
        "hedge_themes": hedge,
        "sector_max_pct": sector_max,
        "per_theme_tilt_cap_pct": per_theme_cap,
        "themes_allowed": themes_allowed,
        "suggested_additions": _recommend_additional_themes(norm),  # 추가 관심 분야 추천(중전제)
    }
    conf = 0.55 + min(0.3, 0.05 * len(norm))
    return _struct("interests", agent, advice_type, original_text=text, suggested_text=suggested,
                   extracted_variables=extracted, risk_warnings=risk_warnings,
                   missing_points=missing_points, follow_up=follow_up, sources=sources, confidence=conf)


# ============================================================
# 2) opinion_advisor — 내 생각 / 견해
# ============================================================

def _recommend_views_text(account_index: int, current: str = "") -> str:
    """성향·지역·관심테마(방향)·방어/채권·속도를 종합해 현명한 견해 초안(한글). 규칙 기반(API 미사용)."""
    prof = profile_mod.get(account_index) or {}
    rk = {"aggressive": "공격적", "neutral": "중립", "defensive": "방어적"}
    pace_k = {"slow": "천천히 분할", "normal": "보통 속도로 분할", "fast": "빠르게 분할"}
    dur_k = {"short": "단기", "intermediate": "중기", "long": "장기", "mixed": "사다리(분산만기)"}
    seg: list[str] = []

    rt = prof.get("risk_tolerance")
    region = (prof.get("region_pref") or "").strip()
    region_phrase = region if region else "미국·한국 등 핵심 지역"
    base = f"{region_phrase} 중심의 글로벌 코어 ETF를 축으로 삼고"
    if rt:
        base = f"전반 성향은 {rk.get(rt, rt)}으로, " + base
    seg.append(base)

    # 관심 테마 — 방향별로 분기 (자동 롱 금지)
    themes = [s.strip() for s in re.split(r"[,/·]| 및 |\s{2,}", prof.get("interests_text") or "") if s.strip()][:8]
    if themes:
        dirs = resolve_theme_directions(account_index, themes)
        longs = [t for t in themes if dirs.get(t) == "long_candidate"]
        hedges = [t for t in themes if dirs.get(t) == "short_or_hedge_candidate"]
        undecided = [t for t in themes if dirs.get(t) in ("watch_only", "unknown_direction", None)]
        if longs:
            seg.append(f"{', '.join(longs)}는 롱으로 분할 편입(무릎 지점 지정가, 일·주 단위로 나눠서)")
        if hedges:
            seg.append(f"{', '.join(hedges)}는 숏/헤지로 인버스 소액(롱과 분리)")
        if undecided:
            seg.append(f"{', '.join(undecided)}는 방향이 정해질 때까지 관망(목표비중 미반영)")

    # 방어자산 — 현금밴드 + 채권(방어 대비 비율) + 듀레이션
    cmin, cmax = prof.get("cash_min_pct"), prof.get("cash_max_pct")
    bond, dur = prof.get("bond_target_pct"), prof.get("bond_duration_pref")
    dphrase = "방어자산"
    if cmin is not None and cmax is not None:
        dphrase = f"방어자산은 현금밴드 {int(cmin)}~{int(cmax)}%"
    if bond is not None:
        dseg = f"{dphrase}에서 그중 국채 {int(float(bond))}%"
        if dur:
            dseg += f"({dur_k.get(dur, dur)})로 금리·경기 대응"
        seg.append(dseg)
    else:
        seg.append(f"{dphrase}로 변동성 대응(국채 비중은 금리 전망 보고 결정)")

    pace = prof.get("rebalance_pace")
    if pace:
        seg.append(f"진입/조정은 {pace_k.get(pace, pace)}")

    seg.append("한 번에 몰지 않고 예측 진입(시장가 금지), 과신은 피하고 하락 대비 현금 여력 유지")
    draft = ". ".join(seg) + "."
    if current.strip():
        draft = current.strip() + "\n\n⟶ 종합 추천 초안: " + draft
    return draft


def opinion_advisor(account_index: int, text: str, advice_type: str = "improve") -> dict:
    agent = FIELD_AGENTS["views"]
    t = text or ""

    d = profile_mod.distill(t)
    sug = d.get("suggested", {})

    extracted: dict = {}
    if sug.get("risk_tolerance"):
        extracted["risk_tolerance"] = sug["risk_tolerance"]
    if sug.get("short_policy"):
        extracted["short_policy"] = sug["short_policy"]
    if sug.get("cash_min_pct") is not None or sug.get("cash_max_pct") is not None:
        extracted["cash_band"] = {"min": sug.get("cash_min_pct"), "max": sug.get("cash_max_pct")}
    if sug.get("horizon"):
        extracted["horizon"] = sug["horizon"]
    if sug.get("rebalance_pace"):
        extracted["pace"] = sug["rebalance_pace"]

    # 빠진 변수.
    missing_points: list = []
    if "risk_tolerance" not in extracted:
        missing_points.append("투자 성향(공격/중립/방어)이 글에서 드러나지 않습니다.")
    if "cash_band" not in extracted:
        missing_points.append("현금 밴드(평소 유지 현금 범위)가 없습니다 — 변동성 대응 기준이 모호해집니다.")
    if "horizon" not in extracted:
        missing_points.append("투자 기간/목적이 빠졌습니다 — 단기/장기에 따라 종목·헤지 판단이 달라집니다.")
    if "pace" not in extracted:
        missing_points.append("조정 속도(분할 빈도)가 없습니다 — 진입을 며칠/주 단위로 나눌지 정해두면 좋습니다.")
    if "short_policy" not in extracted:
        missing_points.append("숏(인버스) 허용 수준이 명시되지 않았습니다.")

    # 모순 / 편향 경고.
    risk_warnings: list = []
    if re.search(r"공격|적극|레버리지", t) and re.search(r"방어|보수|안전|지키", t):
        risk_warnings.append("공격적 의도와 방어적 의도가 함께 보입니다 — 우선순위를 명확히 해야 정책이 일관됩니다.")
    cb = extracted.get("cash_band") or {}
    if cb.get("min") is not None and cb.get("max") is not None and cb["min"] > cb["max"]:
        risk_warnings.append(f"현금 하한({int(cb['min'])}%)이 상한({int(cb['max'])}%)보다 큽니다 — 값이 뒤바뀐 듯합니다.")
    if re.search(r"무조건|반드시|확실|틀림없|100\s*%|올인", t):
        risk_warnings.append("과신/확정 표현이 보입니다 — 예측이 빗나갈 때를 대비한 헤지·현금 여력을 함께 두세요(과신 편향).")
    if re.search(r"몰빵|올인|한\s*종목|다\s*넣", t):
        risk_warnings.append("집중 베팅 신호 — 단일 종목/섹터 한도와 분할 진입으로 리스크를 나누세요.")

    follow_up: list = []
    if "cash_band" not in extracted:
        follow_up.append("평소 유지할 현금 범위는 몇 % ~ 몇 % 인가요?")
    if "risk_tolerance" not in extracted:
        follow_up.append("전반적으로 공격적/중립/방어적 중 어디에 가깝나요?")
    follow_up.append("하락장 방어 기준(손절·현금 확대 트리거)이 있나요?")

    # 메모리 (premise/decision scope) — 정책 우선 충돌 해소.
    eff = policy_rules.effective_policy(account_index)
    policy = _policy_for_conflict(eff)
    raw_items = memory_mod.recall_scoped(agent, account_index, limit_per=4)
    kept, _conflicts = memory_mod.resolve_conflicts(raw_items, policy)
    sources = [{"kind": f"memory:{m.get('scope_type', 'agent')}", "id": m.get("id"),
                "title": m.get("title"), "note": m.get("body"),
                "source_label": m.get("source_label")} for m in kept]
    for ln in lessons_mod.search(scope="premise", limit=2):
        sources.append({"kind": "lesson", "id": ln["id"], "title": ln["title"], "note": ln["body"]})

    # 개선 문장(명료화).
    bits = []
    rk = {"aggressive": "공격적", "neutral": "중립", "defensive": "방어적"}
    if extracted.get("risk_tolerance"):
        bits.append(f"성향: {rk.get(extracted['risk_tolerance'], extracted['risk_tolerance'])}")
    if cb:
        bits.append(f"현금 {cb.get('min')}~{cb.get('max')}%")
    if extracted.get("horizon"):
        bits.append(f"기간 {extracted['horizon']}")
    if extracted.get("pace"):
        bits.append(f"조정 {extracted['pace']}")
    suggested = (t.strip() + ("  ⟶ 정리: " + " · ".join(bits) if bits else "")).strip()
    if advice_type == "find_gaps":
        suggested = t  # 갭 찾기는 원문 보존, missing_points 가 핵심
    elif advice_type == "extract":
        suggested = " · ".join(bits) if bits else t
    elif advice_type == "recommend":
        # 종합 추천: 위에 적은 성향·지역·관심테마(방향)·방어/채권·속도를 묶어 현명한 견해 초안.
        suggested = _recommend_views_text(account_index, t)
        follow_up = ["이 초안을 바탕으로 본인 언어로 다듬어 저장하세요(저장 전엔 정책 불변).",
                     "방향 미정 테마는 위 테마 카드에서 롱/숏·헤지/관망을 직접 정하면 목표비중에 반영됩니다."]

    conf = 0.5 + min(0.35, 0.07 * len(extracted))
    return _struct("views", agent, advice_type, original_text=text, suggested_text=suggested,
                   extracted_variables=extracted, risk_warnings=risk_warnings,
                   missing_points=missing_points, follow_up=follow_up, sources=sources, confidence=conf)


# ============================================================
# 3) region_advisor — 지역 비중
# ============================================================

def region_advisor(account_index: int, text: str, advice_type: str = "improve") -> dict:
    agent = FIELD_AGENTS["region"]
    parsed = regionbond.parse_region(text or "")
    targets = parsed["targets"]
    total = parsed["total"]

    eff = policy_rules.effective_policy(account_index)
    limits = eff.get("limits", {}) or {}
    max_country = limits.get("max_single_country_pct", 70.0)
    emerging_max = limits.get("emerging_market_max_pct", 20.0)

    violations = regionbond.validate(targets, None, None,
                                     max_single_country=max_country, emerging_max=emerging_max)
    risk_warnings = list(parsed["warnings"]) + [v["detail"] for v in violations]

    missing_points: list = []
    follow_up: list = []
    if not targets:
        missing_points.append("지역 비중이 숫자로 없습니다 — 예: '미국 50 / 한국 40 / 기타 10' 처럼 적어주세요.")
        follow_up.append("미국/한국/기타 비중을 숫자로 정해주시겠어요? 합계 100% 권장.")
    elif total != 100:
        follow_up.append(f"현재 합계가 {total}% 입니다. 100%가 되도록 조정할까요?")

    # 집중 경고.
    for reg, w in targets.items():
        if w >= 60:
            risk_warnings.append(f"{reg} {w}% — 한 국가/통화에 쏠려 환율·국가 리스크가 큽니다. 분산을 검토하세요.")

    suggested_pairs = " / ".join(f"{k} {v}" for k, v in targets.items()) if targets else (text or "")
    extracted = {
        "region_policy": {"targets": targets, "total": total},
        "sum_is_100": total == 100 if targets else None,
    }

    sources = [{"kind": "engine", "id": "regionbond.parse_region",
                "note": f"파싱된 지역 {len(targets)}개, 합계 {total}%"}]
    conf = 0.6 if targets and total == 100 else (0.45 if targets else 0.3)
    return _struct("region", agent, advice_type, original_text=text, suggested_text=suggested_pairs,
                   extracted_variables=extracted, risk_warnings=risk_warnings,
                   missing_points=missing_points, follow_up=follow_up, sources=sources, confidence=conf)


# ============================================================
# 4) defensive_advisor — 현금 vs 채권 (방어자산 bucket)
# ============================================================

_DURATION_MAP = {
    "short": "short", "단기": "short", "단기채": "short",
    "intermediate": "intermediate", "중기": "intermediate", "중기채": "intermediate",
    "long": "long", "장기": "long", "장기채": "long",
    "ladder": "ladder", "사다리": "ladder",
    "mixed": "mixed", "혼합": "mixed", "장단기": "mixed",
}


def normalize_duration(d) -> str | None:
    """듀레이션 정규화 → short|intermediate|long|ladder|mixed. 미인식은 None."""
    if not d:
        return None
    return _DURATION_MAP.get(str(d).strip().lower(), None)


def validate_bond_ratio(ratio) -> dict:
    """국채 비율(방어자산 대비, 0~100) 검증. 방어자산을 100% 초과할 수 없다."""
    r = round(float(ratio or 0), 1)
    errors: list = []
    if r < 0:
        errors.append("국채 비율이 음수입니다.")
    if r > 100:
        errors.append(f"국채 비율 {r}% > 100% — 국채는 방어자산을 100% 초과할 수 없습니다(방어 안에서만 배분).")
    return {"ok": not errors, "errors": errors, "bond_ratio_pct": r}


def validate_defensive(pure_cash_pct, bond_pct, risk_asset_pct=None) -> dict:
    """방어자산 숫자 검증. **방어 = 순현금 + 채권**, 위험자산 = 100 - 방어.

    hard error: 합계≠100 / 채권>방어 / 순현금<0 / 위험<0. (현금+채권+위험=110 같은 별도합산 차단)
    """
    pc = round(float(pure_cash_pct or 0), 1)
    b = round(float(bond_pct or 0), 1)
    defensive = round(pc + b, 1)
    risk = round(100.0 - defensive, 1) if risk_asset_pct is None else round(float(risk_asset_pct), 1)
    total = round(pc + b + risk, 1)
    errors: list = []
    if total != 100.0:
        errors.append(f"순현금({pc}) + 채권({b}) + 위험자산({risk}) = {total}% — 100%가 아닙니다(방어에 채권을 무조건 더하지 마세요).")
    if b > defensive:
        errors.append(f"채권/국채 {b}% > 방어자산 {defensive}% — 채권은 방어 bucket 안에서 배분되어야 합니다.")
    if pc < 0:
        errors.append("순현금이 음수입니다.")
    if risk < 0:
        errors.append("위험자산이 음수입니다.")
    return {"ok": not errors, "errors": errors,
            "defensive_bucket_pct": defensive, "pure_cash_pct": pc, "bond_pct": b, "risk_asset_pct": risk}


def defensive_options(cash_min, cash_max, bond_pct, duration, pace=None) -> list[dict]:
    """**채권 비율 중심** 3안 — 현금밴드(방어자산 총량)는 사용자가 위에서 정한 값으로 **고정**하고,
    3안은 *방어자산 중 국채 비율(ratio, 0~100)* + *듀레이션*만 달리한다(현금/위험 비중은 안 건드림).
    절대 국채%(est_bond_pct)는 현 방어밴드(평균) 기준 추정 표시용일 뿐, 적용되는 값은 ratio."""
    cmn = float(cash_min) if cash_min is not None else 10.0
    cmx = float(cash_max) if cash_max is not None else 40.0
    if cmn > cmx:
        cmn, cmx = cmx, cmn
    defensive_ref = round((cmn + cmx) / 2, 1)               # 방어밴드 평균(절대 추정 표시용)
    base_ratio = float(bond_pct) if bond_pct is not None else 25.0
    base_ratio = max(0.0, min(100.0, base_ratio))
    dur = normalize_duration(duration) or "short"

    def mk(option, ratio, dur_, reason):
        ratio = round(max(0.0, min(100.0, ratio)), 1)
        est_bond = round(defensive_ref * ratio / 100.0, 1)  # 현 방어밴드 기준 국채 절대% 추정
        return {"option": option,
                "bond_ratio_pct": ratio,                    # ← 적용되는 값(방어자산 대비 %)
                "bond_duration_preference": dur_,
                "reason": reason,
                "defensive_ref_pct": defensive_ref,         # 현 방어밴드 평균(표시용)
                "est_bond_pct": est_bond,                   # 추정 절대 국채%(표시용)
                # 하위호환(절대값, 방어밴드 평균 기준) — 검증/구표시용
                "defensive_bucket_pct": defensive_ref,
                "pure_cash_pct": round(defensive_ref - est_bond, 1),
                "bond_pct": est_bond,
                "risk_asset_pct": round(100.0 - defensive_ref, 1),
                "rebalance_pace": pace or "normal"}

    return [
        mk("conservative", min(100.0, base_ratio + 15), "mixed",
           "방어 강화 — 국채 비중↑ + 사다리(만기분산)로 금리 변동 완충"),
        mk("base", base_ratio, dur,
           "권장 — 입력한 국채 비율 유지(금리 불확실 시 단기 중심)"),
        mk("aggressive", max(0.0, base_ratio - 15), "short",
           "현금 여력 우선 — 국채 비중↓ + 단기(매수 dry powder 확보)"),
    ]


def defensive_advisor(account_index: int, text: str, advice_type: str = "improve") -> dict:
    agent = FIELD_AGENTS["defensive"]
    bond = regionbond.parse_bond(text or "")
    bond_pct = bond["bond_target_pct"]
    duration = bond["duration_pref"]

    eff = policy_rules.effective_policy(account_index)
    limits = eff.get("limits", {}) or {}
    cash_min = limits.get("cash_min_pct")
    cash_max = limits.get("cash_max_pct")

    risk_warnings: list = []
    missing_points: list = []
    follow_up: list = []

    # 핵심 설명: bond_pct = **방어자산 중 국채 비율(0~100)**. 방어 = 순현금 + 국채.
    # 절대 국채%는 방어 크기에 비례(방어×비율). 현금밴드 위에 더하는 게 아니다.
    defensive_explainer = (
        "채권 비중 = 방어자산 중 국채 비율(%). 현금밴드(방어 총량) 안에서 순현금과 나눠 갖고, "
        "위험자산엔 영향 없음. 예) 방어 40% · 국채 25% → 국채 10% + 순현금 30%.")
    # bond_pct 는 방어자산 대비 비율(ratio). 절대 국채%는 방어(현금 상한 기준)×비율로 산출.
    bond_frac = max(0.0, min(1.0, float(bond_pct) / 100.0)) if bond_pct is not None else None
    net_cash = None
    if bond_pct is not None and cash_max is not None:
        bond_abs = round(float(cash_max) * bond_frac, 1)            # 방어 상한 기준 절대 국채%
        net_cash = round(float(cash_max) - bond_abs, 1)            # 순현금(방어 - 국채)
        ratio_v = validate_bond_ratio(bond_pct)
        for err in ratio_v["errors"]:
            if err not in risk_warnings:
                risk_warnings.append(err)

    # 듀레이션 / 금리 환경.
    if duration == "long":
        risk_warnings.append("장기채는 금리 상승 시 가격 하락폭이 큽니다(듀레이션 리스크) — 금리 불확실 구간엔 비중을 제한하세요.")
    risk_warnings.extend(bond["notes"])
    if bond_pct is not None and duration is None:
        missing_points.append("채권 비중은 있지만 듀레이션(단기/중기/장기/혼합) 선호가 없습니다.")
        follow_up.append("채권 듀레이션은 단기(캐시 대용)·장기(금리하락 베팅)·혼합(사다리) 중 무엇이 좋을까요?")
    if bond_pct is None:
        missing_points.append("채권 비율이 숫자로 없습니다 — 방어자산의 몇 %를 국채로 할지 정해주세요(0~100).")
    if duration in (None, "long"):
        follow_up.append("2026년 금리 불확실 — 단기채 중심 + 만기 분산(bond ladder)을 고려할까요?")

    bond_abs = round(float(cash_max) * bond_frac, 1) if (bond_frac is not None and cash_max is not None) else None
    suggested = defensive_explainer
    if bond_pct is not None:
        suggested += f" 입력 기준 국채 = 방어자산의 {bond_pct}%"
        if bond_abs is not None and net_cash is not None:
            suggested += f" (방어 상한 {cash_max}% 기준 국채 약 {bond_abs}%, 순현금 약 {net_cash}%)"
        suggested += "."

    # 숫자형 결론 — 보수/기준/공격 3안 + 권장(기준)안. (설명으로 끝내지 않음 — CEO 지시)
    options = defensive_options(cash_min, cash_max, bond_pct, duration, eff.get("flags", {}).get("pace"))
    recommendation = options[1]  # 기준안
    # 입력값(있으면) 검증: validate_defensive 는 절대값(순현금/국채)을 받는다 → bond_abs 사용.
    input_validation = None
    if bond_abs is not None and net_cash is not None:
        input_validation = validate_defensive(net_cash, bond_abs)
        for err in input_validation["errors"]:
            if err not in risk_warnings:
                risk_warnings.append(err)
    extracted = {
        "bond_policy": {"bond_target_pct": bond_pct, "bond_ratio_of_defensive_pct": bond_pct,
                        "bond_abs_pct": bond_abs, "duration_pref": normalize_duration(duration)},
        "cash_policy": {"cash_min_pct": cash_min, "cash_max_pct": cash_max, "net_cash_pct": net_cash},
        "defensive_model": "defensive = net_cash + bond; bond_target_pct = ratio of defensive (0-100)",
        "recommendation": recommendation,   # 숫자 결론(기준안)
        "options": options,                  # 보수/기준/공격 3안
        "input_validation": input_validation,
    }
    sources = [{"kind": "engine", "id": "regionbond.parse_bond",
                "note": f"채권 {bond_pct}%, 듀레이션 {duration}"}]
    for ln in lessons_mod.search(scope="economy", limit=1):
        sources.append({"kind": "lesson", "id": ln["id"], "title": ln["title"], "note": ln["body"]})
    conf = 0.6 if bond_pct is not None else 0.4
    return _struct("defensive", agent, advice_type, original_text=text, suggested_text=suggested,
                   extracted_variables=extracted, risk_warnings=risk_warnings,
                   missing_points=missing_points, follow_up=follow_up, sources=sources, confidence=conf)


# ============================================================
# 5) pace_advisor — 조정 속도 / 분할 진입
# ============================================================

PACE_PLAN = {
    "slow": {"days_per_round": "7~14일", "rounds": "4~5", "note": "천천히 분할 — 무릎 지점에 지정가로 나눠 진입."},
    "normal": {"days_per_round": "3~7일", "rounds": "3~4", "note": "보통 속도 — 며칠 단위 분할."},
    "fast": {"days_per_round": "1~3일", "rounds": "2~3", "note": "빠른 조정 — 단, 과회전·추격매수 주의."},
}


def pace_advisor(account_index: int, text: str, advice_type: str = "improve") -> dict:
    agent = FIELD_AGENTS["pace"]
    t = text or ""
    pace = None
    if re.search(r"빠르|자주|단타|공격적으로\s*회전|빈번", t) and not re.search(r"빠르.{0,6}(아니|않|안)", t):
        pace = "fast"
    elif re.search(r"천천|느리|장기|분할|나눠|서서히", t):
        pace = "slow"
    elif re.search(r"보통|중간|normal", t):
        pace = "normal"
    if pace is None:
        # distill fallback.
        pace = (profile_mod.distill(t).get("suggested", {}) or {}).get("rebalance_pace") or "normal"
    custom = bool(re.search(r"\d+\s*(일|주|회|차)", t))

    plan = PACE_PLAN.get(pace, PACE_PLAN["normal"])
    risk_warnings: list = []
    if pace == "fast":
        risk_warnings.append("너무 빠른 매수/매도는 슬리피지·추격매수·세금/수수료를 키웁니다 — 진입은 항상 지정가(예측 진입)로.")
    risk_warnings.append("진입은 시장가 금지 — '무릎' 지점에 지정가로 예약(예측 진입)해 분할로 채웁니다.")

    missing_points: list = []
    follow_up: list = []
    if not custom and not re.search(r"분할|나눠", t):
        missing_points.append("분할 회수/간격이 구체적이지 않습니다 — 며칠 간격 몇 회로 나눌지 정하면 좋습니다.")
        follow_up.append(f"{plan['rounds']}회로 {plan['days_per_round']} 간격 분할을 적용할까요?")

    suggested = (f"조정 속도: {pace} — {plan['note']} "
                 f"분할 계획: {plan['rounds']}회 / 라운드당 {plan['days_per_round']}. "
                 "각 라운드는 예약성 지정가로 무릎 지점에 배치(일·주 단위 판단).")
    extracted = {
        "rebalance_pace": pace,
        "split_plan": {"rounds": plan["rounds"], "interval": plan["days_per_round"]},
        "entry_rule": "limit_only(예측 진입, 시장가 금지)",
        "custom_specified": custom,
    }
    sources = []
    for ln in lessons_mod.search(scope="decision", limit=1):
        sources.append({"kind": "lesson", "id": ln["id"], "title": ln["title"], "note": ln["body"]})
    conf = 0.6
    return _struct("pace", agent, advice_type, original_text=text, suggested_text=suggested,
                   extracted_variables=extracted, risk_warnings=risk_warnings,
                   missing_points=missing_points, follow_up=follow_up, sources=sources, confidence=conf)


# ============================================================
# 6) whole_advisor — 관심 + 생각 전체 정합성
# ============================================================

def whole_advisor(account_index: int, interests: str, views: str, advice_type: str = "reflect") -> dict:
    agent = FIELD_AGENTS["whole"]
    th = theme_advisor(account_index, interests or "", "improve")
    op = opinion_advisor(account_index, views or "", "improve")

    risk_warnings: list = []
    missing_points: list = list(op["missing_points"])
    follow_up: list = []

    # 정합성: 방어적인데 고변동성 테마 다수.
    rt = op["extracted_variables"].get("risk_tolerance")
    highvol = [c["theme"] for c in th["extracted_variables"].get("classified", []) if c["class"] == "고변동성"]
    if rt == "defensive" and highvol:
        risk_warnings.append(
            f"방어적 성향인데 고변동성 테마({', '.join(highvol)})가 큽니다 — 성향과 관심이 충돌합니다. "
            "비중을 낮추거나 ETF 코어로 완충하세요.")
    if rt == "aggressive" and not th["extracted_variables"].get("themes"):
        missing_points.append("공격적 성향이지만 관심 테마가 비어있습니다 — tilt 대상이 없습니다.")

    # 필드 누락 종합.
    if not (interests or "").strip():
        missing_points.append("관심 분야 필드가 비었습니다.")
    if not (views or "").strip():
        missing_points.append("내 생각 필드가 비었습니다.")

    # 정책 객체 outline 초안.
    policy_outline = {
        "risk_tolerance": op["extracted_variables"].get("risk_tolerance"),
        "cash_band": op["extracted_variables"].get("cash_band"),
        "pace": op["extracted_variables"].get("pace"),
        "themes": th["extracted_variables"].get("themes"),
        "hedge_themes": th["extracted_variables"].get("hedge_themes"),
        "per_theme_tilt_cap_pct": th["extracted_variables"].get("per_theme_tilt_cap_pct"),
    }

    risk_warnings.extend(th["risk_warnings"][:1])
    follow_up.extend((th["follow_up"] + op["follow_up"])[:3])

    suggested = (f"관심: {th['suggested_text']} / 생각: {op['suggested_text']}")
    extracted = {"policy_outline": policy_outline,
                 "field_consistency": "ok" if not risk_warnings else "needs_review"}
    # whole 은 per-field sources 를 덮지 않는다 — 각 필드 source 를 라벨링해 합친다.
    sources = ([{**s, "from": "interests"} for s in th["sources"]] +
               [{**s, "from": "views"} for s in op["sources"]])
    conf = round((th["confidence"] + op["confidence"]) / 2, 3)
    return _struct("whole", agent, advice_type, original_text=f"[관심]{interests or ''} [생각]{views or ''}",
                   suggested_text=suggested, extracted_variables=extracted, risk_warnings=risk_warnings,
                   missing_points=missing_points, follow_up=follow_up, sources=sources, confidence=conf)


# ============================================================
# consult dispatcher — prehook → advisor → write row → posthook
# ============================================================

_ADVICE_TYPES: dict[str, set] = {
    "interests": {"improve", "risk_check", "to_allocation"},
    "views": {"improve", "find_gaps", "extract", "recommend"},
    "region": {"improve", "risk_check"},
    "defensive": {"improve", "risk_check"},
    "pace": {"improve", "risk_check"},
    "whole": {"reflect", "improve"},
}


def consult(account_index: int | None, field_name: str, text: str = "",
            advice_type: str | None = None, *, interests: str | None = None,
            views: str | None = None) -> dict:
    """필드 디스패처. account_index 필수(없으면 hard-block). 조언 + consultation_id 반환."""
    if account_index is None:
        return {"ok": False, "error": "account_id 없음 — 필드 조언은 계좌 귀속이 필수입니다(hard-block).",
                "gate": "block"}
    if field_name not in FIELD_AGENTS:
        return {"ok": False, "error": f"알 수 없는 필드: {field_name}"}

    agent = FIELD_AGENTS[field_name]
    valid = _ADVICE_TYPES.get(field_name, {"improve"})
    if advice_type not in valid:
        advice_type = next(iter(valid)) if field_name != "whole" else "reflect"

    # prehook — account_id 검증 게이트(필드 조언은 block 없지만 account 누락은 위에서 막음) + 메모리 provenance.
    pre = prehooks.prepare(agent, "consult", account_index=account_index)
    task_id = pre.get("task_id")

    # advisor 실행.
    if field_name == "interests":
        advice = theme_advisor(account_index, text, advice_type)
    elif field_name == "views":
        advice = opinion_advisor(account_index, text, advice_type)
    elif field_name == "region":
        advice = region_advisor(account_index, text, advice_type)
    elif field_name == "defensive":
        advice = defensive_advisor(account_index, text, advice_type)
    elif field_name == "pace":
        advice = pace_advisor(account_index, text, advice_type)
    else:  # whole
        advice = whole_advisor(account_index, interests or "", views or "", advice_type)

    # field_consultations 행 기록 (조언 = 임시 제안. 정책 변경 아님).
    evidence_ids = [s.get("id") for s in advice["sources"] if str(s.get("kind", "")).startswith("memory")]
    lesson_ids = [s.get("id") for s in advice["sources"] if s.get("kind") == "lesson"]
    conn = store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO field_consultations(account_index, field_name, agent_name, advice_type, "
            "original_text, suggested_text, extracted_variables_json, risk_warnings_json, "
            "missing_points_json, follow_up_json, evidence_ids, lesson_ids, confidence, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_index, field_name, advice["agent_name"], advice["advice_type"],
             advice["original_text"], advice["suggested_text"],
             json.dumps(advice["extracted_variables"], ensure_ascii=False),
             json.dumps(advice["risk_warnings"], ensure_ascii=False),
             json.dumps(advice["missing_points"], ensure_ascii=False),
             json.dumps(advice["follow_up"], ensure_ascii=False),
             json.dumps(evidence_ids, ensure_ascii=False),
             json.dumps(lesson_ids, ensure_ascii=False),
             advice["confidence"], _now()),
        )
        consultation_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    # posthook — 반복 가능한 조언이면 lesson candidate(즉시 lessons 승격 아님) + provenance.
    lesson_candidates = []
    if advice["risk_warnings"]:
        lesson_candidates.append({
            "scope": "premise" if field_name in ("views", "whole") else "sector",
            "title": f"[필드:{field_name}] 반복 위험 점검",
            "body": advice["risk_warnings"][0],
            "ref": (advice["extracted_variables"].get("themes") or [None])[0] if field_name == "interests" else None,
            "confidence": advice["confidence"], "agent": agent, "source": "field_advisor",
        })
    if task_id is not None:
        posthooks.finalize(
            task_id, status="done",
            outcome={"field": field_name, "consultation_id": consultation_id,
                     "advice_type": advice_type, "note": "temp suggestion — no policy change until save"},
            lesson_candidates=lesson_candidates,
            next_action="사용자 저장 시 policy version 생성(그 전엔 정책 불변)",
        )

    return {"ok": True, "consultation_id": consultation_id, "task_id": task_id, "advice": advice}


def record_action(consultation_id: int, account_index: int, field_name: str,
                  user_action: str, detail: str | None = None) -> dict:
    """field_advice_events 적재(append-only). user_action ∈ applied|edited|ignored|saved."""
    if user_action not in {"applied", "edited", "ignored", "saved"}:
        return {"ok": False, "error": f"잘못된 user_action: {user_action}"}
    conn = store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO field_advice_events(account_index, field_consultation_id, field_name, "
            "user_action, detail, created_at) VALUES(?,?,?,?,?,?)",
            (account_index, consultation_id, field_name, user_action, detail, _now()),
        )
        conn.commit()
        return {"ok": True, "event_id": cur.lastrowid, "user_action": user_action}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="필드별 전문 조언 — Anthropic API 미사용(규칙+메모리)")
    ap.add_argument("--account", type=int, help="계좌 인덱스 (필수 — 없으면 hard-block)")
    ap.add_argument("--field", choices=list(FIELD_AGENTS), help="조언할 필드")
    ap.add_argument("--text", default="", help="필드 텍스트")
    ap.add_argument("--interests", default="", help="whole: 관심 분야")
    ap.add_argument("--views", default="", help="whole: 내 생각")
    ap.add_argument("--advice-type", dest="advice_type", default=None)
    # record action 모드.
    ap.add_argument("--record", action="store_true", help="사용자 행동 기록 모드")
    ap.add_argument("--consultation-id", dest="consultation_id", type=int)
    ap.add_argument("--user-action", dest="user_action")
    ap.add_argument("--detail", default=None)
    args = ap.parse_args()

    try:
        if args.record:
            if args.account is None or args.consultation_id is None or not args.user_action:
                out = {"ok": False, "error": "--record 에는 --account --consultation-id --user-action 필요"}
            else:
                out = record_action(args.consultation_id, args.account, args.field or "",
                                    args.user_action, args.detail)
        elif args.account is None:
            out = {"ok": False, "error": "account_id 없음 — 필드 조언은 계좌가 필수입니다(hard-block).",
                   "gate": "block"}
        elif not args.field:
            out = {"ok": False, "error": "--field 필요"}
        elif args.field == "whole":
            out = consult(args.account, "whole", advice_type=args.advice_type,
                          interests=args.interests, views=args.views)
        else:
            out = consult(args.account, args.field, text=args.text, advice_type=args.advice_type)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
