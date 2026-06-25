"""금리 동향 기반 국채 비중·듀레이션 **추천(제안) 엔진** — 방어자산 내부의 국채.

CEO 목적: 방어자산 안의 국채 비중과 듀레이션을 **금리 동향 기반으로 추천**한다.

진입점 2종:
  - recommend(account)   : 금리 동향 기반 **단일** 비중·듀레이션 추천(기존).
  - bond_options(account): 거시+계좌 목적 기반 **국채 비중 후보(A/B/C/D)** 추천형 엔진(신규).
    "국채 몇%?"를 사용자가 찍는 게 아니라, 시스템이 후보 3~4안을 제시하고 사용자가 고른다.
    각 후보에 전체환산·트레이드오프(rising/falling/fx/liquidity)·account_fit·장기채 변동성 경고·
    system_recommended 강조 포함. 추천일 뿐 — 자동 policy/주문 0(사용자 선택 → 3안 재생성 → 재확정).

  - 금리 인상기/높음  → 단기국채·현금↑ (듀레이션 짧게)
  - 인하 기대/하락기  → 장기국채 일부 (듀레이션 길게)
  - 불확실           → bond ladder (단기/장기 분산)
  - 장단기 역전(곡선 역전) → 경기둔화 신호 (방어 보수화)
모든 비중(%)을 계산해 제시한다(방어 대비 % + 전체 환산 %).

본질 원칙(불변 — CLAUDE.md §2, §11.8):
  - **추천(제안)일 뿐.** 출력은 항상 requires_user_approval=True · auto_applied=False.
    자동 policy 변경/주문 0. 실제 반영은 사용자가 채권 비중 입력 → 3안 재생성 → 재확정(확정안=truth).
  - **가짜 데이터 금지.** 금리 미연동이고 사용자 금리뷰도 없으면 rate_regime='unknown' 으로
    일반 원칙만 제시하고 **숫자(비중/듀레이션 split)는 만들지 않는다**.
  - **데이터 소스 정직.** macro 연동이면 실 금리(macro_connected), 미연동이면 사용자 금리뷰
    (user_view), 둘 다 없으면 none — 출력 data_source 에 정직하게 표기.
  - **읽기 전용 소스.** macro_connect / bond_bucket / user_views / investor_objective 는 읽기만.
  - 비밀(.env) 0 · Anthropic API 미사용 (규칙 + Claude+메모리).

데이터 소스(읽기):
  - macro_connect.macro_snapshot()    — 한·미 기준금리·국고채 2Y/10Y (ECOS/FRED, 미연동 가능)
  - bond_bucket.defensive_breakdown() — 방어 총량 + 현 국채 비율(확정안/프로필)
  - user_views(layer='macro') / investor_objective.market_view — 사용자 수동 금리 견해

  python -m main_mission.portfolio_os.bond_recommendation --account 1
"""
from __future__ import annotations

import argparse
import json
import sys

from . import bond_bucket, macro_connect, user_views, investor_objective

# ── rate_regime enum (이 모듈이 SSOT) ──────────────────────────────
#   rising       금리 인상기(최근 상승) → 단기·현금↑
#   high         금리 수준 높음(인상기 아니어도) → 보수적·단기
#   cut_expected 인하 기대(사용자 견해/완화 신호) → 장기 일부
#   falling      금리 하락기(최근 하락) → 장기 듀레이션↑
#   uncertain    불확실(혼조/역전 등) → ladder 분산
#   unknown      금리 데이터·견해 없음 → 일반 원칙만(가짜 숫자 0)
RATE_REGIMES = ("rising", "high", "cut_expected", "falling", "uncertain", "unknown")

# regime → 듀레이션 기본 split (방어 대비 국채 내부의 단기/장기 비율, 합 100).
#   mixed/ladder 일 때 적용. short/long 단일이면 split 의미는 100/0 또는 0/100 으로 표기.
_LADDER_SPLIT = {"short": 50.0, "long": 50.0}

# regime → 권장 국채 비율(방어자산 대비 %). **금리 동향 기반 기준선**(사용자 확정 전 제안).
#   인상/고금리는 보수적(국채 비중은 단기 위주로 작게·현금 여력↑), 인하기대/하락은 장기 일부로 늘림.
_REGIME_BOND_RATIO = {
    "rising": 30.0,        # 단기 위주, 현금 여력↑ → 방어 내 국채 비중 보수적
    "high": 35.0,          # 고금리: 단기국채 이자 매력 있으나 듀레이션은 짧게
    "cut_expected": 55.0,  # 인하 기대: 장기국채 일부로 자본이득 기대 → 비중↑
    "falling": 60.0,       # 하락기: 장기 듀레이션 비중 더 확대
    "uncertain": 45.0,     # 불확실: ladder 로 중간 수준
}


def _round1(x: float) -> float:
    return round(float(x), 1)


