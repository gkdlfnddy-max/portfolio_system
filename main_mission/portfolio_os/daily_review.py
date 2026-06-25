"""Daily Portfolio Review — 실시간 trading bot 아님.

정기 점검 → 판단 보조 → (필요시) 예약성 지정가 조정 계획 → 사람 승인 → 회고.
기준 질문: "오늘 이 계좌가 목표 포트폴리오에 더 안전하게 가까워지려면 무엇을 / 또는 아무것도 안 해야 하는가?"

핵심:
  - **관망(hold/watch)도 정상 결과** (실패 아님).
  - 주문 후보는 **selected allocation + drift** 에서만 (decision.compute 가 단일 출처·가드 내장).
  - 주문은 **예약성 지정가**(시장가 매수 금지). 분할·미체결 다음 cycle 재평가.
  - Daily Review 없이 / selected allocation 없이 / stale snapshot 으로 주문 후보 금지.

  python -m main_mission.portfolio_os.daily_review --account 1
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .store import db as store_db
from . import decision as decision_mod
from . import market_context as market_ctx
from . import field_advisors as fa_mod
from . import profile as profile_mod
from . import evidence as evidence_mod
from . import decline_scan as decline_mod
from . import decline_policy_draft as decline_draft_mod
from . import portfolio_impact as impact_mod
from . import etf_analysis as etf_mod
from . import bond_bucket as bond_bucket_mod
from .growth import middleware as growth_mw

# ── 통합 소스(읽기 전용). 일부는 병렬 작업으로 늦게 도착할 수 있어 graceful import. ──
# 늦게 오거나 부재해도 daily_review 가 깨지지 않게 try/except 로 흡수한다(정직 "미연동").
try:  # 관점/견해
    from . import user_views as user_views_mod
except Exception:  # noqa: BLE001
    user_views_mod = None
try:  # 투자 목적
    from . import investor_objective as objective_mod
except Exception:  # noqa: BLE001
    objective_mod = None
try:  # 관점별 A/B/C 후보
    from . import perspective_variants as variants_mod
except Exception:  # noqa: BLE001
    variants_mod = None
try:  # 거시 변화 — 병렬 B 작업(아직 없을 수 있음). 없으면 graceful "거시 미연동".
    from . import macro_connect as macro_mod
except Exception:  # noqa: BLE001
    macro_mod = None
try:  # 자료(evidence) 요약 — 병렬 E 작업(늦게 올 수 있음). 없으면 graceful.
    from . import evidence_summary as evidence_summary_mod
except Exception:  # noqa: BLE001
    evidence_summary_mod = None
try:  # 분산축 수급(투자자별 매매동향) 로더 — 병렬 A 작업(늦게 올 수 있음). 없으면 graceful.
    from .decline import context as decline_ctx_mod
except Exception:  # noqa: BLE001
    decline_ctx_mod = None

PLAN_VALID_DAYS = 5  # 예약 계획 기본 유효기간(일)
CARRY_OVER_EXPIRE_DAYS = 5  # 직전 미체결 후보를 이 일수 넘게 끌면 만료(추격 금지)

# 스윙/헤지 점검 규칙 — 헤지비율 밴드(%). 과열↑·헤지 부족 → expand, 정상화 → reduce, 그 외 maintain.
SWING_HEDGE_LOW = 5.0    # 이 미만이면 헤지 부족
SWING_HEDGE_HIGH = 25.0  # 이 초과면 헤지 과다(정상화 필요)
SWING_DRIFT_HOT = 3.0    # 롱 drift 가 양(+)으로 이만큼이면 과열(롱 비중↑) 신호

# 수급(분산축) 점검 — 투자자별 매매동향 집계 기준(설명 중심, 단정 금지).
FLOW_WINDOW_DAYS = 5         # 최근 N일 순매수 흐름을 본다(일·주 단위 판단)
FLOW_MIN_DAYS = 3           # 유효 데이터 최소 일수(미만이면 정직하게 '판단 제외')
FLOW_PERSIST_DAYS = 5       # 동반 순매도/순매수가 이만큼 이어지면 '지속' 신호


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _next_review_date() -> str:
    """다음 점검 시점(일·주 단위 판단) — 기본 다음 날 점검 권장."""
    from datetime import timedelta
    return (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()


def _latest_snapshot(conn, account_index):
    return conn.execute(
        "SELECT id FROM account_snapshots WHERE account_index=? ORDER BY id DESC LIMIT 1",
        (account_index,),
    ).fetchone()


def _bond_duration_pref(conn, account_index) -> str | None:
    """계좌의 현재 채권 듀레이션 선호(investor_profile.bond_duration_pref)."""
    row = conn.execute(
        "SELECT bond_duration_pref FROM investor_profile WHERE account_index=?",
        (account_index,),
    ).fetchone()
    return (row["bond_duration_pref"] if row else None) or None


def _duration_block(conn, account_index):
    """금리·경제 전망 → 채권 듀레이션 추천. 매 점검마다 산출 + snapshot 저장.

    반환: (market_context_id, duration_recommendation dict, context dict).
    데이터 소스 미연동이면 추천은 보수적 기본값(불확실)이며 honest 라벨이 붙는다.
    """
    context = market_ctx.current_context()
    pref = _bond_duration_pref(conn, account_index)
    rec = market_ctx.recommend_duration(context, current_pref=pref)
    mc_id = market_ctx.save_snapshot(context)
    if not context.get("data_connected"):
        rec["no_data_note"] = "금리·경제 데이터 소스 미연동 — 추천은 보수적 기본값(불확실) 기준"
    return mc_id, rec, context


# 듀레이션 한글 라벨(국채 점검 블록 전용 — market_context.DURATION_KO 와 동일).
DUR_KO_BLOCK = {"short": "단기", "intermediate": "중기", "long": "장기", "mixed": "사다리(혼합)"}

# ── 국채(govbond) 점검 임계 — 재검토 후보 띄움(자동 변경 금지). ──
GOVBOND_LONG_HEAVY_PCT = 60.0   # 국채 중 장기 비중이 이 % 초과면 '장기채 변동성 과도' 후보
GOVBOND_CASH_MIN_PCT = 10.0     # 순현금이 이 % 미만이면 '현금 부족' 후보(유동성)
GOVBOND_LONG_ABS_HOT = 12.0     # 장기국채 전체% 가 이만큼이면 변동성 노출 주의 후보


def _govbond_block(conn, account_index: int, *, macro: dict,
                   duration_rec: dict, swing_hedge: dict | None) -> dict:
    """오늘의 국채(govbond) 점검 — 현재 국채 비중이 계좌 목적/금리환경에 맞는지 재점검.

    CEO 원칙(불변):
      - 국채 bucket 은 **한 번 정하면 끝이 아니다** — Daily/월간 점검에서 재검토한다.
      - 단, **자동 변경 금지**(auto_order:false, auto_applied:false). 재검토 '후보'까지만.
      - 국채 ETF 는 **방어자산 구현 수단**(수익 극대화 아님)임을 명시.
      - 데이터 없으면 정직하게 '미연동'(가짜 0/단정 금지) — graceful.

    점검 축:
      현재 국채/순현금/단기·장기 비중 · 단/장 비율 적정성 · 장기채 변동성 과도 ·
      현금 부족(유동성) · 환율 부담(미국채=USD 노출) · 위험자산 진입속도 충돌 ·
      금리환경 vs 듀레이션 추천. 금리/환율/VIX/FOMC·금통위 변화 시 재검토 후보 띄움.
    """
    # 현재 방어 구성(확정안이 있으면 truth, 없으면 프로필 미리보기) — 읽기 전용.
    try:
        bd = bond_bucket_mod.defensive_breakdown(account_index)
    except Exception as e:  # noqa: BLE001 — 분해 실패는 점검을 막지 않음(정직)
        return {
            "data_available": False, "candidates": [], "checks": [],
            "auto_order_created": False, "auto_applied": False,
            "requires_user_approval": True, "broker_neutral": True,
            "note": f"국채 비중 분해 오류 — 정직하게 점검 보류: {e}",
        }
    if not bd.get("ok"):
        return {
            "data_available": False, "candidates": [], "checks": [],
            "auto_order_created": False, "auto_applied": False,
            "requires_user_approval": True, "broker_neutral": True,
            "note": "국채 비중 데이터 없음 — 점검 보류(정직).",
        }

    b = bd.get("breakdown") or {}
    govbond = float(b.get("govbond_pct") or 0.0)        # 국채 전체%
    pure_cash = float(b.get("pure_cash_pct") or 0.0)    # 순현금 전체%
    short_gov = float(b.get("short_govbond_pct") or 0.0)
    long_gov = float(b.get("long_govbond_pct") or 0.0)
    risk_pct = float(b.get("risk_asset_pct") or 0.0)
    duration_pref = b.get("duration_pref")
    confirmed = bool(bd.get("confirmed"))

    # 단/장 비율(국채 내부) — 국채가 있을 때만 의미.
    long_share = round(long_gov / govbond * 100, 1) if govbond > 0 else None

    # 거시(금리/환율) 연동 여부 — macro_connect 가 늦으면 graceful 미연동.
    macro_connected = bool(macro.get("connected"))
    indicators = macro.get("indicators") or {}
    has_fx = any(k for k in indicators if "fx" in str(k).lower() or "usd" in str(k).lower() or "krw" in str(k).lower())
    rate_regime = duration_rec.get("rate_outlook")  # rising|falling|uncertain
    rate_connected = bool(duration_rec.get("data_connected"))

    checks: list[dict] = []   # 점검 결과(통과/주의) — 설명 중심
    candidates: list[dict] = []  # 재검토 후보(전부 후보, 자동 변경 0)

    def _add_check(key: str, ok: bool, msg: str) -> None:
        checks.append({"key": key, "status": "ok" if ok else "review", "msg": msg})

    # 1) 국채를 보유 중인지(0이면 점검 대상 적음 — 정직).
    if govbond <= 0:
        checks.append({"key": "no_govbond", "status": "ok",
                       "msg": "현재 국채 비중 0% — 방어는 순현금 중심. 금리 인하 기대 시 일부 국채 편입을 검토할 수 있습니다(후보)."})

    # 2) 장기채 변동성 과도(국채 내 장기 비중 과다 + 절대 노출).
    if govbond > 0 and long_share is not None and long_share > GOVBOND_LONG_HEAVY_PCT:
        candidates.append({
            "kind": "long_bond_volatility",
            "candidate": f"국채 중 장기 비중 {long_share}%(>{GOVBOND_LONG_HEAVY_PCT}%) — 장기국채 변동성 과도 가능. "
                         "단기 비중 확대(완충)를 재검토(후보).",
            "auto": False,
        })
        _add_check("long_share", False,
                   f"장기국채 비중 {long_share}% — 안전자산처럼 보이지만 가격 변동이 큽니다(재검토 후보).")
    elif govbond > 0 and long_share is not None:
        _add_check("long_share", True, f"국채 내 단/장 비율(장기 {long_share}%)은 과도하지 않습니다.")
    if long_gov >= GOVBOND_LONG_ABS_HOT:
        candidates.append({
            "kind": "long_bond_exposure",
            "candidate": f"장기국채 전체 비중 {round(long_gov, 1)}% — 금리 급변 시 평가손익 변동이 큽니다. "
                         "노출 적정성 재검토(후보).",
            "auto": False,
        })

    # 3) 현금 부족(유동성) — 순현금이 임계 미만이면 즉시 매수여력 점검 후보.
    if pure_cash < GOVBOND_CASH_MIN_PCT:
        candidates.append({
            "kind": "low_pure_cash",
            "candidate": f"순현금 {round(pure_cash, 1)}%(<{GOVBOND_CASH_MIN_PCT}%) — 국채로 방어를 채웠어도 "
                         "즉시 매수여력(유동성)이 얇을 수 있습니다. 순현금 확보 재검토(후보).",
            "auto": False,
        })
        _add_check("pure_cash", False, f"순현금 {round(pure_cash, 1)}% — 유동성 점검 후보.")
    else:
        _add_check("pure_cash", True, f"순현금 {round(pure_cash, 1)}% — 유동성 여력 확보(관망).")

    # 4) 환율 부담 — 미국채(USD 노출) 여부는 확정안 종목 매핑이 아니라 후보 수준으로 정직 표기.
    if not macro_connected or not has_fx:
        _add_check("fx", True,
                   "환율(USDKRW) 데이터 미연동 — 미국채 보유 시 환 부담을 단정하지 않습니다(정직). "
                   "미국 국채는 USD 노출이 있어 환율 변동이 평가손익에 더해집니다(일반 원칙).")
    else:
        candidates.append({
            "kind": "fx_review",
            "candidate": "환율(USDKRW) 변화 감지 — 미국 국채 보유 시 환 부담 재검토(후보). "
                         "원/달러 변동이 미국채 평가손익에 더해집니다.",
            "auto": False,
        })

    # 5) 금리환경 vs 듀레이션 추천 충돌 — 추천과 현재 선호가 다르면 재검토 후보.
    rec_dur = duration_rec.get("recommended")
    if duration_pref and rec_dur and duration_pref != rec_dur and rate_connected:
        candidates.append({
            "kind": "duration_vs_regime",
            "candidate": f"금리환경({rate_regime}) 기준 듀레이션 추천은 '{DUR_KO_BLOCK.get(rec_dur, rec_dur)}' "
                         f"인데 현재 선호는 '{DUR_KO_BLOCK.get(duration_pref, duration_pref)}' — 단/장 비율 재검토(후보).",
            "auto": False,
        })
        _add_check("duration_regime", False,
                   f"금리환경과 듀레이션 선호 불일치(추천 {DUR_KO_BLOCK.get(rec_dur, rec_dur)}) — 재검토 후보.")
    elif not rate_connected:
        _add_check("duration_regime", True,
                   "금리·경제 데이터 미연동 — 듀레이션은 보수적 기본값 기준이며 단정하지 않습니다(정직).")
    else:
        _add_check("duration_regime", True, "현재 듀레이션 선호가 금리환경 추천과 큰 충돌 없음.")

    # 6) 위험자산 진입속도 충돌 — 위험자산이 크고 순현금이 얇으면 진입 속도 조절 후보.
    if risk_pct >= 60.0 and pure_cash < GOVBOND_CASH_MIN_PCT:
        candidates.append({
            "kind": "entry_speed_vs_defense",
            "candidate": f"위험자산 {round(risk_pct, 1)}% · 순현금 {round(pure_cash, 1)}% — 진입 속도가 방어(유동성)와 "
                         "충돌할 수 있습니다. 분할·지정가 진입 속도 재검토(후보).",
            "auto": False,
        })

    # 7) 거시(금리/환율/VIX) 변화 — macro 변화가 있으면 재검토 트리거(후보).
    macro_changes = macro.get("changes") or []
    if macro_connected and macro_changes:
        candidates.append({
            "kind": "macro_change",
            "candidate": "거시(금리/환율/지수) 변화 감지 — 국채 비중·단장 비율을 금리환경에 맞춰 재검토(후보). "
                         "FOMC·금통위 등 이벤트 전후 변동성 유의.",
            "auto": False,
        })

    aligned = (not confirmed and govbond == 0) or len(candidates) == 0
    return {
        "data_available": True,
        "confirmed": confirmed,
        "source": bd.get("source"),
        "breakdown": {
            "govbond_pct": round(govbond, 1),
            "pure_cash_pct": round(pure_cash, 1),
            "short_govbond_pct": round(short_gov, 1),
            "long_govbond_pct": round(long_gov, 1),
            "long_share_pct": long_share,
            "risk_asset_pct": round(risk_pct, 1),
            "duration_pref": duration_pref,
        },
        "rate_regime": rate_regime,
        "rate_data_connected": rate_connected,
        "fx_data_connected": bool(macro_connected and has_fx),
        "checks": checks,
        "candidates": candidates,            # 재검토 후보(전부 후보·자동 변경 0)
        "candidate_count": len(candidates),
        "aligned": aligned,                  # 후보 없음 = 현 국채 구성 유지 가능(관망도 정상)
        "auto_order_created": False,         # 불변: 자동주문 0
        "auto_applied": False,               # 불변: 자동 policy/비중 변경 0
        "requires_user_approval": True,
        "broker_neutral": True,
        "note": ("국채 점검은 읽기 전용 재검토입니다 — 국채 bucket 은 한 번 정하면 끝이 아니라 금리/환율 "
                 "환경에 따라 재점검 대상입니다. 다만 자동 변경은 없습니다(전부 후보, 사람 승인). "
                 "국채 ETF 는 방어자산 구현 수단이며 수익 극대화 수단이 아닙니다. "
                 "데이터 없는 축은 정직하게 미연동으로 표기합니다."
                 if candidates else
                 "오늘 국채 점검 결과 재검토 후보가 없습니다 — 현 국채 구성 유지 가능(관망도 정상). "
                 "국채 ETF 는 방어자산 구현 수단(수익 극대화 아님)이며, 자동 변경은 없습니다."),
    }


def _make_plan(conn, account_index, decision_id, needing_lines) -> int:
    """needs_adjust 라인 → 예약성 지정가 계획(+steps). 시장가 매수 금지(지정가만)."""
    cur = conn.execute(
        "INSERT INTO scheduled_order_plans(account_index, decision_id, status, valid_until, created_at) "
        "VALUES(?,?,?,?,?)",
        (account_index, decision_id, "pending_approval", None, _now()),
    )
    plan_id = cur.lastrowid
    for l in needing_lines:
        conn.execute(
            "INSERT INTO scheduled_order_steps(plan_id, ref, direction, total_pct, total_krw, "
            "cycle_pct, cycle_krw, remaining_pct, round_no, total_rounds, limit_price, on_unfilled, "
            "hold_condition, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, l.get("ref"), l.get("direction"), l.get("total_adjust_pct"),
             l.get("total_adjust_krw"), l.get("this_cycle_pct"), l.get("this_cycle_krw"),
             l.get("remaining_pct"), 1, l.get("split_rounds"),
             None,  # 지정가는 호가/체결 단계에서 — 없으면 다음 cycle 재평가
             "다음 점검 cycle 에서 재평가(추격 금지)", "지정가보다 불리하면 이번 회차 보류",
             "candidate", _now()),
        )
    return plan_id


def _profile_themes(account_index: int) -> list[str]:
    """investor_profile.interests_text → 테마 목록(allocation 과 동일 분해)."""
    prof = profile_mod.get(account_index) or {}
    import re
    parts = re.split(r"[,/·]| 및 |\s{2,}", prof.get("interests_text") or "")
    return [s.strip() for s in parts if s.strip()][:8]


def _theme_action(hedge_ratio_pct: float, long_drift: float) -> tuple[str, str]:
    """헤지비율·롱 drift 로 maintain|reduce|expand 판정.

    - 과열(롱 drift 양수로 큼)인데 헤지 부족 → expand(헤지 확대)
    - 헤지 과다(정상화) → reduce
    - 그 외 → maintain
    관망과 마찬가지로 'maintain' 도 정상 결과(주문 신호 아님 — 노출 점검일 뿐)."""
    overheating = long_drift >= SWING_DRIFT_HOT
    if hedge_ratio_pct > SWING_HEDGE_HIGH:
        return "reduce", f"헤지비율 {hedge_ratio_pct}% 가 정상 밴드 상단({SWING_HEDGE_HIGH}%) 초과 — 헤지 정상화(축소) 검토"
    if hedge_ratio_pct < SWING_HEDGE_LOW and overheating:
        return "expand", (f"롱 과열(drift +{long_drift}%) + 헤지비율 {hedge_ratio_pct}% 가 하단({SWING_HEDGE_LOW}%) 미만 "
                          "— 헤지 확대 검토")
    if hedge_ratio_pct < SWING_HEDGE_LOW:
        return "maintain", f"헤지비율 {hedge_ratio_pct}% 로 얇지만 과열 신호 없음 — 유지(관망)"
    return "maintain", f"헤지비율 {hedge_ratio_pct}% 가 정상 밴드({SWING_HEDGE_LOW}~{SWING_HEDGE_HIGH}%) 내 — 유지"


def _swing_hedge_block(account_index: int, dec: dict) -> dict:
    """mixed_swing 테마별 롱/헤지/순노출/총노출/헤지비율 + maintain|reduce|expand.

    롱/헤지 비중은 selected allocation lines(decision.compute 결과)에서 재사용하고,
    전체 net/gross/hedge_ratio 는 decision.compute 가 준 값을 그대로 쓴다(재계산 안 함).
    mixed_swing 테마가 없으면 themes=[] (honest 빈 상태) — 바로 주문 신호 아님(노출 점검만)."""
    lines = dec.get("lines", []) or []
    themes = _profile_themes(account_index)
    try:
        dirs = fa_mod.resolve_theme_directions(account_index, themes)
    except Exception:  # noqa: BLE001 — 방향 해석 실패는 빈 스윙 블록으로 흡수(정직)
        dirs = {}
    mixed = [t for t in themes if dirs.get(t) == "mixed_swing"]

    # allocation 매핑: 롱 tilt 는 ref==theme, 헤지는 ref==theme+" 인버스".
    def _line_pct(ref: str, role: str) -> tuple[float, float]:
        for ln in lines:
            if ln.get("ref") == ref and ln.get("role") == role:
                return float(ln.get("target_pct") or 0.0), float(ln.get("drift") or 0.0)
        return 0.0, 0.0

    out_themes = []
    for th in mixed:
        long_pct, long_drift = _line_pct(th, "long")
        hedge_pct, _hd = _line_pct(th + " 인버스", "hedge")
        net = round(long_pct - hedge_pct, 1)
        gross = round(long_pct + hedge_pct, 1)
        ratio = round((hedge_pct / long_pct * 100), 1) if long_pct > 0 else 0.0
        action, reason = _theme_action(ratio, round(long_drift, 1))
        out_themes.append({
            "theme": th, "long_pct": round(long_pct, 1), "hedge_pct": round(hedge_pct, 1),
            "net_pct": net, "gross_pct": gross, "hedge_ratio_pct": ratio,
            "action": action, "reason": reason,
        })

    # 전체(포트폴리오 합계) — decision.compute 결과 재사용.
    cash_cur = dec.get("cash_current_pct")
    bond_pct = round(sum(float(l.get("target_pct") or 0.0) for l in lines if l.get("role") == "bond"), 1)
    theme_exposure = round(sum(float(l.get("target_pct") or 0.0)
                              for l in lines if l.get("role") in ("long", "hedge")), 1)
    overall = {
        "today_net_pct": dec.get("net_exposure_pct"),
        "today_gross_pct": dec.get("gross_exposure_pct"),
        "today_hedge_ratio_pct": dec.get("hedge_ratio_pct"),
        "defensive_pct": round((float(cash_cur) if cash_cur is not None else 0.0) + bond_pct, 1),
        "cash_current_pct": cash_cur,
        "bond_target_pct": bond_pct,
        "theme_exposure_pct": theme_exposure,
    }
    return {
        "themes": out_themes,
        "overall": overall,
        "has_mixed_swing": len(out_themes) > 0,
        "note": ("스윙/헤지는 노출 점검입니다 — mixed_swing 자체는 주문 신호가 아닙니다. "
                 "주문 후보는 selected allocation + risk_passed + drift 기준 충족일 때만 생성됩니다."),
    }


def _carry_over_block(conn, account_index: int) -> dict:
    """직전 cycle 의 미체결(candidate/hold) 예약 step 을 다음 점검에서 재평가.

    **재평가일 뿐 자동 주문이 아니다.** 각 미체결 step 에 대해 이월(carry)/만료(expire)
    판정만 한다 — 추격 매수 금지(가격 추격 없이 다음 cycle 로 보류 or 만료).
    만료 기준: 생성된 plan 이 CARRY_OVER_EXPIRE_DAYS 일 초과면 expire(보류 누적 차단).
    오늘 새로 만들 plan 은 대상에서 제외(직전 cycle 잔여만)."""
    today = datetime.now(timezone.utc).date()
    rows = conn.execute(
        "SELECT s.id step_id, s.plan_id, s.ref, s.direction, s.total_pct, s.remaining_pct, "
        "s.round_no, s.total_rounds, s.status step_status, s.created_at step_created, "
        "p.status plan_status, p.created_at plan_created "
        "FROM scheduled_order_steps s JOIN scheduled_order_plans p ON p.id=s.plan_id "
        "WHERE p.account_index=? AND s.status IN ('candidate','hold') "
        "AND p.status IN ('pending_approval','approved') "
        "ORDER BY s.plan_id DESC, s.id DESC",
        (account_index,),
    ).fetchall()
    items: list[dict] = []
    expired_plan_ids: set[int] = set()
    for r in rows:
        # plan 나이로 만료 판정(추격 금지 — 오래 끈 후보는 재평가 후 만료).
        try:
            created = datetime.fromisoformat(r["plan_created"]).date()
            age_days = (today - created).days
        except (ValueError, TypeError):
            age_days = None
        expired = age_days is not None and age_days > CARRY_OVER_EXPIRE_DAYS
        verdict = "expire" if expired else "carry"
        if expired:
            expired_plan_ids.add(int(r["plan_id"]))
        items.append({
            "plan_id": int(r["plan_id"]),
            "step_id": int(r["step_id"]),
            "ref": r["ref"],
            "direction": r["direction"],
            "total_pct": r["total_pct"],
            "remaining_pct": r["remaining_pct"],
            "round_no": r["round_no"],
            "total_rounds": r["total_rounds"],
            "age_days": age_days,
            "verdict": verdict,
            "note": ("유효기간 초과 — 만료(추격 금지·재진입은 새 점검 후보로)"
                     if expired else "직전 미체결 — 다음 cycle 재평가(추격 금지, 자동 주문 아님)"),
        })
    # 만료된 step/plan 정리 — 재평가 결과 반영(주문 실행 아님, 후보 상태 전이만).
    for pid in expired_plan_ids:
        conn.execute("UPDATE scheduled_order_steps SET status='blocked' "
                     "WHERE plan_id=? AND status IN ('candidate','hold')", (pid,))
        conn.execute("UPDATE scheduled_order_plans SET status='expired' WHERE id=?", (pid,))
    if expired_plan_ids:
        conn.commit()
    return {
        "items": items,
        "carry_count": sum(1 for i in items if i["verdict"] == "carry"),
        "expire_count": len(expired_plan_ids),
        "note": ("직전 cycle 미체결 후보의 재평가 결과입니다 — 자동 주문이 아니며, "
                 "이월(carry)은 다음 점검에서 다시 평가하고 만료(expire)는 후보를 닫습니다(추격 금지)."),
    }


def _attach_evidence(conn, account_index: int, review_id: int, *, themes: list[str]) -> dict:
    """market_context/evidence 를 review 에 정직하게 연결.

    근거 메모리(evidence_documents)에서 계좌·테마 관련 근거를 회수해
    daily_review_evidence_links 로 링크한다. 근거가 없으면 빈 목록 + honest 표기
    (없는 근거를 만들어내지 않는다)."""
    linked: list[dict] = []
    seen: set[int] = set()
    try:
        for th in (themes or [None]):
            for ev in evidence_mod.recall_evidence(theme=th, account_index=account_index,
                                                    limit=5, conn=conn):
                eid = ev.get("id")
                if eid is None or eid in seen:
                    continue
                seen.add(eid)
                evidence_mod.link_evidence(eid, "daily_review", review_id,
                                           note="daily review 근거 연결", conn=conn)
                linked.append({
                    "evidence_id": eid, "theme": ev.get("theme"), "topic": ev.get("topic"),
                    "stance": ev.get("stance"), "eff_confidence": ev.get("eff_confidence"),
                    "summary": ev.get("summary"), "source_type": ev.get("source_type"),
                })
    except Exception:  # noqa: BLE001 — 근거 연결 실패는 review 를 막지 않음(정직 빈 목록)
        pass
    return {
        "links": linked,
        "has_evidence": len(linked) > 0,
        "note": ("연결된 외부 근거(뉴스/공시/리포트)입니다 — 근거는 입장(stance) 태깅일 뿐 "
                 "그 자체가 주문 신호가 아닙니다." if linked else
                 "연결된 외부 근거가 없습니다 — 근거 없는 판단은 정직하게 '근거 없음'으로 표기합니다."),
    }


def _decline_block(conn, account_index: int) -> dict:
    """하락 징후 점검 — 보유/관심 종목을 decline_scan 으로 스캔(읽기 전용·broker-neutral).

    account_snapshots(holdings) + universe_instruments 에서 종목을 모은다(브로커 직접 호출 없음).
    각 종목: 6축 가용성 / overall_confidence / 신뢰축·부족축·상충신호 / 위험점수.
    집합: 보수적 전환 '후보'(있으면) + 오늘의 조치(유지|관망|보수적 전환 제안). **자동주문: 없음.**
    일봉 없으면 not_enough_data 로 정직 표기(거짓 경보 금지). confidence 낮으면 단정 회피.
    """
    # 종목 수집(스냅샷 기반 — broker-neutral). 가장 최신 snapshot 의 holdings + active universe.
    codes: dict[str, dict] = {}
    snap = _latest_snapshot(conn, account_index)
    if snap:
        for h in conn.execute("SELECT ticker FROM holdings WHERE snapshot_id=?", (snap["id"],)).fetchall():
            codes[h["ticker"]] = {"instrument_code": h["ticker"], "sector": None}
    for u in conn.execute(
        "SELECT ticker, asset_class FROM universe_instruments WHERE account_index=? AND is_active=1",
        (account_index,)).fetchall():
        codes.setdefault(u["ticker"], {"instrument_code": u["ticker"], "sector": u["asset_class"]})

    if not codes:
        return {
            "scanned_count": 0, "names": [], "proposal": None,
            "today_action": "유지", "auto_order_created": False,
            "note": "보유/관심 종목이 없어 하락 징후 스캔 대상이 없습니다(관망).",
        }

    # 현재 현금밴드(읽기 전용 — 권고 상향 기준).
    cash_band = None
    try:
        from . import policy as policy_mod
        cash_band = policy_mod.compile_policy(account_index).get("cash_band")
    except Exception:  # noqa: BLE001 — 정책 컴파일 실패해도 스캔은 진행(정직)
        cash_band = None

    try:
        res = decline_mod.scan(list(codes.values()), account_index=account_index,
                               current_cash_band=cash_band)
    except Exception as e:  # noqa: BLE001 — 스캔 실패는 review 를 막지 않음(정직 표기)
        return {"scanned_count": len(codes), "names": [], "proposal": None,
                "today_action": "관망", "auto_order_created": False,
                "note": f"하락 징후 스캔 오류 — 정직하게 보류(관망): {e}"}

    # 종목별 요약(6축 가용성·신뢰도·신뢰/부족/상충·위험점수). 일봉 없으면 not_enough_data.
    names: list[dict] = []
    for s in res.get("scanned", []):
        if not s.get("ok"):
            names.append({"instrument_code": s.get("instrument_code"),
                          "status": "not_enough_data",
                          "note": "일봉 데이터 부족 — 정직하게 분석 불가(거짓 경보 금지)."})
            continue
        comp = s.get("composite") or {}
        meta = comp.get("metacognition") or {}
        n_missing = len(meta.get("data_missing_axes", []))
        n_axes = len(comp.get("axes", {})) or (len(comp.get("breakdown", [])) + n_missing)
        n_available = n_axes - n_missing
        names.append({
            "instrument_code": s.get("instrument_code"),
            "status": "ok",
            "risk_score": s.get("risk_score"),
            "holistic_risk": s.get("holistic_risk"),
            "overall_confidence": s.get("overall_confidence"),
            "axes_available": f"{n_available}/{n_axes}",
            "coverage": meta.get("coverage"),
            "reliable_axes": meta.get("reliable_axes", []),
            "data_missing_axes": meta.get("data_missing_axes", []),
            "conflicting_signals": meta.get("conflicting_signals", False),
            "conflict_detail": meta.get("conflict_detail", ""),
            "confidence_judgment": s.get("confidence_judgment"),
        })

    proposal = res.get("proposal")
    analyzed = res.get("summary", {}).get("analyzed", 0)
    if proposal:
        today_action = "보수적 전환 제안"
    elif analyzed == 0:
        today_action = "관망"  # 분석 가능한 종목 없음(데이터 부족) — 정직 관망
    else:
        today_action = "유지"  # 위험 낮음 — 현 운용기준 유지

    return {
        "scanned_count": len(codes),
        "analyzed": analyzed,
        "skipped_no_data": res.get("summary", {}).get("skipped_no_data"),
        "summary": res.get("summary"),
        "by_sector": res.get("by_sector"),
        "names": names,
        "proposal": proposal,                       # 보수적 전환 후보(없으면 None)
        "today_action": today_action,               # 유지 | 관망 | 보수적 전환 제안
        "auto_order_created": False,                # 명시: 주문 자동생성 없음
        "policy_draft": None,                       # draft 는 사람 흐름에서만 — review 는 제안까지(읽기 전용)
        "note": ("하락 징후 점검은 읽기 전용 제안입니다 — 주문/정책 자동변경 없음. "
                 "보수적 전환은 후보이며 사람 승인 후 policy draft→version 으로만 반영됩니다. "
                 "신뢰도가 낮은 종목은 단정 없이 관망/주의로 표기합니다."),
    }


# ============================================================
# 수급(분산축) — 투자자별 매매동향 요약 + 해석 (설명 중심·읽기 전용·자동주문 0)
# ============================================================

def _flow_sign_label(net: float) -> str:
    """순매수 부호 → 한글 라벨(단정 금지용 — '매수/매도'가 아니라 '순매수/순매도')."""
    if net > 0:
        return "순매수"
    if net < 0:
        return "순매도"
    return "중립"


def _supply_demand_block(conn, account_index: int) -> dict:
    """오늘의 수급(분산축) — 외국인/기관/개인 순매수 흐름 요약 + **해석**.

    데이터: investor_flows(외국인/기관/개인 net). 병렬 A 가 적재/로더를 만든다 — 미연동이면
    graceful 하게 "투자자별 매매동향 데이터 부족 — 수급 판단 제외"(가짜 0 금지).

    원칙(불변):
      - **설명 중심**: 외국인/기관/개인 흐름을 합산·요약하고 *해석*과 confidence 를 붙인다.
      - **단정 금지**: "외국인 순매도 = 매도" 식 단정 금지. 허용은 수급 악화 경고·진입 속도 조절
        후보·현금밴드 상향 후보·hedge 검토·추가 확인뿐.
      - **broker-neutral · 자동주문 0 · policy 변경 0.** 후보·해석까지만.
      - **정직 제외**: 데이터 없으면 판단을 만들지 않고 정직하게 '제외'한다.
    """
    base = {
        "data_available": False,
        "scanned_count": 0,
        "covered_count": 0,
        "names": [],
        "aggregate": None,
        "interpretation": [],
        "portfolio_impact": [],
        "candidates": [],            # 진입 속도 조절·현금밴드 상향·hedge 검토 등 — 전부 후보
        "confidence": 0.0,
        "auto_order_created": False,  # 불변: 자동주문 0
        "auto_applied": False,        # 불변: 자동 policy 0
        "requires_user_approval": True,
        "broker_neutral": True,
    }

    # A 로더 미연동 → 정직 제외(깨지지 않음).
    if decline_ctx_mod is None or not hasattr(decline_ctx_mod, "load_investor_flows"):
        base["note"] = ("투자자별 매매동향 데이터(분산축) 미연동 — 수급 판단 제외(정직). "
                        "investor_flows 적재 후 외국인/기관/개인 순매수 흐름을 해석합니다.")
        return base

    # 보유 + 활성 유니버스에서 종목 수집(broker-neutral — 브로커 직접 호출 없음).
    codes: list[str] = []
    snap = _latest_snapshot(conn, account_index)
    if snap:
        for h in conn.execute("SELECT ticker FROM holdings WHERE snapshot_id=?", (snap["id"],)).fetchall():
            codes.append(h["ticker"])
    for u in conn.execute(
        "SELECT ticker FROM universe_instruments WHERE account_index=? AND is_active=1",
        (account_index,)).fetchall():
        if u["ticker"] not in codes:
            codes.append(u["ticker"])

    if not codes:
        base["note"] = "보유/관심 종목이 없어 수급(분산축) 점검 대상이 없습니다(관망)."
        return base

    # 종목별 최근 흐름 로드 → 윈도 집계. 데이터 없는 종목은 정직하게 covered 에서 제외.
    names: list[dict] = []
    agg_foreign = 0.0
    agg_inst = 0.0
    agg_retail = 0.0
    persist_smart_sell = 0     # 윈도 내 외국인+기관 동반 순매도일 수
    persist_retail_buy = 0     # 윈도 내 개인 순매수일 수
    pension_supportive = False  # 기관(연기금 대용) 누적 순매수 → 방어 신호 후보
    covered = 0
    for code in codes:
        try:
            flows = decline_ctx_mod.load_investor_flows(conn, code, limit=FLOW_WINDOW_DAYS + 5) or []
        except Exception:  # noqa: BLE001 — 한 종목 로드 실패가 전체를 막지 않음(정직)
            flows = []
        # 최신순으로 정렬되어 들어올 수도, 과거→최신일 수도 있음 — 날짜로 정렬 후 최근 윈도.
        flows = sorted(flows, key=lambda r: r.get("trade_date", ""))
        window = flows[-FLOW_WINDOW_DAYS:]
        valid = [r for r in window if any(
            r.get(k) is not None for k in ("foreign_net", "institution_net", "retail_net"))]
        if len(valid) < FLOW_MIN_DAYS:
            names.append({"instrument_code": code, "status": "not_enough_data",
                          "note": "투자자별 매매동향 데이터 부족 — 수급 판단 제외(정직)."})
            continue
        covered += 1
        f_cum = sum(float(r["foreign_net"]) for r in valid if r.get("foreign_net") is not None)
        i_cum = sum(float(r["institution_net"]) for r in valid if r.get("institution_net") is not None)
        r_cum = sum(float(r["retail_net"]) for r in valid if r.get("retail_net") is not None)
        agg_foreign += f_cum
        agg_inst += i_cum
        agg_retail += r_cum
        smart_sell_days = sum(
            1 for r in valid
            if (float(r.get("foreign_net") or 0.0) + float(r.get("institution_net") or 0.0)) < 0)
        retail_buy_days = sum(1 for r in valid if float(r.get("retail_net") or 0.0) > 0)
        if smart_sell_days >= min(FLOW_PERSIST_DAYS, len(valid)):
            persist_smart_sell += 1
        if retail_buy_days >= min(FLOW_PERSIST_DAYS, len(valid)):
            persist_retail_buy += 1
        if i_cum > 0:
            pension_supportive = True
        names.append({
            "instrument_code": code,
            "status": "ok",
            "days": len(valid),
            "foreign_net_cum": round(f_cum, 1),
            "institution_net_cum": round(i_cum, 1),
            "retail_net_cum": round(r_cum, 1),
            "foreign": _flow_sign_label(f_cum),
            "institution": _flow_sign_label(i_cum),
            "retail": _flow_sign_label(r_cum),
            # 분산(distribution) 패턴: 스마트머니(외인+기관) 순매도 + 개인 순매수.
            "distribution_pattern": (f_cum + i_cum) < 0 and r_cum > 0,
        })

    if covered == 0:
        base["scanned_count"] = len(codes)
        base["names"] = names
        base["note"] = ("투자자별 매매동향 데이터 부족 — 수급 판단 제외(정직). "
                        "대상 종목의 investor_flows 가 최소 일수에 못 미칩니다(가짜 0 금지).")
        return base

    smart_cum = agg_foreign + agg_inst
    aggregate = {
        "window_days": FLOW_WINDOW_DAYS,
        "covered_count": covered,
        "foreign_net_cum": round(agg_foreign, 1),
        "institution_net_cum": round(agg_inst, 1),
        "retail_net_cum": round(agg_retail, 1),
        "smart_money_net_cum": round(smart_cum, 1),   # 외국인+기관 합산
        "foreign": _flow_sign_label(agg_foreign),
        "institution": _flow_sign_label(agg_inst),
        "retail": _flow_sign_label(agg_retail),
        "distribution_names": [n["instrument_code"] for n in names if n.get("distribution_pattern")],
    }

    # ── 해석(설명 중심·단정 금지). 흐름을 사람 말로 풀되 '매도=매도' 식 단정은 하지 않는다. ──
    interpretation: list[str] = []
    portfolio_impact: list[str] = []
    candidates: list[dict] = []

    dist_names = aggregate["distribution_names"]
    if smart_cum < 0 and agg_retail > 0:
        interpretation.append(
            f"최근 {FLOW_WINDOW_DAYS}일 외국인·기관 합산 순매도({smart_cum:+.0f}), 개인이 받아내는 "
            "흐름(개인 순매수) — 스마트머니가 빠지고 개인이 받는 '분산' 성격의 수급으로, 단기 수급은 "
            "취약할 수 있습니다(단정 아님 — 추세·가격과 함께 봐야 함).")
        portfolio_impact.append("스마트머니 이탈 구간은 신규 진입 시 *속도 조절* 검토 대상(분할·지정가).")
        candidates.append({"kind": "slow_entry",
                           "candidate": "신규 진입 속도 조절(분할·지정가) 검토", "auto": False})
        if dist_names:
            candidates.append({"kind": "hedge_review",
                               "candidate": f"분산 패턴 종목({', '.join(dist_names[:3])}) hedge 검토(인버스 한도 내)",
                               "auto": False})
    elif smart_cum > 0:
        interpretation.append(
            f"최근 {FLOW_WINDOW_DAYS}일 외국인·기관 합산 순매수({smart_cum:+.0f}) — 스마트머니 유입 성격의 "
            "수급(방어적 해석 가능, 단정 아님).")
        portfolio_impact.append("스마트머니 유입은 수급 측면 우호 신호 후보 — 다만 추세·밸류와 함께 확인 필요.")
    else:
        interpretation.append(
            f"최근 {FLOW_WINDOW_DAYS}일 투자자별 수급이 뚜렷한 한 방향이 아닙니다(혼조) — 수급만으로 단정 곤란.")

    if persist_smart_sell > 0:
        interpretation.append(
            f"외국인·기관 동반 순매도 지속(대상 {persist_smart_sell}종목) — 수급 취약 경고(추가 확인 권장).")
        candidates.append({"kind": "cash_band_up",
                           "candidate": "현금밴드 상향 후보(방어) 검토", "auto": False})
    if pension_supportive and smart_cum < 0:
        interpretation.append(
            "다만 기관 누적 순매수가 유지되는 종목이 있어 전면적 수급 악화로 단정하기는 이릅니다"
            "(기관 매수 유지면 방어 신호 후보).")

    # confidence: 커버리지(데이터 있는 종목 비율) + 윈도 충분성. 데이터 적으면 낮춤(정직).
    coverage = covered / max(1, len(codes))
    confidence = round(0.35 + 0.45 * coverage + (0.1 if covered >= 3 else 0.0), 2)
    confidence = min(0.9, confidence)  # 수급 단일 축은 단정 금지 — 상한 0.9

    base.update({
        "data_available": True,
        "scanned_count": len(codes),
        "covered_count": covered,
        "names": names,
        "aggregate": aggregate,
        "interpretation": interpretation,
        "portfolio_impact": portfolio_impact,
        "candidates": candidates,    # 전부 후보(주문/정책 변경 아님)
        "confidence": confidence,
        "note": ("수급(분산축)은 읽기 전용 설명입니다 — 외국인/기관/개인 순매수 흐름을 해석합니다. "
                 "'순매도=매도' 식 단정은 하지 않으며, 진입 속도 조절·현금밴드 상향·hedge 검토는 "
                 "전부 후보입니다(주문/정책 자동변경 없음). 데이터 부족 종목은 정직하게 제외했습니다."),
    })
    return base


# ============================================================
# 6축 상태 — 오늘의 기술·거시·분산·이벤트·심리·정책 종합 상태 (읽기 전용·자동주문 0)
# ============================================================

# 6축 한글 라벨(decline.axes.AXIS_LABELS 와 동일 — 미연동 축도 정직 표기).
SIX_AXES = ("technical", "distribution", "macro", "event", "sentiment", "policy")
SIX_AXIS_LABELS = {
    "technical": "기술", "distribution": "분산", "macro": "거시",
    "event": "이벤트", "sentiment": "심리", "policy": "정책/규제",
}


def _six_axis_block(*, decline: dict, macro: dict, supply_demand: dict) -> dict:
    """오늘의 6축 상태 종합 — 이미 계산된 블록(decline/macro/supply_demand)을 재사용해 정직 요약.

    축별: data_available(연동/미연동) · confidence · 주요 신호 · 미연동이면 명시.
    + 종목 종합 overall_confidence(가용 종목 평균) · holistic_risk(가용 종목 평균) · 포트폴리오 영향.

    원칙(불변):
      - **데이터 없는 축은 제외(정직)** — 가짜 점수/단정 0. 미연동 축은 그대로 '미연동' 표기.
      - confidence 낮으면 단정 회피(holistic_risk 가 높아도 '단정' 대신 '주의' 톤).
      - broker-neutral · 자동주문 0 · policy 변경 0 — 상태 요약과 후보까지만.
    재스캔하지 않는다(중복 비용 회피) — _decline_block 의 종목별 composite 를 집계한다.
    """
    names = [n for n in (decline.get("names") or []) if n.get("status") == "ok"]

    # 종목 composite 에서 축별 가용성/신뢰도/주요신호 집계(가용한 종목이 하나라도 있으면 축 가용).
    axis_avail: dict[str, bool] = {ax: False for ax in SIX_AXES}
    axis_conf_vals: dict[str, list[float]] = {ax: [] for ax in SIX_AXES}
    axis_signals: dict[str, set[str]] = {ax: set() for ax in SIX_AXES}
    for n in names:
        for ax in n.get("reliable_axes", []) or []:
            if ax in axis_avail:
                axis_avail[ax] = True
        # data_missing_axes 의 보수: 명시된 미연동 축은 가용에서 제외(이미 False 기본).

    # 종목 단위 신뢰도/위험 집계(정직 — 가용 종목만).
    confs = [n["overall_confidence"] for n in names if isinstance(n.get("overall_confidence"), (int, float))]
    risks = [n["holistic_risk"] for n in names if isinstance(n.get("holistic_risk"), (int, float))]
    overall_conf = round(sum(confs) / len(confs), 3) if confs else None
    holistic_risk = round(sum(risks) / len(risks), 1) if risks else None

    # 거시축: macro 블록 연동 여부/변화로 직접 보강(종목 composite 미연동이어도 계좌 공통 거시는 알 수 있음).
    macro_connected = bool(macro.get("connected"))
    macro_changes = macro.get("changes") or []
    if macro_connected:
        axis_avail["macro"] = True

    # 분산축(수급): supply_demand 블록 연동 여부로 보강.
    sd_available = bool(supply_demand.get("data_available"))
    if sd_available:
        axis_avail["distribution"] = True

    # 축별 정직 요약 구성.
    axes_out: list[dict] = []
    for ax in SIX_AXES:
        avail = axis_avail[ax]
        signals: list[str] = []
        conf = None
        cvals = axis_conf_vals[ax]
        if cvals:
            conf = round(sum(cvals) / len(cvals), 3)
        if ax == "macro" and macro_connected:
            lean = macro.get("lean")
            if lean:
                signals.append(f"거시 기울기: {lean}")
            for c in macro_changes[:3]:
                msg = c.get("note") or c.get("signal") or c.get("label") if isinstance(c, dict) else str(c)
                if msg:
                    signals.append(str(msg))
        if ax == "distribution" and sd_available:
            agg = supply_demand.get("aggregate") or {}
            smart = agg.get("smart_money_net_cum")
            if smart is not None:
                signals.append(f"스마트머니(외인+기관) 누적 {smart:+.0f}")
            conf = supply_demand.get("confidence", conf)
        # 종목 composite 의 reliable_axes 에 자주 잡힌 축이면 '신호 관측됨' 정도만(단정 금지).
        if avail and not signals:
            cnt = sum(1 for n in names if ax in (n.get("reliable_axes") or []))
            if cnt:
                signals.append(f"가용 종목 {cnt}개에서 신호 관측(단정 아님)")
        axes_out.append({
            "axis": ax,
            "label": SIX_AXIS_LABELS[ax],
            "data_available": avail,
            "confidence": conf,
            "signals": signals,
            "note": ("연동됨" if avail else "미연동 — 데이터 없음(분석 제외, 정직)"),
        })

    available_axes = [a["label"] for a in axes_out if a["data_available"]]
    missing_axes = [a["label"] for a in axes_out if not a["data_available"]]

    # 포트폴리오 영향(읽기 전용·단정 금지). holistic_risk + confidence 로 톤 결정.
    impact: list[str] = []
    if holistic_risk is None or overall_conf is None:
        impact.append("종목 6축 종합을 낼 데이터가 부족 — 단정 없이 관망/추가확인(정직).")
    elif overall_conf < 0.3:
        impact.append(f"종합 위험 {holistic_risk:.0f}(가용 종목 평균)이나 신뢰도 {overall_conf:.2f}로 낮음 "
                      "— 단정 금지, 관망/주의·데이터 추가 필요.")
    elif holistic_risk >= 35:
        impact.append(f"종합 위험 {holistic_risk:.0f}(신뢰도 {overall_conf:.2f}) — 방어(현금/채권) 비중 점검 "
                      "후보·진입 속도 조절 검토(주문 아님, 사람 승인).")
    else:
        impact.append(f"종합 위험 {holistic_risk:.0f}(신뢰도 {overall_conf:.2f}) — 현 운용기준 유지 가능(관망도 정상).")
    if missing_axes:
        impact.append(f"미연동 축({', '.join(missing_axes)})은 분석에서 제외 — '모든 데이터를 고려했다'고 말하지 않음.")

    return {
        "axes": axes_out,
        "available_axes": available_axes,
        "missing_axes": missing_axes,
        "available_count": len(available_axes),
        "total_axes": len(SIX_AXES),
        "overall_confidence": overall_conf,   # 가용 종목 평균(없으면 None)
        "holistic_risk": holistic_risk,       # 가용 종목 평균(없으면 None)
        "analyzed_names": len(names),
        "portfolio_impact": impact,           # 읽기 전용·단정 금지
        "auto_order_created": False,          # 불변: 자동주문 0
        "auto_applied": False,                # 불변: 자동 policy 0
        "requires_user_approval": True,
        "broker_neutral": True,
        "note": ("오늘의 6축 상태(기술·거시·분산·이벤트·심리·정책) 요약입니다 — 데이터 없는 축은 "
                 "정직하게 제외했고, 신뢰도가 낮으면 단정하지 않습니다. 주문/정책 자동변경 없음(전부 후보)."),
    }


# ============================================================
# 통합 섹션 — 관점/거시/관점별 A·B·C/물어볼 질문 (전부 읽기 전용·자동주문 0)
# ============================================================

def _user_views_block(account_index: int) -> dict:
    """오늘의 사용자 관점 요약 — user_views(견해) + investor_objective(목적).

    읽기 전용. 모듈이 없거나 데이터가 없으면 정직하게 '관점 미입력/목적 미설정' 표기.
    견해는 1급 입력이지만 *단정이 아님* — 데이터와 별개로 제시한다(자동 적용 0)."""
    views: list[dict] = []
    if user_views_mod is not None:
        try:
            views = user_views_mod.list_views(account_index, status="active") or []
        except Exception:  # noqa: BLE001 — 견해 조회 실패는 빈 목록(정직)
            views = []
    by_layer: dict[str, list[dict]] = {}
    for v in views:
        by_layer.setdefault(v.get("layer") or "기타", []).append({
            "theme": v.get("theme"), "ticker": v.get("ticker"), "etf": v.get("etf"),
            "stance": v.get("stance"), "conviction": v.get("conviction"),
            "horizon": v.get("horizon"), "note": v.get("note"),
        })

    objective = None
    objective_set = False
    if objective_mod is not None:
        try:
            objective = objective_mod.get(account_index)
            objective_set = bool(objective and objective.get("investment_goal"))
        except Exception:  # noqa: BLE001 — 목적 미설정/실패는 graceful
            objective = None

    if not views and not objective_set:
        note = ("저장된 관점(user_views)·투자 목적(investor_objective)이 없습니다 — "
                "관점/목적을 먼저 입력하면 더 정교한 점검이 됩니다(가정하지 않음).")
    else:
        note = ("사용자 관점은 1급 입력이지만 단정이 아닙니다 — 데이터와 별개로 제시하며 "
                "자동 적용되지 않습니다(사람 승인 흐름 유지).")
    return {
        "views_count": len(views),
        "by_layer": by_layer,
        "objective": objective,
        "objective_set": objective_set,
        "has_views": len(views) > 0,
        "note": note,
    }


def _macro_block(account_index: int) -> dict:
    """오늘의 주요 거시 변화 — macro_connect(병렬 B). 미연동이면 정직 graceful.

    macro_connect 가 아직 없을 수 있다(병렬 작업 중). 없으면 거짓 거시 수치를 만들지 않고
    '거시 미연동'으로 표기한다(자동주문/단정 없음). 거시는 후보 신호일 뿐 — 자동적용 0.
    snapshot(지표) + macro_to_portfolio(사람이 읽는 해석/방어·공격 기울기)를 묶는다."""
    if macro_mod is None or not hasattr(macro_mod, "macro_snapshot"):
        return {
            "connected": False, "changes": [], "summary": None,
            "note": "거시(macro) 미연동 — macro_connect 도착 전(정직 표기). "
                    "거시 수치를 가정/생성하지 않습니다.",
        }
    try:
        snap = macro_mod.macro_snapshot() or {}
    except Exception as e:  # noqa: BLE001 — 거시 조회 실패는 review 를 막지 않음(정직)
        return {"connected": False, "changes": [], "summary": None,
                "note": f"거시 조회 오류 — 정직하게 미연동 처리: {e}"}
    data_available = bool(snap.get("data_available"))
    if not data_available:
        return {
            "connected": False, "changes": [], "summary": None,
            "as_of": snap.get("as_of"), "indicators": {},
            "note": snap.get("note") or "거시 데이터 비어 있음 — 정직하게 미연동으로 표기.",
        }
    # 거시 → 포트폴리오 해석(후보 신호). 실패해도 지표 자체는 표시(정직).
    signals: list[dict] = []
    lean = None
    defensive_score = None
    try:
        m2p = macro_mod.macro_to_portfolio(snap) or {}
        signals = m2p.get("signals", []) or []
        lean = m2p.get("lean")
        defensive_score = m2p.get("defensive_score")
    except Exception:  # noqa: BLE001 — 매핑 실패는 지표만 표기(정직)
        signals = []
    return {
        "connected": True,
        "as_of": snap.get("as_of"),
        "indicators": snap.get("indicators", {}),
        "fresh_count": snap.get("fresh_count"),
        "stale_count": snap.get("stale_count"),
        "changes": signals,                 # 사람이 읽는 거시 변화/해석(후보)
        "lean": lean,                       # 거시가 가리키는 방어/공격 기울기(후보)
        "defensive_score": defensive_score,
        "summary": snap.get("note"),
        "auto_applied": False,              # 거시는 후보 — 자동 policy 변경 0
        "requires_user_approval": True,
        "note": ("연동된 거시 변화 요약입니다 — 그 자체가 주문 신호가 아니며 관점/하락신호와 "
                 "함께 사람 판단의 입력입니다(자동적용 0)."),
    }


def _perspective_block(account_index: int) -> dict:
    """오늘의 관점별 A/B/C 후보 — perspective_variants.generate(읽기 전용으로 호출).

    A=현재 관점 best, B=방어적, C=공격적. **하나의 정답이 아님.** draft 저장 없이
    (save_draft=False) 후보만 받아 표시한다 — 자동 적용/주문 0, 사람 선택·승인 필요.
    모듈 부재/실패 시 정직 빈 상태."""
    if variants_mod is None:
        return {"connected": False, "candidates": [], "objective_set": False,
                "auto_order_created": False, "requires_user_approval": True,
                "note": "관점별 후보(perspective_variants) 미연동 — 후보 미생성(정직)."}
    try:
        out = variants_mod.generate(account_index, save_draft=False)
    except Exception as e:  # noqa: BLE001 — 후보 생성 실패는 review 를 막지 않음(정직)
        return {"connected": False, "candidates": [], "objective_set": False,
                "auto_order_created": False, "requires_user_approval": True,
                "note": f"관점별 후보 생성 오류 — 정직하게 미생성: {e}"}
    if not out.get("ok"):
        return {"connected": False, "candidates": [], "objective_set": False,
                "auto_order_created": False, "requires_user_approval": True,
                "note": f"관점별 후보 생성 불가: {out.get('error')}"}
    # 각 안의 핵심만 추려 표시(전체 rows 는 무겁다 — 요약 비중·이유만).
    cands = []
    for c in out.get("candidates", []):
        cands.append({
            "perspective": c.get("perspective"), "label": c.get("label"),
            "summary": c.get("summary"), "weights": c.get("weights"),
            "why_fits_user": c.get("why_fits_user"),
            "pros": c.get("pros"), "risks": c.get("risks"),
            "break_triggers": c.get("break_triggers"),
        })
    obj = out.get("objective") or {}
    return {
        "connected": True,
        "candidates": cands,                       # A/B/C (요약)
        "objective_set": bool(obj.get("set")),
        "views_summary": out.get("views_summary"),
        "auto_order_created": False,
        "auto_applied": False,
        "requires_user_approval": True,
        "note": ("관점별 후보 3안(A=현재 관점·B=방어·C=공격)입니다 — 하나의 정답이 아니라 "
                 "관점에 따른 해석입니다. 전부 후보이며 사람이 한 안을 골라 승인해야 반영됩니다."),
    }


def _evidence_summary_block(account_index: int) -> dict:
    """오늘의 자료 요약 — evidence_summary.evidence_for_account(병렬 E). 미연동이면 graceful.

    evidence_summary 모듈이 늦게 올 수 있다. 없으면 '자료 요약 미연동'으로 정직 표기하고,
    기존 evidence 링크(_attach_evidence)는 별도로 유지된다(이 블록은 보강일 뿐)."""
    if evidence_summary_mod is None or not hasattr(evidence_summary_mod, "evidence_for_account"):
        return {"connected": False, "items": [], "conflicts": [],
                "note": "자료 요약(evidence_summary) 미연동 — 도착 전(정직 표기)."}
    try:
        out = evidence_summary_mod.evidence_for_account(account_index, limit=10)
    except Exception as e:  # noqa: BLE001 — 자료 요약 실패는 review 를 막지 않음(정직)
        return {"connected": False, "items": [], "conflicts": [],
                "note": f"자료 요약 오류 — 정직하게 미연동 처리: {e}"}
    out = out or {}
    items = out.get("items", []) or []
    return {
        "connected": True,
        "items": items,
        "conflicts": out.get("conflicts", []),
        "stale_count": out.get("stale_count"),
        "data_source_status": out.get("data_source_status"),
        "has_evidence": len(items) > 0,
        "note": ("내 보유/관심 관련 자료 요약입니다 — 입장(stance) 태깅일 뿐 주문 신호가 아닙니다."
                 if items else
                 "연결된 자료가 없습니다 — 근거 없는 단정은 하지 않습니다(정직)."),
    }


def _today_questions_block(account_index: int, *, decline: dict, synthesis: dict,
                           swing_hedge: dict | None, user_views: dict,
                           macro: dict, perspective: dict,
                           supply_demand: dict | None = None) -> dict:
    """오늘 사용자에게 물어볼 질문 — **단정 아닌 선택지 질문**.

    하락 징후·견해 vs 데이터 충돌·mixed_swing 과열·거시 변화·관점 미입력 등에서
    "늦출까요 / 헤지 검토할까요 / 유지할까요" 식 **선택지 질문**을 만든다.
    질문은 그 자체로 주문/정책 변경이 아니다 — 사람의 선택·승인을 구하는 것이다."""
    questions: list[dict] = []

    # 1) 하락 보수적 전환 후보 → 선택지 질문.
    if decline.get("proposal"):
        questions.append({
            "topic": "decline",
            "question": "하락 징후가 잡힌 종목이 있습니다 — 보수적 전환(현금/방어↑)을 검토할까요, "
                        "유지할까요, 더 지켜볼까요?",
            "options": ["보수적 전환 검토", "현 비중 유지", "더 관찰(관망)"],
        })

    # 2) 견해 vs 데이터 충돌 → 선택지 질문(단정 금지).
    if (synthesis.get("conflicts_count") or 0) > 0:
        conf_codes = [v.get("instrument_code") for v in synthesis.get("view_vs_data", [])
                      if v.get("alignment") == "conflict"]
        codes_txt = ", ".join(filter(None, conf_codes[:3])) or "일부 종목"
        questions.append({
            "topic": "view_vs_data_conflict",
            "question": f"견해와 데이터가 충돌하는 종목({codes_txt})이 있습니다 — 견해를 유지할까요, "
                        "데이터에 맞춰 비중을 조정할까요, 헤지로 양쪽을 절충할까요?",
            "options": ["견해 유지", "데이터 반영 조정", "헤지 절충(인버스 한도 내)"],
        })

    # 3) mixed_swing 과열(롱↑·헤지 부족) → 선택지 질문.
    if swing_hedge:
        for th in swing_hedge.get("themes", []):
            if th.get("action") == "expand":
                questions.append({
                    "topic": "mixed_swing_overheat",
                    "question": f"{th.get('theme')} 단기 과열 신호 — 신규매수를 늦출까요, 헤지를 "
                                "확대할까요, 현 노출을 유지할까요?",
                    "options": ["신규매수 보류", "헤지 확대 검토", "현 노출 유지"],
                })

    # 3.5) 수급 악화(외국인·기관 동반 순매도+개인 순매수) → 선택지 질문(단정 금지).
    if supply_demand and supply_demand.get("data_available"):
        agg = supply_demand.get("aggregate") or {}
        smart = agg.get("smart_money_net_cum")
        if smart is not None and smart < 0 and (agg.get("retail_net_cum") or 0) > 0:
            questions.append({
                "topic": "supply_demand",
                "question": "외국인·기관 합산 순매도 흐름이 보입니다(개인이 받는 분산 성격) — 신규 진입 "
                            "속도를 늦출까요, 현금/방어 비중을 올릴까요, 더 지켜볼까요?",
                "options": ["진입 속도 조절", "현금밴드 상향 검토", "더 관찰(관망)"],
            })

    # 4) 거시 변화 — 연동됐고 변화가 있으면 선택지 질문.
    if macro.get("connected") and macro.get("changes"):
        questions.append({
            "topic": "macro",
            "question": "거시 변화가 감지됐습니다 — 방어 비중(현금/채권)을 늘릴까요, 현 운용을 "
                        "유지할까요, 관점별 후보(B 방어안)를 검토할까요?",
            "options": ["방어 비중 확대", "현 운용 유지", "B(방어) 후보 검토"],
        })

    # 5) 관점/목적 미입력 → 입력 요청 질문(가정 금지).
    if not user_views.get("has_views"):
        questions.append({
            "topic": "missing_views",
            "question": "저장된 투자 관점(견해)이 없습니다 — 관심 테마에 대한 생각을 입력하시겠어요? "
                        "(입력하면 관점별 후보가 더 정교해집니다)",
            "options": ["관점 입력", "지금은 생략"],
        })
    if not user_views.get("objective_set"):
        questions.append({
            "topic": "missing_objective",
            "question": "투자 목적이 미설정입니다 — '손실 축소/배당/성장/안정 운용' 등 무엇을 "
                        "최선으로 보시나요? (목적에 따라 '최선'의 기준이 달라집니다)",
            "options": ["목적 설정", "지금은 생략"],
        })

    # 6) 관점별 후보가 생성됐으면 어느 안을 볼지 질문(자동 선택 금지).
    if perspective.get("connected") and perspective.get("candidates"):
        questions.append({
            "topic": "perspective_choice",
            "question": "오늘의 관점별 후보(A=현재·B=방어·C=공격)가 준비됐습니다 — 어느 관점을 "
                        "기준으로 검토하시겠어요? (하나를 골라 승인해야 반영됩니다)",
            "options": ["A(현재 관점)", "B(방어)", "C(공격)", "오늘은 보류"],
        })

    if not questions:
        questions.append({
            "topic": "none",
            "question": "오늘은 특이 신호가 없습니다 — 현 운용기준을 유지할까요, 관점별 후보를 한번 "
                        "검토해볼까요?",
            "options": ["현 운용 유지", "관점별 후보 검토"],
        })

    return {
        "questions": questions,
        "count": len(questions),
        "note": ("오늘 사용자에게 물어볼 질문입니다 — 단정이 아니라 선택지 질문입니다. "
                 "질문 자체는 주문/정책 변경이 아니며, 사람의 선택·승인을 구합니다."),
    }


def _conservative_candidates_block(decline: dict, synthesis: dict) -> dict:
    """오늘의 보수적 전환 후보 — 하락/영향에서 방어 쪽 후보만 모은다(전부 후보, 주문 아님).

    하락 보수적 전환 제안 + synthesis 의 방어성 조정 후보(현금밴드↑·위험축소·헤지·매수보류)를
    모아 한곳에 보여준다. 자동 적용/주문 0 — 사람 승인 흐름 유지."""
    candidates: list[dict] = []
    if decline.get("proposal"):
        candidates.append({"source": "decline", "kind": "conservative_shift",
                           "detail": decline.get("proposal")})
    defensive_kinds = {"cash_band_raise", "reduce_risk_assets", "consider_hedge",
                       "slow_new_buy", "staged_buy", "slow_rebalance", "reduce_theme_exposure"}
    for c in synthesis.get("today_adjustment_candidates", []) or []:
        if c.get("kind") in defensive_kinds:
            candidates.append({"source": "synthesis", "kind": c.get("kind"), "detail": c})
    return {
        "candidates": candidates,
        "count": len(candidates),
        "auto_order_created": False,
        "auto_applied": False,
        "requires_user_approval": True,
        "note": ("보수적(방어) 전환 후보입니다 — 전부 후보이며 자동 적용/주문 없음. "
                 "사람 승인 후에만 policy draft→version 으로 반영됩니다." if candidates else
                 "오늘은 보수적 전환 후보가 없습니다(현 운용기준 유지·관망도 정상)."),
    }


def _integration_confidence(*, data_ok: bool, macro: dict, user_views: dict,
                            evidence_summary: dict, decline: dict) -> dict:
    """통합 점검의 confidence(low/medium/high) — 데이터 부족할수록 낮춘다(정직).

    스냅샷/선택안 없음(data_ok=False)·거시 미연동·관점 미입력·자료 없음·일봉 부족이
    겹칠수록 낮아진다. 단정 회피의 근거(=낮을수록 '관망/추가확인' 톤)."""
    penalties = 0
    reasons: list[str] = []
    if not data_ok:
        penalties += 2
        reasons.append("스냅샷/선택안 없음 — 포트폴리오 기준 미확정")
    if not macro.get("connected"):
        penalties += 1
        reasons.append("거시(macro) 미연동")
    if not user_views.get("has_views") and not user_views.get("objective_set"):
        penalties += 1
        reasons.append("관점/목적 미입력")
    if not evidence_summary.get("has_evidence", evidence_summary.get("connected")):
        penalties += 1
        reasons.append("연결된 자료(evidence) 부족")
    no_data_names = [n for n in decline.get("names", []) if n.get("status") == "not_enough_data"]
    if no_data_names:
        penalties += 1
        reasons.append("일부 종목 일봉 데이터 부족")
    level = "high" if penalties == 0 else "medium" if penalties <= 2 else "low"
    return {
        "level": level,
        "penalties": penalties,
        "reasons": reasons,
        "note": ("데이터가 충분해 비교적 신뢰할 수 있습니다." if level == "high" else
                 "데이터가 일부 부족합니다 — 단정 대신 후보/관망 톤으로 봅니다." if level == "medium" else
                 "데이터가 많이 부족합니다 — 단정 금지, 입력/연동 후 재점검 권장."),
    }


def _integration_payload(user_views: dict, macro: dict, perspective: dict,
                         evidence_summary: dict, conservative: dict, questions: dict,
                         decline: dict, synthesis: dict, *, swing_hedge: dict | None,
                         data_ok: bool, supply_demand: dict | None = None,
                         six_axis: dict | None = None) -> dict:
    """통합 섹션을 payload/return 에 동일하게 싣기 위한 묶음(자동주문 0 명시 포함)."""
    confidence = _integration_confidence(
        data_ok=data_ok, macro=macro, user_views=user_views,
        evidence_summary=evidence_summary, decline=decline)
    # no-trade 사유 후보(섹션 차원) — review-level no_trade_reason 과 별개의 정직 표기.
    no_trade_reasons: list[str] = []
    if not data_ok:
        no_trade_reasons.append("포트폴리오 기준(스냅샷/선택안) 미확정 — 주문 후보 없음(관망).")
    if synthesis.get("not_doing_today"):
        no_trade_reasons.extend(synthesis.get("not_doing_today", []))
    no_trade_reasons.append("모든 조정은 후보이며 사람 승인 전 주문/정책 변경 없음.")
    return {
        "user_views": user_views,                  # 오늘의 사용자 관점 요약
        "six_axis": six_axis or {},                # 오늘의 6축 상태(기술·거시·분산·이벤트·심리·정책)
        "supply_demand": supply_demand or {},      # 오늘의 수급(분산축) — 외국인/기관/개인 흐름+해석
        "macro": macro,                            # 오늘의 주요 거시 변화
        "perspective_variants": perspective,       # 오늘의 관점별 A/B/C 후보
        "evidence_summary": evidence_summary,      # 오늘의 자료 요약(병렬 E)
        "conservative_candidates": conservative,   # 오늘의 보수적 전환 후보
        "today_questions": questions,              # 오늘 사용자에게 물어볼 질문
        "integration_confidence": confidence,      # 데이터 부족 시 낮춤(정직)
        "no_trade_reasons": no_trade_reasons,      # 섹션 차원 no-trade 사유
        "auto_order_created": False,               # 불변: 자동주문 0
        "auto_applied": False,                     # 불변: 자동 policy 0
        "requires_user_approval": True,            # 사용자 선택/승인 필요
        "broker_neutral": True,                    # broker-neutral 유지
    }


def _synthesis_block(conn, account_index: int, decline: dict) -> dict:
    """오늘의 종합 — 자료/견해/하락신호 → 포트폴리오 영향 + 조정 후보 + 하지 않을 이유.

    broker-neutral·읽기 전용. portfolio_impact(영향+후보) + etf_analysis(겹침)를 묶어
    "오늘의 주요 자료 / 보유·관심 이슈 / 견해 vs 데이터 일치·충돌 / 하락 징후 /
    포트폴리오 영향 / 오늘의 조정 후보 / 오늘 하지 않을 이유 / 추가 확인 필요"를 구성한다.
    **자동주문 0 · 자동 적용 0.** 후보뿐이며 사람 승인 후에만 policy 흐름으로 간다.
    """
    try:
        impact = impact_mod.analyze_account(account_index)
    except Exception as e:  # noqa: BLE001 — 영향 분석 실패가 review 를 막지 않음(정직)
        impact = {"ok": False, "error": str(e), "instrument_impacts": [],
                  "theme_impacts": [], "portfolio_candidates": [], "summary": {}}
    try:
        etf = etf_mod.analyze_account_etfs(account_index)
    except Exception as e:  # noqa: BLE001
        etf = {"ok": False, "error": str(e), "data_connected": False, "overlaps": []}

    impacts = impact.get("instrument_impacts", [])
    conflicts = [i for i in impacts if i.get("alignment") == "conflict"]
    mixed = [i for i in impacts if i.get("mixed_swing")]

    # 견해 vs 데이터 일치/충돌 요약(명시).
    view_vs_data = [{
        "instrument_code": i["instrument_code"], "alignment": i["alignment"],
        "note": i["alignment_note"], "mixed_swing": i["mixed_swing"],
    } for i in impacts if i.get("alignment") in ("conflict", "aligned", "mixed")]

    # 오늘의 조정 후보(포트폴리오 차원 + 종목별 핵심) — 전부 후보(주문/정책 변경 아님).
    today_candidates = list(impact.get("portfolio_candidates", []))

    # 오늘 하지 않을 이유(관망 정당화) — 신뢰도 낮음/근거 없음/충돌(단정 금지).
    not_doing: list[str] = []
    low_conf = [i["instrument_code"] for i in impacts if i.get("low_confidence")]
    if low_conf:
        not_doing.append(f"신뢰도 낮은 종목({', '.join(low_conf[:5])}) — 단정 금지, 관망.")
    if conflicts:
        not_doing.append("사용자 견해 vs 데이터 충돌 종목 존재 — 매도/매수 단정 대신 mixed_swing 후보.")
    if not today_candidates or all(c.get("kind") == "hold" for c in today_candidates):
        not_doing.append("포트폴리오 차원 특이 신호 없음 — 현 운용기준 유지(관망도 정상).")
    not_doing.append("모든 조정은 후보이며 사람 승인 전 주문/정책/비중 변경 없음.")

    # 추가 확인 필요.
    need_more: list[str] = []
    if not impact.get("summary", {}).get("evidence_items"):
        need_more.append("연결된 자료(evidence) 부족 — 자료 수집 후 재판단 권장.")
    if not etf.get("data_connected"):
        need_more.append("ETF 구성 데이터 미연동 — 겹침/노출 분석 위해 etf_constituents 적재 필요.")
    no_data_names = [n.get("instrument_code") for n in decline.get("names", [])
                     if n.get("status") == "not_enough_data"]
    if no_data_names:
        need_more.append(f"일봉 데이터 부족: {', '.join(filter(None, no_data_names))[:120]} — 하락 분석 보강 필요.")

    return {
        "view_vs_data": view_vs_data,          # 견해 vs 데이터 일치/충돌(명시)
        "conflicts_count": len(conflicts),
        "mixed_swing_count": len(mixed),
        "portfolio_impact_summary": impact.get("summary"),
        "instrument_impacts": impacts,
        "theme_impacts": impact.get("theme_impacts", []),
        "today_adjustment_candidates": today_candidates,   # 오늘의 조정 후보(전부 후보)
        "etf_overlaps": etf.get("overlaps", []),
        "etf_concentration_flags": etf.get("concentration_flags", []),
        "etf_data_connected": etf.get("data_connected", False),
        "not_doing_today": not_doing,          # 오늘 하지 않을 이유
        "need_more_confirmation": need_more,   # 추가 확인 필요
        "auto_order_created": False,           # broker-neutral · 자동주문 0
        "auto_applied": False,
        "requires_user_approval": True,
        "note": ("오늘의 종합(읽기 전용·broker-neutral) — 자료·견해·하락신호를 포트폴리오 영향으로 "
                 "연결하고 조정 후보를 제시합니다. 주문·정책·비중 자동변경 없음(전부 후보)."),
    }


def generate_review(account_index: int, review_date: str | None = None) -> dict:
    """오늘의 Daily Review 생성(계좌×일 1행, 재생성 시 갱신). Growth Middleware 강제 통과.

    prehook(daily_portfolio_review) 는 account_id 귀속만 게이트한다(관망도 정상 결과이므로
    스냅샷/선택안 없음은 review 가 watch 로 정직하게 보고). block 이면 watch shape 로 반환."""
    def _impl(_inp, _ctx):
        return _generate_review_impl(account_index, review_date)

    out = growth_mw.run_task("daily_portfolio_review", "broker-chief", _impl,
                             account_index=account_index,
                             input={"account_index": account_index, "review_date": review_date})
    if out["blocked"]:
        # account_id 누락 등 게이트 차단 → 주문 후보 없이 watch(정상 결과 계열).
        return {"ok": False, "blocked": True,
                "action_decision": "watch", "has_orders": False,
                "scheduled_order_plan_id": None,
                "no_trade_reason": "; ".join(out["reasons"]) or "prehook gate=block",
                "account_index": account_index}
    if not out["ok"]:
        return {"ok": False, "error": "; ".join(out.get("reasons") or ["내부 오류"])}
    return out["result"]


def _generate_review_impl(account_index: int, review_date: str | None = None) -> dict:
    """오늘의 Daily Review 생성(계좌×일 1행, 재생성 시 갱신)."""
    review_date = review_date or _today()
    conn = store_db.connect()
    try:
        # 채권 듀레이션 추천(금리·경제 전망 기반) — 매 점검마다 산출 + snapshot 저장.
        # 관망/조정/스냅샷 없음 등 모든 분기에서 동일하게 포함된다(지속 추천).
        mc_id, duration_rec, _ctx = _duration_block(conn, account_index)

        # 직전 cycle 미체결 후보 재평가(carry/expire) — 모든 분기 공통(자동 주문 아님).
        carry_over = _carry_over_block(conn, account_index)

        # 하락 징후 점검 — 모든 분기 공통(읽기 전용 제안, 자동주문 0).
        decline = _decline_block(conn, account_index)

        # 오늘의 종합 — 자료/견해/하락신호 → 영향 + 조정 후보(읽기 전용, 자동주문 0).
        synthesis = _synthesis_block(conn, account_index, decline)

        # 오늘의 수급(분산축) — 외국인/기관/개인 순매수 흐름 요약 + 해석(설명 중심, 자동주문 0).
        # 병렬 A 의 investor_flows/로더 미연동이면 graceful 하게 '수급 판단 제외'(정직).
        supply_demand = _supply_demand_block(conn, account_index)

        # ── 통합 섹션(모든 분기 공통·읽기 전용·자동주문 0). 늦게 오는 모듈은 graceful. ──
        user_views = _user_views_block(account_index)          # 오늘의 사용자 관점 요약
        macro = _macro_block(account_index)                    # 오늘의 주요 거시 변화(미연동이면 정직)
        # 오늘의 6축 상태 — 이미 계산된 decline/macro/supply_demand 재사용(재스캔 없음, 정직 제외).
        six_axis = _six_axis_block(decline=decline, macro=macro, supply_demand=supply_demand)
        perspective = _perspective_block(account_index)        # 오늘의 관점별 A/B/C 후보
        evidence_summary = _evidence_summary_block(account_index)  # 오늘의 자료 요약(병렬 E)
        conservative = _conservative_candidates_block(decline, synthesis)  # 보수적 전환 후보
        # 오늘의 국채(govbond) 점검 — 현재 국채 비중이 금리/환율 환경에 맞는지 재검토(자동 변경 0).
        # 모든 분기 공통(관망/스냅샷 없음 분기에서도 동일하게 점검). swing_hedge 는 normal 분기에서만.
        govbond_check = _govbond_block(conn, account_index, macro=macro,
                                       duration_rec=duration_rec, swing_hedge=None)

        snap = _latest_snapshot(conn, account_index)
        if not snap:
            questions = _today_questions_block(
                account_index, decline=decline, synthesis=synthesis, swing_hedge=None,
                user_views=user_views, macro=macro, perspective=perspective,
                supply_demand=supply_demand)
            integ = _integration_payload(user_views, macro, perspective, evidence_summary,
                                         conservative, questions, decline, synthesis,
                                         swing_hedge=None, data_ok=False,
                                         supply_demand=supply_demand, six_axis=six_axis)
            return _store(conn, account_index, review_date, action="watch",
                          action_reason="오늘 점검", no_trade_reason="잔고 스냅샷 없음 — 동기화 후 점검",
                          snapshot_id=None, selected_allocation_id=None, drift_score=None,
                          risk_passed=None, plan_id=None,
                          market_context_id=mc_id, duration_rec=duration_rec,
                          payload={"blocked": "no_snapshot", "duration_recommendation": duration_rec,
                                   "carry_over": carry_over, "decline": decline,
                                   "govbond_check": govbond_check,
                                   "synthesis": synthesis, **integ})

        dec = decision_mod.compute(account_index)
        if not dec.get("ok"):
            # selected allocation 없음 / stale 등 → 주문 후보 없이 관망(정상 결과)
            block_code = dec.get("block_code")
            no_trade = dec.get("error")
            if block_code == "stale_snapshot":
                # stale 사유 명시(decision.compute 가 fail-closed 로 watch 시킴).
                no_trade = f"스냅샷이 오래됨(stale) — 동기화 후 재점검. ({dec.get('error')})"
            questions = _today_questions_block(
                account_index, decline=decline, synthesis=synthesis, swing_hedge=None,
                user_views=user_views, macro=macro, perspective=perspective,
                supply_demand=supply_demand)
            integ = _integration_payload(user_views, macro, perspective, evidence_summary,
                                         conservative, questions, decline, synthesis,
                                         swing_hedge=None, data_ok=False,
                                         supply_demand=supply_demand, six_axis=six_axis)
            return _store(conn, account_index, review_date, action="watch",
                          action_reason="오늘 점검", no_trade_reason=no_trade,
                          snapshot_id=snap["id"], selected_allocation_id=None, drift_score=None,
                          risk_passed=None, plan_id=None,
                          market_context_id=mc_id, duration_rec=duration_rec,
                          payload={"blocked": block_code, "detail": dec.get("error"),
                                   "stale": block_code == "stale_snapshot",
                                   "duration_recommendation": duration_rec,
                                   "carry_over": carry_over, "decline": decline,
                                   "govbond_check": govbond_check,
                                   "synthesis": synthesis, **integ})

        prov = dec.get("provenance", {})
        lines = dec.get("lines", [])
        needing = [l for l in lines if l.get("needs_adjust")]
        risk_passed = bool(dec.get("risk", {}).get("passed", True))
        drift_score = round(max((abs(l.get("drift", 0.0)) for l in lines), default=0.0), 1)

        # 스윙/헤지 노출 점검(mixed_swing 테마) — 주문 후보와 독립(노출 점검만, 자동 주문 신호 아님).
        swing_hedge = _swing_hedge_block(account_index, dec)

        plan_id = None
        if not needing:
            action, reason = "hold", "목표비중과의 차이가 밴드 내"
            no_trade = "조정 불필요 — 오늘은 관망(정상). 스윙/헤지 노출만 점검(주문 신호 아님)."
        elif not risk_passed:
            action, reason = "watch", "리스크 게이트 위반"
            no_trade = "리스크 게이트 위반 — 조정 보류, 정책 재점검 필요. 스윙/헤지 노출만 점검."
        else:
            # selected allocation + risk_passed + drift 충족 → 예약성 조정 후보(주문 단계는 승인/PIN/live lock).
            action, reason, no_trade = "rebalance", "목표비중 접근을 위한 예약성 조정 후보", None
            plan_id = _make_plan(conn, account_index, dec.get("decision_id"), needing)

        questions = _today_questions_block(
            account_index, decline=decline, synthesis=synthesis, swing_hedge=swing_hedge,
            user_views=user_views, macro=macro, perspective=perspective,
            supply_demand=supply_demand)
        integ = _integration_payload(user_views, macro, perspective, evidence_summary,
                                     conservative, questions, decline, synthesis,
                                     swing_hedge=swing_hedge, data_ok=True,
                                     supply_demand=supply_demand, six_axis=six_axis)

        payload = {
            "selected_variant": dec.get("selected_variant"),
            "total_value_krw": dec.get("total_value_krw"),
            "cash_current_pct": dec.get("cash_current_pct"),
            "cash_target_pct": dec.get("cash_target_pct"),
            "lines": lines,
            "risk": dec.get("risk"),
            "adjust_count": len(needing),
            "duration_recommendation": duration_rec,
            "swing_hedge": swing_hedge,
            "carry_over": carry_over,
            "govbond_check": govbond_check,      # 국채 점검(재검토 후보, 자동 변경 0)
            "decline": decline,                  # 하락 징후 점검(읽기 전용 제안, 자동주문 0)
            "synthesis": synthesis,              # 오늘의 종합(영향+조정 후보, 자동주문 0)
            "next_review": _next_review_date(),  # 일·주 단위 점검 — 다음 점검 권장 시점
            "note": "주문은 예약성 지정가 · 사람 승인 후 실행 · 시장가 매수 금지 · live 하드락 유지",
            **integ,
        }
        return _store(conn, account_index, review_date, action=action, action_reason=reason,
                      no_trade_reason=no_trade, snapshot_id=prov.get("account_snapshot_id") or snap["id"],
                      selected_allocation_id=prov.get("selected_allocation_id"), drift_score=drift_score,
                      risk_passed=risk_passed, plan_id=plan_id,
                      market_context_id=mc_id, duration_rec=duration_rec, payload=payload)
    finally:
        conn.close()


def _store(conn, account_index, review_date, *, action, action_reason, no_trade_reason,
           snapshot_id, selected_allocation_id, drift_score, risk_passed, plan_id, payload,
           market_context_id=None, duration_rec=None) -> dict:
    # 계좌×일 1행 — 재생성 시 덮어쓰기(같은 날 여러 번 점검 가능, 마지막이 그날 결과).
    # 재생성 시 직전 evidence 링크도 정리(stale 링크 누적 방지).
    old = conn.execute(
        "SELECT id FROM daily_portfolio_reviews WHERE account_index=? AND review_date=?",
        (account_index, review_date)).fetchall()
    for o in old:
        conn.execute("DELETE FROM daily_review_evidence_links WHERE review_id=?", (o["id"],))
    conn.execute("DELETE FROM daily_portfolio_reviews WHERE account_index=? AND review_date=?",
                 (account_index, review_date))
    # review_id 확보 위해 payload 없이 먼저 INSERT → evidence 링크 → payload UPDATE.
    cur = conn.execute(
        "INSERT INTO daily_portfolio_reviews(account_index, review_date, account_snapshot_id, "
        "selected_allocation_id, drift_score, market_context_id, action_decision, action_reason, no_trade_reason, "
        "scheduled_order_plan_id, risk_passed, approved_by_user, payload, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_index, review_date, snapshot_id, selected_allocation_id, drift_score, market_context_id, action,
         action_reason, no_trade_reason, plan_id, (1 if risk_passed else 0) if risk_passed is not None else None,
         0, None, _now()),
    )
    review_id = cur.lastrowid
    # 생성된 plan 에 review_id 역참조(추적성) — 주문 실행 아님(연결만).
    if plan_id:
        conn.execute("UPDATE scheduled_order_plans SET review_id=? WHERE id=?", (review_id, plan_id))
    # evidence 연결 — 모든 분기 공통(없으면 정직 빈 목록). payload 에도 담아 latest() 가 읽게 한다.
    evidence = _attach_evidence(conn, account_index, review_id, themes=_profile_themes(account_index))
    payload = dict(payload or {})
    payload["evidence"] = evidence
    conn.execute("UPDATE daily_portfolio_reviews SET payload=? WHERE id=?",
                 (json.dumps(payload, ensure_ascii=False), review_id))
    conn.commit()
    return {
        "ok": True, "review_id": review_id, "account_index": account_index, "review_date": review_date,
        "action_decision": action, "action_reason": action_reason, "no_trade_reason": no_trade_reason,
        "drift_score": drift_score, "selected_allocation_id": selected_allocation_id,
        "scheduled_order_plan_id": plan_id, "risk_passed": risk_passed,
        "has_orders": plan_id is not None,
        "market_context_id": market_context_id,
        "duration_recommendation": duration_rec,
        "swing_hedge": payload.get("swing_hedge"),
        "carry_over": payload.get("carry_over"),
        "govbond_check": payload.get("govbond_check"),
        "decline": payload.get("decline"),
        "synthesis": payload.get("synthesis"),
        "evidence": evidence,
        "next_review": payload.get("next_review"),
        # ── 통합 섹션(읽기 전용·자동주문 0). watch 분기에서도 동일하게 노출. ──
        "six_axis": payload.get("six_axis"),
        "supply_demand": payload.get("supply_demand"),
        "user_views": payload.get("user_views"),
        "macro": payload.get("macro"),
        "perspective_variants": payload.get("perspective_variants"),
        "evidence_summary": payload.get("evidence_summary"),
        "conservative_candidates": payload.get("conservative_candidates"),
        "today_questions": payload.get("today_questions"),
        "integration_confidence": payload.get("integration_confidence"),
        "no_trade_reasons": payload.get("no_trade_reasons"),
        "auto_order_created": payload.get("auto_order_created", False),
        "requires_user_approval": payload.get("requires_user_approval", True),
    }


def latest(account_index: int) -> dict | None:
    conn = store_db.connect()
    try:
        r = conn.execute(
            "SELECT * FROM daily_portfolio_reviews WHERE account_index=? ORDER BY review_date DESC, id DESC LIMIT 1",
            (account_index,),
        ).fetchone()
        if not r:
            return None
        d = dict(r)
        if d.get("payload"):
            try:
                d["payload"] = json.loads(d["payload"])
            except (ValueError, TypeError):
                pass
        return d
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--show", action="store_true", help="최신 리뷰만 조회")
    args = ap.parse_args()
    try:
        out = latest(args.account) if args.show else generate_review(args.account)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