# ============================================================
# 1) 금리 동향 분류 (macro 우선, 없으면 사용자 견해, 둘 다 없으면 unknown)
# ============================================================
def _macro_regime(snapshot: dict) -> dict | None:
    """macro_snapshot → rate_regime 분류. 신선(non-stale) 지표만 사용(가짜 신호 금지).

    분류 규칙:
      - 장단기 역전(10Y-2Y <= 0, 한국 또는 미국) → 경기둔화 신호 → 인하 기대 쪽이나
        불확실/방어 보수화로 본다(곡선 역전 = cut_expected 후보 + uncertain 가산).
      - policy_rate_change_3m > 0 (최근 인상) → rising.
      - policy_rate_change_3m < 0 (최근 인하) → falling.
      - 변화 정보 없고 기준금리 수준 높음(>=3.0%) → high.
      - 그 외(혼조/판단 근거 약함) → uncertain.
    신선 금리 지표가 하나도 없으면 None(→ 사용자 견해로 폴백)."""
    ind = snapshot.get("indicators", {}) or {}

    def fresh(name):
        x = ind.get(name)
        return x if (x and not x.get("stale")) else None

    rationale: list[str] = []
    used: list[str] = []

    # 장단기 스프레드(역전 = 경기둔화 선행)
    inverted = False
    spread_val = None
    for a, b, label in (("yield_10y", "yield_2y", "한국 국채"),
                        ("yield_10y_us", "yield_2y_us", "미국 국채")):
        ra, rb = fresh(a), fresh(b)
        if ra and rb:
            sp = round(ra["value"] - rb["value"], 3)
            spread_val = sp if spread_val is None else spread_val
            used += [a, b]
            if sp <= 0:
                inverted = True
                rationale.append(f"{label} 10Y-2Y {sp:+.2f}%p (장단기 역전) — 경기둔화 선행 신호.")

    # 최근 금리 변화(3개월) — 인상/인하 방향
    change = fresh("policy_rate_change_3m") or fresh("policy_rate_change_3m_us")
    level = fresh("policy_rate") or fresh("policy_rate_us")

    regime = None
    if change is not None:
        used.append("policy_rate_change_3m")
        if change["value"] > 0:
            regime = "rising"
            rationale.append(f"최근 3개월 기준금리 변화 {change['value']:+.2f}%p — 인상기(단기·현금 선호).")
        elif change["value"] < 0:
            regime = "falling"
            rationale.append(f"최근 3개월 기준금리 변화 {change['value']:+.2f}%p — 하락기(장기 듀레이션 확대 여지).")

    if regime is None and level is not None:
        used.append("policy_rate")
        if level["value"] >= 3.0:
            regime = "high"
            rationale.append(f"기준금리 {level['value']:.2f}% — 고금리 환경(듀레이션 짧게, 현금 여력↑).")

    # 곡선 역전이면, 명확한 인상기가 아닌 한 인하 기대/불확실 쪽으로 본다.
    if inverted:
        if regime in (None, "high"):
            regime = "cut_expected"
            rationale.append("역전 곡선 — 향후 인하 기대(장기국채 일부 분산 검토), 단 단기 변동성은 ladder 로 대비.")
        elif regime == "rising":
            # 인상 중인데 역전 → 사이클 후반 불확실
            regime = "uncertain"
            rationale.append("인상기지만 곡선 역전 — 사이클 후반 불확실: ladder(단기/장기 분산) 권장.")

    if regime is None:
        if not used:
            return None  # 신선 금리 지표 없음 → 사용자 견해로 폴백
        regime = "uncertain"
        rationale.append("금리 방향 신호가 혼조/약함 — 불확실: ladder 분산 권장.")

    return {
        "rate_regime": regime,
        "data_source": "macro_connected",
        "rationale": rationale,
        "indicators_used": sorted(set(used)),
        "curve_inverted": inverted,
        "spread_10y_2y": spread_val,
        "as_of": snapshot.get("as_of"),
    }


# 사용자 금리 견해 → regime 매핑(자유 텍스트/stance/market_view 규칙).
_RISING_KW = ("인상", "올린다", "올릴", "상승", "금리 상승", "긴축", "hike", "rising", "raise")
_CUT_KW = ("인하", "내린다", "내릴", "하락", "완화", "금리 인하", "cut", "easing", "falling", "lower")
_UNCERTAIN_KW = ("불확실", "혼조", "모르", "관망", "변동", "uncertain", "mixed")
_HIGH_KW = ("높", "고금리", "high")


def _user_view_regime(account_index: int) -> dict | None:
    """사용자 수동 금리뷰 → rate_regime. user_views(theme/note 에 금리 의도) 또는
    investor_objective.market_view 를 읽어 규칙 매핑. 금리 견해가 없으면 None.

    layer 제약을 강제하지 않고(스키마 enum 호환), theme/note 텍스트에서 금리 의도를 감지한다.
    stance(positive/negative)는 '금리에 대한' 견해로 해석한다:
      금리 positive(=오른다고 봄) → rising / 금리 negative(=내린다고 봄) → cut_expected.
    """
    text_parts: list[str] = []
    stance_dir = None
    matched_view = None
    try:
        for v in user_views.list_views(account_index, status="active"):
            blob = " ".join(str(x) for x in (v.get("theme"), v.get("note")) if x)
            if not blob:
                continue
            low = blob.lower()
            # 금리/채권 관련 견해만 사용(테마가 금리/채권일 때).
            if any(k in blob for k in ("금리", "채권", "국채", "duration", "듀레이션")) or \
               any(k in low for k in ("rate", "yield", "bond", "treasury")):
                text_parts.append(blob)
                if v.get("stance") in ("positive", "negative") and stance_dir is None:
                    stance_dir = v["stance"]
                    matched_view = v.get("id")
    except Exception:  # noqa: BLE001 — 견해 조회 실패는 None 폴백(정직)
        pass

    # investor_objective.market_view 는 보는 '기간'(short/long)일 뿐 금리 방향이 아님 →
    #   단독으로 regime 을 만들지 않는다(가짜 신호 금지). note 의 자유 견해만 본다.
    obj = None
    try:
        obj = investor_objective.get(account_index)
    except Exception:  # noqa: BLE001
        obj = None
    if obj and obj.get("note"):
        on = str(obj["note"])
        if any(k in on for k in ("금리", "채권", "국채")) or \
           any(k in on.lower() for k in ("rate", "yield", "bond")):
            text_parts.append(on)

    blob = " ".join(text_parts).strip()
    if not blob:
        return None

    low = blob.lower()
    rationale: list[str] = []

    def has(words):
        return any(w in blob or w in low for w in words)

    regime = None
    if has(_UNCERTAIN_KW):
        regime = "uncertain"
        rationale.append("사용자 금리 견해: 불확실/혼조 — ladder(단기/장기 분산) 권장.")
    elif has(_RISING_KW) or stance_dir == "positive":
        regime = "rising"
        rationale.append("사용자 금리 견해: 인상/상승 예상 — 단기국채·현금 선호(듀레이션 짧게).")
    elif has(_CUT_KW) or stance_dir == "negative":
        regime = "cut_expected"
        rationale.append("사용자 금리 견해: 인하/하락 예상 — 장기국채 일부 분산 검토(듀레이션 길게).")
    elif has(_HIGH_KW):
        regime = "high"
        rationale.append("사용자 금리 견해: 금리 수준이 높다고 봄 — 단기 위주·현금 여력↑.")

    if regime is None:
        return None  # 금리 관련 텍스트는 있으나 방향 불명 → unknown 으로 폴백
    return {
        "rate_regime": regime,
        "data_source": "user_view",
        "rationale": rationale,
        "user_view_id": matched_view,
        "curve_inverted": None,
        "spread_10y_2y": None,
    }


def classify_rate_regime(account_index: int, *, snapshot: dict | None = None) -> dict:
    """금리 동향 분류 — macro 연동 우선, 없으면 사용자 견해, 둘 다 없으면 unknown.

    정직: 어떤 소스를 썼는지 data_source 로 표기. macro 미연동·견해 없음이면
    rate_regime='unknown' 이고 숫자 추천을 만들지 않는다(가짜 0)."""
    snap = snapshot if snapshot is not None else macro_connect.macro_snapshot()
    if snap.get("data_available"):
        m = _macro_regime(snap)
        if m is not None:
            return m
    u = _user_view_regime(account_index)
    if u is not None:
        return u
    return {
        "rate_regime": "unknown",
        "data_source": "none",
        "rationale": ["금리 데이터(ECOS/FRED) 미연동이고 사용자 금리 견해도 없습니다 — "
                      "일반 원칙만 제시합니다. 금리 지표 적재 또는 금리 견해 입력 시 정교화됩니다."],
        "curve_inverted": None,
        "spread_10y_2y": None,
    }


# ============================================================
# 2) regime → 국채 비중·듀레이션 매핑 (모든 % 계산)
# ============================================================
def _regime_to_bond_plan(regime: str) -> dict:
    """rate_regime → {suggested_bond_ratio_pct(방어 대비), duration, split, ladder, principle}.

    unknown 이면 숫자 없이 일반 원칙만(가짜 숫자 0)."""
    if regime == "unknown":
        return {
            "suggested_bond_ratio_pct": None,
            "suggested_duration": None,
            "suggested_split": None,
            "ladder": None,
            "principle": ("일반 원칙: 인상기/고금리 → 단기국채·현금↑(듀레이션 짧게), "
                          "인하 기대 → 장기국채 일부(듀레이션 길게), 불확실 → ladder(단기/장기 분산)."),
        }
    ratio = _REGIME_BOND_RATIO[regime]
    if regime in ("rising", "high"):
        return {"suggested_bond_ratio_pct": ratio, "suggested_duration": "short",
                "suggested_split": {"short": 100.0, "long": 0.0}, "ladder": False,
                "principle": "금리 인상/고금리 — 단기국채로 듀레이션 짧게, 현금 여력 확보."}
    if regime in ("cut_expected", "falling"):
        # 인하/하락: 장기 일부로 자본이득 기대하되 전량 장기는 위험 → 장기 위주 mixed.
        split = {"short": 30.0, "long": 70.0}
        return {"suggested_bond_ratio_pct": ratio,
                "suggested_duration": "long" if regime == "falling" else "mixed",
                "suggested_split": split, "ladder": regime == "cut_expected",
                "principle": "금리 인하 기대/하락 — 장기국채 일부로 듀레이션 확대(자본이득 기대)."}
    # uncertain → ladder
    return {"suggested_bond_ratio_pct": ratio, "suggested_duration": "mixed",
            "suggested_split": dict(_LADDER_SPLIT), "ladder": True,
            "principle": "금리 불확실 — bond ladder(단기50/장기50)로 분산."}


# ============================================================
# 3) recommend(account) — 메인 진입점
# ============================================================
def recommend(account_index: int, *, snapshot: dict | None = None) -> dict:
    """금리 동향 기반 국채 비중·듀레이션 **추천**. 추천일 뿐 — 자동 반영/주문 0.

    출력:
      rate_regime, suggested_bond_ratio_pct(방어 대비%), suggested_duration,
      suggested_split({short,long}), ladder, rationale[], data_source,
      confidence, applies_to_defensive, + 전체 환산(% of total) + 현재 vs 제안 비교.
    """
    cls = classify_rate_regime(account_index, snapshot=snapshot)
    regime = cls["rate_regime"]
    data_source = cls["data_source"]
    plan = _regime_to_bond_plan(regime)

    # 방어 총량 + 현 국채 비율 (읽기 — bond_bucket; 확정안 있으면 truth).
    defensive_pct = None
    current_bond_ratio = None
    confirmed = None
    bd_source = None
    try:
        bd = bond_bucket.defensive_breakdown(account_index)
        if bd.get("ok"):
            defensive_pct = float((bd.get("cash_band") or {}).get("target")) \
                if (bd.get("cash_band") or {}).get("target") is not None else None
            current_bond_ratio = float((bd.get("breakdown") or {}).get("bond_ratio_pct")) \
                if (bd.get("breakdown") or {}).get("bond_ratio_pct") is not None else None
            confirmed = bd.get("confirmed")
            bd_source = bd.get("source")
    except Exception:  # noqa: BLE001 — 방어 구성 조회 실패는 정직하게 None
        pass

    rationale = list(cls.get("rationale") or [])
    rationale.append(plan["principle"])

    # 전체 환산: 제안 국채비율(방어 대비) × 방어총량 / 100 = 국채 전체%(절대).
    suggested_ratio = plan["suggested_bond_ratio_pct"]
    total_breakdown = None
    if suggested_ratio is not None and defensive_pct is not None:
        # bond_bucket.compute_breakdown 재사용(읽기 전용 계산 — carve 정합 동일).
        dur = plan["suggested_duration"]
        split = plan["suggested_split"]
        bdur = "mixed" if (split and split.get("short", 0) > 0 and split.get("long", 0) > 0
                           and dur in ("mixed", "long", "short")) else dur
        # split 이 단일(100/0)이면 short, (0/100)이면 long 으로 환산.
        if split and split.get("short", 0) == 100.0:
            bdur = "short"
        elif split and split.get("long", 0) == 100.0:
            bdur = "long"
        comp = bond_bucket.compute_breakdown(defensive_pct, suggested_ratio, bdur,
                                             split if bdur == "mixed" else None)
        total_breakdown = {
            "defensive_bucket_pct": comp["defensive_bucket_pct"],
            "suggested_govbond_pct_of_total": comp["govbond_pct"],
            "suggested_pure_cash_pct_of_total": comp["pure_cash_pct"],
            "suggested_short_govbond_pct_of_total": comp["short_govbond_pct"],
            "suggested_long_govbond_pct_of_total": comp["long_govbond_pct"],
            "risk_asset_pct": comp["risk_asset_pct"],
        }

    # confidence: macro_connected > user_view > none. 신선도/역전 신호로 가감.
    if data_source == "macro_connected":
        confidence = 0.75 if regime != "uncertain" else 0.6
    elif data_source == "user_view":
        confidence = 0.5
    else:
        confidence = 0.0  # unknown — 일반 원칙만

    # 현재 vs 제안 비교(정직 — 추천일 뿐).
    comparison = None
    if suggested_ratio is not None and current_bond_ratio is not None:
        delta = _round1(suggested_ratio - current_bond_ratio)
        direction = "increase" if delta > 0 else "decrease" if delta < 0 else "hold"
        comparison = {
            "current_bond_ratio_pct": current_bond_ratio,
            "suggested_bond_ratio_pct": suggested_ratio,
            "delta_pct_points": delta,
            "direction": direction,
            "note": ("현재 확정안 대비 제안 방향입니다(추천일 뿐 — 자동 반영 안 함)."
                     if confirmed else "현재 프로필 미리보기 대비 제안입니다(미확정)."),
        }

    out = {
        "ok": True,
        "account_index": int(account_index),
        "rate_regime": regime,
        "data_source": data_source,
        "suggested_bond_ratio_pct": suggested_ratio,     # 방어자산 대비 국채 비율(0~100)
        "suggested_duration": plan["suggested_duration"],  # short|long|mixed (unknown→None)
        "suggested_split": plan["suggested_split"],       # {short,long} 합100 (unknown→None)
        "ladder": plan["ladder"],
        "applies_to_defensive": True,                     # 위험자산이 아니라 방어자산 내부 국채
        "confidence": confidence,
        "rationale": rationale,
        "curve_inverted": cls.get("curve_inverted"),
        "spread_10y_2y": cls.get("spread_10y_2y"),
        "indicators_used": cls.get("indicators_used", []),
        "defensive_context": {
            "defensive_bucket_pct": defensive_pct,
            "current_bond_ratio_pct": current_bond_ratio,
            "confirmed": confirmed,
            "source": bd_source,
        },
        "total_breakdown": total_breakdown,               # 전체 환산(방어총량 알 때만)
        "comparison": comparison,
        # ── 추천일 뿐: 자동 반영/주문 0 (불변) ──
        "requires_user_approval": True,
        "auto_applied": False,
        "auto_order_created": False,
        "policy_written": False,
        "note": _honest_note(regime, data_source),
    }
    return out


# ============================================================
# 4) bond_options(account) — 국채 비중 **후보(A/B/C/D) 추천형 엔진**
# ============================================================
# CEO 목적: "국채 몇%?" 숫자를 사용자가 찍는 게 아니라, 시스템이 거시(금리/환율/VIX/곡선)
#   + 계좌 목적/방어자산/손실회피를 분석해 **국채 비중 후보 3~4안을 제시**하고 사용자가 고른다.
#   로보어드바이저 표준: 단기~중기 기본, 금리환경별 트레이드오프 설명, 장기채=변동성 큼(안전자산 아님).
#
# 후보 비중(방어자산 대비 국채 비율 %)의 기준 사다리. rate_regime·계좌 목적으로 *동적* 선택.
_OPTION_LADDER = (0.0, 25.0, 40.0, 50.0)

# 장기국채 변동성 경고(불변 — 후보 설명에 항상 포함). "안전자산 단정 금지".
_LONG_BOND_VOLATILITY_WARNING = (
    "장기국채는 금리 하락 시 자본이득(수혜)이 크지만, 금리가 오르면 가격 하락폭도 큽니다 "
    "— 단기 변동성이 주식 못지않을 수 있어 '안전자산'으로 단정하지 마세요(듀레이션 위험)."
)

# 후보 비중별 듀레이션 split(방어 대비 국채 내부의 단기/장기, 합100).
#   낮은 비중은 단기 위주(안정), 높은 비중일수록 장기 비중을 키워 금리 방향 베팅 성격↑.
#   단, 인상기/고금리 regime 에서는 어떤 후보든 장기 비중을 절제한다(아래 _option_split).
_OPTION_BASE_SPLIT = {
    0.0: None,                                  # 국채 0 → split 없음
    25.0: {"short": 80.0, "long": 20.0},        # 보수: 단기 위주
    40.0: {"short": 60.0, "long": 40.0},        # 균형
    50.0: {"short": 50.0, "long": 50.0},        # 적극: 듀레이션 확대
}


def _objective_profile(account_index: int) -> dict:
    """계좌 목적/성향 읽기(읽기 전용). 방어 성향(국채·단기 보수) vs 성장 성향 판정용.

    investor_objective.get → investment_goal·risk_tolerance·loss_aversion.
    조회 실패/미설정이면 None 들로 정직 반환(가정 금지)."""
    goal = risk = None
    loss_av = None
    try:
        obj = investor_objective.get(account_index)
        if obj:
            goal = obj.get("investment_goal")
            risk = obj.get("risk_tolerance")
            loss_av = obj.get("loss_aversion")
    except Exception:  # noqa: BLE001 — 목적 조회 실패는 정직하게 None
        pass
    # 방어 지향 목적(국채·단기 비중 보수적으로 키움) vs 성장 지향(국채 낮춤).
    _DEFENSIVE_GOALS = {"loss_reduction", "cash_preservation", "volatility_reduction", "dividend"}
    _GROWTH_GOALS = {"growth", "aggressive_growth"}
    lean = "neutral"
    if goal in _DEFENSIVE_GOALS or risk == "low" or (loss_av is not None and loss_av >= 0.6):
        lean = "defensive"
    if goal in _GROWTH_GOALS or risk == "high":
        # 성장 신호가 방어 신호보다 우선하지 않도록: 손실회피 강하면 방어 유지.
        if lean != "defensive":
            lean = "growth"
    return {"investment_goal": goal, "risk_tolerance": risk,
            "loss_aversion": loss_av, "lean": lean, "is_set": bool(goal or risk)}


def _select_option_ratios(regime: str, lean: str) -> list[float]:
    """rate_regime + 계좌 성향 → 제시할 후보 비중 3~4안(방어 대비 %).

    - 방어 지향 계좌: 0 을 빼고 국채를 더 적극 제시(25/40/50)하되, 인상기엔 50 절제.
    - 성장 지향 계좌: 국채 낮은 쪽 강조(0/25/40).
    - 중립: 0/25/40/50 모두.
    - unknown regime 이라도 후보 사다리는 제시(단 system_recommended 는 없음)."""
    if lean == "defensive":
        base = [25.0, 40.0, 50.0]
        # 인상기/고금리: 듀레이션 위험 → 최상단(50, 장기 다)은 빼고 0(현금 여력)을 추가 옵션으로.
        if regime in ("rising", "high"):
            base = [0.0, 25.0, 40.0]
    elif lean == "growth":
        base = [0.0, 25.0, 40.0]
        if regime in ("cut_expected", "falling"):
            base = [0.0, 25.0, 40.0, 50.0]
    else:  # neutral
        base = [0.0, 25.0, 40.0, 50.0]
        if regime in ("rising", "high"):
            base = [0.0, 25.0, 40.0]  # 인상기엔 적극(50) 절제
    # 항상 정렬·중복 제거.
    return sorted(set(base))


def _option_split(ratio: float, regime: str) -> dict | None:
    """후보 비중 + regime → 듀레이션 split. 인상기/고금리는 장기 절제(단기 위주)."""
    if ratio <= 0.0:
        return None
    base = dict(_OPTION_BASE_SPLIT.get(ratio) or {"short": 60.0, "long": 40.0})
    if regime in ("rising", "high"):
        # 듀레이션 짧게 — 장기 비중을 크게 줄인다(상한 20%).
        long_cap = min(base.get("long", 0.0), 20.0)
        return {"short": round(100.0 - long_cap, 1), "long": round(long_cap, 1)}
    if regime in ("cut_expected", "falling"):
        # 인하/하락: 장기 비중을 키워 자본이득 기대(단 전량 장기는 피함).
        long_boost = min(base.get("long", 40.0) + 20.0, 70.0)
        return {"short": round(100.0 - long_boost, 1), "long": round(long_boost, 1)}
    return base


def _recommended_ratio(regime: str, ratios: list[float]) -> float | None:
    """system_recommended: regime 기준선(_REGIME_BOND_RATIO)에 가장 가까운 후보 1개.

    unknown regime 이면 추천 강조 없음(None — 가짜 단정 금지)."""
    if regime == "unknown" or not ratios:
        return None
    target = _REGIME_BOND_RATIO.get(regime)
    if target is None:
        return None
    # 후보 중 기준선에 가장 가까운 값(동률이면 보수적=작은 값).
    return min(ratios, key=lambda r: (abs(r - target), r))


def _option_qualities(ratio: float, regime: str, split: dict | None) -> dict:
    """후보별 정성 설명 필드(트레이드오프 정직 표기)."""
    long_pct = (split or {}).get("long", 0.0)
    has_long = long_pct > 0.0
    rising_risk = (
        "금리가 오르면 국채 가격이 하락 — 비중이 높을수록 손실 노출↑."
        if ratio > 0 else "국채 0% — 금리 상승에 따른 채권 평가손 노출 없음(대신 금리 하락 수혜도 없음).")
    if has_long:
        rising_risk += " 특히 장기 비중이 있어 금리 상승 시 변동성 큼."
    falling_benefit = (
        "금리가 내리면 국채 가격 상승(자본이득) — 비중·듀레이션이 클수록 수혜↑."
        if ratio > 0 else "국채 0% — 금리 하락 국면의 자본이득 기회는 포기.")
    fx_risk = ("국내 국채(KRW) 후보는 환위험 없음. 미국채(SHY/IEF/TLT) 선택 시 원/달러 환위험 추가."
               if ratio > 0 else "해당 없음(국채 0%).")
    liquidity = ("단기국채/현금성은 유동성 높음(필요 시 현금화 용이)."
                 if (split or {}).get("short", 0.0) >= 50.0 or ratio == 0
                 else "장기국채 비중이 커 급매 시 가격 변동 가능 — 유동성은 중간.")
    return {
        "rising_rate_risk": rising_risk,
        "falling_rate_benefit": falling_benefit,
        "fx_risk": fx_risk,
        "liquidity": liquidity,
        "long_bond_volatility_warning": _LONG_BOND_VOLATILITY_WARNING if has_long else None,
    }


def _option_account_fit(ratio: float, regime: str, prof: dict) -> str:
    """후보가 이 계좌 목적/성향에 얼마나 맞는지(정직 — 가정 금지)."""
    lean = prof["lean"]
    goal = prof.get("investment_goal")
    if not prof.get("is_set"):
        return ("계좌 목적/성향 미설정 — 일반 기준으로 제시합니다. 목적을 입력하면 후보가 정교화됩니다.")
    glabel = investor_objective.GOALS.get(goal, goal) if goal else "성향"
    if lean == "defensive":
        if ratio == 0.0:
            return f"방어 지향({glabel})이지만 국채 0%는 현금 100%(방어 내) — 금리상승 우려 시 단기적합."
        if ratio >= 50.0:
            return f"방어 지향({glabel})엔 비중이 다소 큼 — 듀레이션 위험을 단기 split 으로 관리 필요."
        return f"방어 지향({glabel})에 부합 — 단기 위주로 안정적 방어 강화."
    if lean == "growth":
        if ratio >= 40.0:
            return f"성장 지향({glabel})엔 국채 비중이 높은 편 — 위험자산 여력을 줄일 수 있음(보수적 선택)."
        return f"성장 지향({glabel})에 부합 — 방어는 최소화하고 위험자산 여력 확보."
    return "중립 성향 — 금리 환경에 맞춰 균형 있게 선택 가능."


def bond_options(account_index: int, *, snapshot: dict | None = None) -> dict:
    """국채 비중 **후보(A/B/C/D) 추천형 엔진**. 거시 + 계좌 목적으로 후보를 제시(사용자가 선택).

    추천일 뿐 — requires_user_approval=True · auto_applied=False. 자동 policy/주문 0.
    실제 반영은 사용자가 후보를 선택 → 3안(allocation) 재생성 → 재확정(확정안=truth).

    출력 options[] 각 항목:
      label(A/B/C/D), govbond_ratio_pct(방어 대비), suggested_split{short,long},
      전체환산{순현금/단기국채/장기국채/위험 %}, rationale, suited_when,
      rising_rate_risk, falling_rate_benefit, fx_risk, liquidity, account_fit,
      long_bond_volatility_warning, confidence, system_recommended(bool).
    """
    cls = classify_rate_regime(account_index, snapshot=snapshot)
    regime = cls["rate_regime"]
    data_source = cls["data_source"]
    prof = _objective_profile(account_index)

    # 방어 총량 + 현 국채 비율(읽기 — bond_bucket). 전체 환산에 사용.
    defensive_pct = None
    current_bond_ratio = None
    confirmed = None
    bd_source = None
    try:
        bd = bond_bucket.defensive_breakdown(account_index)
        if bd.get("ok"):
            cb = bd.get("cash_band") or {}
            defensive_pct = float(cb["target"]) if cb.get("target") is not None else None
            bk = bd.get("breakdown") or {}
            current_bond_ratio = float(bk["bond_ratio_pct"]) if bk.get("bond_ratio_pct") is not None else None
            confirmed = bd.get("confirmed")
            bd_source = bd.get("source")
    except Exception:  # noqa: BLE001
        pass

    ratios = _select_option_ratios(regime, prof["lean"])
    recommended = _recommended_ratio(regime, ratios)

    # confidence: 데이터 충실도. 거시 연동↑, 사용자 견해 중간, 목적 설정 시 가산.
    if data_source == "macro_connected":
        base_conf = 0.75 if regime != "uncertain" else 0.6
    elif data_source == "user_view":
        base_conf = 0.5
    else:
        base_conf = 0.25  # unknown — 후보 사다리는 제시하나 확신 낮음(추천 강조 없음)
    if prof.get("is_set"):
        base_conf = round(min(base_conf + 0.1, 0.9), 2)

    labels = ["A", "B", "C", "D"]
    options: list[dict] = []
    for i, ratio in enumerate(ratios):
        split = _option_split(ratio, regime)
        # 전체 환산(방어총량 알 때만 — carve 정합은 bond_bucket.compute_breakdown 재사용).
        total_breakdown = None
        if defensive_pct is not None:
            if ratio <= 0.0:
                comp = bond_bucket.compute_breakdown(defensive_pct, 0.0, None, None)
            else:
                comp = bond_bucket.compute_breakdown(defensive_pct, ratio, "mixed", split)
            total_breakdown = {
                "defensive_bucket_pct": comp["defensive_bucket_pct"],
                "govbond_pct_of_total": comp["govbond_pct"],
                "pure_cash_pct_of_total": comp["pure_cash_pct"],
                "short_govbond_pct_of_total": comp["short_govbond_pct"],
                "long_govbond_pct_of_total": comp["long_govbond_pct"],
                "risk_asset_pct": comp["risk_asset_pct"],
            }
        quals = _option_qualities(ratio, regime, split)
        rationale = _option_rationale(ratio, regime, split, prof)
        suited = _option_suited_when(ratio, regime)
        options.append({
            "label": labels[i] if i < len(labels) else f"OPT{i+1}",
            "govbond_ratio_pct": _round1(ratio),       # 방어 대비 국채 비율(0~100)
            "suggested_split": split,                  # {short,long} 합100 (국채 0 → None)
            "total_breakdown": total_breakdown,        # 전체 환산(방어총량 알 때만)
            "rationale": rationale,                    # 왜 이 비중
            "suited_when": suited,                     # 이 후보가 적합한 상황
            "account_fit": _option_account_fit(ratio, regime, prof),
            "confidence": base_conf,
            "system_recommended": (recommended is not None and ratio == recommended),
            **quals,
        })

    out = {
        "ok": True,
        "account_index": int(account_index),
        "rate_regime": regime,
        "data_source": data_source,
        "applies_to_defensive": True,                  # 위험자산이 아니라 방어자산 내부 국채
        "regime_rationale": list(cls.get("rationale") or []),
        "curve_inverted": cls.get("curve_inverted"),
        "spread_10y_2y": cls.get("spread_10y_2y"),
        "indicators_used": cls.get("indicators_used", []),
        "objective": {
            "investment_goal": prof.get("investment_goal"),
            "risk_tolerance": prof.get("risk_tolerance"),
            "loss_aversion": prof.get("loss_aversion"),
            "lean": prof["lean"],
            "is_set": prof["is_set"],
        },
        "defensive_context": {
            "defensive_bucket_pct": defensive_pct,
            "current_bond_ratio_pct": current_bond_ratio,
            "confirmed": confirmed,
            "source": bd_source,
        },
        "options": options,
        "system_recommended_ratio_pct": recommended,   # regime 기준 강조 후보(unknown→None)
        "long_bond_volatility_warning": _LONG_BOND_VOLATILITY_WARNING,
        "govbond_etf_candidates": bond_bucket.govbond_etf_candidates(),  # 실 티커 후보(검증 필요)
        # ── 추천일 뿐: 자동 반영/주문 0 (불변) ──
        "requires_user_approval": True,
        "auto_applied": False,
        "auto_order_created": False,
        "policy_written": False,
        "note": _options_note(regime, data_source),
    }
    return out


def _option_rationale(ratio: float, regime: str, split: dict | None, prof: dict) -> str:
    """후보 비중을 '왜 이 비중' 으로 설명(정직 — regime·계좌 근거)."""
    if ratio == 0.0:
        return ("국채 0% (방어는 전액 순현금). 금리 상승·불확실 국면에서 채권 평가손 회피, "
                "현금 유동성 최대 확보. 금리 하락 수혜는 포기.")
    long_pct = (split or {}).get("long", 0.0)
    dur_desc = ("단기 위주" if long_pct <= 20.0 else
                "단기/장기 균형" if long_pct < 60.0 else "장기 비중↑(듀레이션 확대)")
    regime_phrase = {
        "rising": "인상기라 단기 위주로 짧게 — 듀레이션 위험 최소화.",
        "high": "고금리라 단기국채 이자 매력 활용·듀레이션은 짧게.",
        "cut_expected": "인하 기대라 장기 일부로 자본이득 노림 — 단 전량 장기는 회피.",
        "falling": "하락기라 장기 듀레이션을 늘려 자본이득 기대.",
        "uncertain": "방향 불확실이라 단기/장기 ladder 로 분산.",
        "unknown": "금리 방향 미확정 — 일반 기준으로 제시(가짜 단정 없음).",
    }.get(regime, "")
    return (f"방어자산 대비 국채 {ratio:.0f}% ({dur_desc}). {regime_phrase} "
            f"비중이 클수록 금리 방향에 대한 노출(수혜/손실)이 커집니다.")


def _option_suited_when(ratio: float, regime: str) -> str:
    """이 후보가 적합한 상황(사용자 선택 가이드)."""
    if ratio == 0.0:
        return "금리 상승을 강하게 예상하거나, 방어를 전액 현금으로 두고 유연성을 원할 때."
    if ratio <= 25.0:
        return "채권을 소량만 두어 안정성을 약간 더하되 현금 여력을 크게 유지하고 싶을 때."
    if ratio <= 40.0:
        return "방어자산 안에서 현금과 국채를 균형 있게 가져가고 싶을 때(표준적 선택)."
    return "금리 하락(자본이득)을 기대하거나, 방어를 적극적으로 국채 중심으로 운용하고 싶을 때."


def _options_note(regime: str, data_source: str) -> str:
    base = ("국채 비중 **후보(추천)** 입니다 — 방어자산(현금밴드) 내부의 국채에만 적용. "
            "시스템이 거시(금리/환율/곡선)+계좌 목적을 분석해 후보를 제시할 뿐, "
            "자동 반영/주문은 없습니다. 실제 반영은 사용자가 후보 선택 → 3안(allocation) 재생성 → "
            "재확정(확정안=단일 진실)을 거칩니다. 장기국채는 변동성이 커 안전자산으로 단정하지 마세요.")
    if regime == "unknown" or data_source == "none":
        return (base + " 현재 금리 데이터 미연동·금리 견해 없음 — 후보는 일반 기준이며 "
                "system_recommended 강조는 하지 않습니다(가짜 단정 금지).")
    if data_source == "user_view":
        return base + " 금리 데이터 미연동 — 사용자 금리 견해 기반 후보입니다(실 지표 적재 시 정교화)."
    return base + " 실 금리 지표(ECOS/FRED) 기반 후보입니다(stale 지표 제외 — 정직)."


def _honest_note(regime: str, data_source: str) -> str:
    base = ("국채 비중·듀레이션 **추천(제안)** 입니다 — 방어자산(현금밴드) 내부의 국채에만 적용. "
            "자동 반영/주문은 없습니다. 실제 반영은 사용자가 채권 비중을 입력 → 3안 재생성 → "
            "재확정(확정안=단일 진실)을 거칩니다.")
    if regime == "unknown" or data_source == "none":
        return (base + " 현재 금리 데이터 미연동이고 금리 견해도 없어 **일반 원칙만** 제시합니다 "
                "(가짜 숫자 없음). ECOS/FRED 적재 또는 금리 견해 입력 시 정교화됩니다.")
    if data_source == "user_view":
        return base + " 금리 데이터 미연동 — **사용자 금리 견해** 기반 추천입니다(실 지표 적재 시 정교화)."
    return base + " 실 금리 지표(ECOS/FRED) 기반 추천입니다. stale 지표는 제외했습니다(정직)."


# ============================================================
# CLI
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(
        description="금리 동향 기반 국채 비중·듀레이션 추천(제안일 뿐 — 자동 반영 0)")
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--regime-only", action="store_true",
                    help="금리 동향 분류만 출력(추천 계산 생략)")
    ap.add_argument("--options", action="store_true",
                    help="국채 비중 후보(A/B/C/D) 추천형 엔진 출력(사용자가 선택)")
    args = ap.parse_args()
    try:
        if args.regime_only:
            out = classify_rate_regime(args.account)
        elif args.options:
            out = bond_options(args.account)
        else:
            out = recommend(args.account)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
