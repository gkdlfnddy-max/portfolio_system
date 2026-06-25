"""포트폴리오 영향 분석 + 조정 후보 엔진.

목적(불변):
  자료(evidence_items)·사용자 견해(user_views)·하락 징후(decline_scan)가 모이면
  *내 포트폴리오에 어떤 의미인지*를 종목/테마 단위로 연결해 **조정 후보**를 만든다.
  예: "반도체 ETF 보유 + 가격 강하나 고점신호 + 사용자 장기긍정/단기과열
       → long 유지 + hedge 검토 + 신규매수 속도 완화 후보".

핵심 규칙(불변 — CLAUDE.md §2):
  - **분석/후보까지만.** 자동주문·자동 policy 변경·승인 전 allocation 반영 전부 금지.
  - 조정은 **후보**로만(관망 / 현금밴드 상향 후보 / 위험자산 축소 후보 / 헤지 검토 후보 /
    신규매수 보류 후보 / 리밸런싱 속도 완화 후보 / 테마 노출 축소 후보).
  - **위험과 기회를 구분**한다(같은 종목이 둘 다 가질 수 있음).
  - **사용자 견해 vs 데이터 일치/충돌을 명시**한다. 충돌(예: 사용자 장기긍정 ↔ 단기 고점신호)
    이면 단순 매도/매수 단정이 아니라 **mixed_swing 구조**(long 유지 + 분할매수 + hedge 검토)
    를 제안한다.
  - confidence 낮으면 약한 후보/관망 — 단정 금지(CLAUDE.md §11.8).
  - 지능 = 규칙(decline_scan) + Claude+메모리 성장. **Anthropic API 미사용.**

읽는 것(DB 읽기 전용 — 타 모듈 본문 의존 X):
  user_views(Agent1 입력) · evidence_items(Agent2) · decline_scan.scan_instrument
  (하락 6축·confidence) · holdings/universe_instruments(보유·관심·ETF) · etf_constituents.

  python -m main_mission.portfolio_os.portfolio_impact --account 1
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import decline_scan as scan_mod
from .store import db as store_db

# confidence(데이터/하락 6축) → 후보 강도 캡. candidate.CONFIDENCE_BANDS(SSOT)에서 가져온다.
#   < low : 단정 금지 — 관망/약한 후보만.
#   ≥ mid : 비교적 강한 후보 가능(단, 항상 사람 승인).
from .candidate import CONFIDENCE_BANDS  # noqa: E402  (SSOT 단일 진실)

CONFIDENCE_LOW = CONFIDENCE_BANDS["low"]
CONFIDENCE_MID = CONFIDENCE_BANDS["mid"]

# 사용자 견해 stance → 부호(데이터 일치/충돌 비교용).
_STANCE_SIGN = {"positive": 1, "negative": -1, "neutral": 0, "observe": 0}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# DB 읽기 (읽기 전용 — 코드 의존 없이 테이블만)
# ============================================================
def _latest_snapshot_id(conn, account_index: int):
    row = conn.execute(
        "SELECT id FROM account_snapshots WHERE account_index=? ORDER BY id DESC LIMIT 1",
        (account_index,)).fetchone()
    return row["id"] if row else None


def _load_holdings(conn, account_index: int) -> list[dict]:
    sid = _latest_snapshot_id(conn, account_index)
    if not sid:
        return []
    rows = conn.execute(
        "SELECT ticker, name, market_value FROM holdings WHERE snapshot_id=?", (sid,)).fetchall()
    return [dict(r) for r in rows]


def _load_universe(conn, account_index: int) -> list[dict]:
    rows = conn.execute(
        "SELECT ticker, name, asset_class, is_inverse, is_leveraged "
        "FROM universe_instruments WHERE account_index=? AND is_active=1", (account_index,)).fetchall()
    return [dict(r) for r in rows]


def _load_user_views(conn, account_index: int) -> list[dict]:
    """이 계좌의 활성 사용자 견해(user_views). 계좌 격리(교차적용 금지)."""
    rows = conn.execute(
        "SELECT id, layer, theme, ticker, etf, stance, conviction, horizon, note "
        "FROM user_views WHERE account_index=? AND status='active' ORDER BY id DESC",
        (account_index,)).fetchall()
    return [dict(r) for r in rows]


def _load_evidence(conn, account_index: int) -> list[dict]:
    """포트폴리오 관련 evidence_items(stale 제외). 계좌 귀속(related_account=계좌 or NULL=공용)."""
    rows = conn.execute(
        "SELECT id, source_type, source_date, freshness, confidence, related_ticker, related_etf, "
        "related_theme, summary, positive_factors, negative_factors, uncertainties "
        "FROM evidence_items WHERE stale=0 AND (related_account=? OR related_account IS NULL) "
        "ORDER BY id DESC", (account_index,)).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# 매칭 — 종목/ETF/테마로 evidence·user_views 연결
# ============================================================
def _norm(s) -> str:
    return (s or "").strip().lower()


def _views_for(views: list[dict], *, ticker: str | None, theme: str | None) -> list[dict]:
    t, th = _norm(ticker), _norm(theme)
    out = []
    for v in views:
        if t and (_norm(v.get("ticker")) == t or _norm(v.get("etf")) == t):
            out.append(v)
        elif th and _norm(v.get("theme")) == th:
            out.append(v)
    return out


def _evidence_for(evs: list[dict], *, ticker: str | None, theme: str | None) -> list[dict]:
    t, th = _norm(ticker), _norm(theme)
    out = []
    for e in evs:
        if t and (_norm(e.get("related_ticker")) == t or _norm(e.get("related_etf")) == t):
            out.append(e)
        elif th and _norm(e.get("related_theme")) == th:
            out.append(e)
    return out


def _evidence_bias(evs: list[dict]) -> dict:
    """evidence 묶음 → 긍/부정 편향 부호 + 평균 confidence(데이터 측 신호).

    positive_factors/negative_factors 존재 여부로 부호를 합산(단순·정직 — 가짜 점수 X).
    """
    pos = neg = 0
    confs = []
    for e in evs:
        if e.get("positive_factors"):
            pos += 1
        if e.get("negative_factors"):
            neg += 1
        if e.get("confidence") is not None:
            confs.append(float(e["confidence"]))
    sign = (1 if pos > neg else (-1 if neg > pos else 0))
    avg_conf = round(sum(confs) / len(confs), 3) if confs else None
    return {"positive_count": pos, "negative_count": neg, "sign": sign,
            "avg_confidence": avg_conf, "count": len(evs)}


def _user_bias(views: list[dict]) -> dict:
    """user_views 묶음 → 단기/장기 stance 부호 + 평균 conviction. horizon 으로 단/장 분리."""
    short_signs, long_signs, all_signs, convs = [], [], [], []
    for v in views:
        sign = _STANCE_SIGN.get(_norm(v.get("stance")), 0)
        all_signs.append(sign)
        hz = _norm(v.get("horizon")) or _norm(v.get("layer"))
        if hz == "short":
            short_signs.append(sign)
        elif hz in ("long", "mid"):
            long_signs.append(sign)
        if v.get("conviction") is not None:
            convs.append(float(v["conviction"]))

    def _agg(signs):
        if not signs:
            return None
        s = sum(signs)
        return 1 if s > 0 else (-1 if s < 0 else 0)

    return {"overall_sign": _agg(all_signs), "short_sign": _agg(short_signs),
            "long_sign": _agg(long_signs),
            "avg_conviction": round(sum(convs) / len(convs), 3) if convs else None,
            "count": len(views)}


# ============================================================
# 종목 단위 영향 분석
# ============================================================
def analyze_instrument(instrument_code: str, *, held: bool, is_etf: bool,
                       theme: str | None, user_views: list[dict], evidence: list[dict],
                       decline: dict | None) -> dict:
    """한 종목(또는 ETF)에 대한 (하락신호 + evidence + user_views) 종합 → 영향 + 조정 후보.

    decline: scan_mod.scan_instrument 결과(없으면 None — 데이터 부족).
    반환: 영향 분류 + 위험/기회 + 견해 vs 데이터 일치/충돌 + 조정 후보.
    """
    u = _user_bias(user_views)
    e = _evidence_bias(evidence)

    # 하락 징후 측 신호.
    decline_ok = bool(decline and decline.get("ok"))
    risk_score = decline.get("risk_score") if decline_ok else None
    holistic = decline.get("holistic_risk") if decline_ok else None
    overall_conf = decline.get("overall_confidence") if decline_ok else None
    decline_high = decline_ok and decline.get("risk_level") in ("high", "severe")

    # 데이터 측 종합 신뢰도: 하락 6축 confidence 와 evidence confidence 중 가용한 것.
    data_confs = [c for c in (overall_conf, e.get("avg_confidence")) if c is not None]
    data_confidence = round(sum(data_confs) / len(data_confs), 3) if data_confs else None
    low_conf = data_confidence is None or data_confidence < CONFIDENCE_LOW
    strong_conf = data_confidence is not None and data_confidence >= CONFIDENCE_MID

    # 데이터 부호: 하락 위험(음) + evidence 편향. 위험 높으면 음(-)으로 기운다.
    data_sign = e.get("sign", 0)
    if decline_high:
        data_sign = -1
    elif decline_ok and risk_score is not None and risk_score >= 25 and data_sign >= 0:
        data_sign = -1 if data_sign == 0 else data_sign

    # --- 견해 vs 데이터 일치/충돌 ---
    user_sign = u.get("overall_sign")
    alignment, conflict_note = _alignment(u, data_sign, decline_high)

    # --- 위험 / 기회 구분 ---
    risks, opportunities = [], []
    if decline_high:
        risks.append(f"하락 징후 강함(위험점수 {risk_score}, holistic {holistic}).")
    elif decline_ok and risk_score is not None and risk_score >= 25:
        risks.append(f"하락 선행신호 일부(위험점수 {risk_score}).")
    if e.get("negative_count"):
        risks.append(f"부정 자료 {e['negative_count']}건(공시/뉴스/거시 등).")
    if u.get("short_sign") == -1:
        risks.append("사용자 단기 부정 견해(단기 과열/조정 우려).")
    if e.get("positive_count"):
        opportunities.append(f"긍정 자료 {e['positive_count']}건.")
    if u.get("long_sign") == 1:
        opportunities.append("사용자 장기 긍정 견해(구조적 성장 기대).")
    if decline_ok and not decline_high and (risk_score is None or risk_score < 15):
        opportunities.append("하락 징후 낮음 — 추세 훼손 신호 약함.")

    # --- mixed_swing 판단 ---
    #   보유 + (사용자 장기 긍정) + (단기 과열/하락신호) = 단타 아닌 노출관리(net/gross).
    mixed_swing = bool(
        held and u.get("long_sign") == 1 and (decline_high or u.get("short_sign") == -1
                                              or (decline_ok and risk_score is not None and risk_score >= 25)))

    # --- 조정 후보 생성 ---
    candidates = _instrument_candidates(
        held=held, is_etf=is_etf, mixed_swing=mixed_swing, decline_high=decline_high,
        decline_ok=decline_ok, risk_score=risk_score, user=u, evidence=e,
        low_conf=low_conf, strong_conf=strong_conf, conflict=(alignment == "conflict"))

    return {
        "instrument_code": instrument_code,
        "is_etf": is_etf,
        "held": held,
        "theme": theme,
        "data_confidence": data_confidence,
        "low_confidence": low_conf,
        "decline": {"analyzed": decline_ok, "risk_score": risk_score,
                    "holistic_risk": holistic, "overall_confidence": overall_conf,
                    "high": decline_high} if decline is not None else
                   {"analyzed": False, "note": "일봉 데이터 부족 — 하락 분석 불가(정직)."},
        "user_view": u,
        "evidence_bias": e,
        "alignment": alignment,                 # aligned | conflict | mixed | none
        "alignment_note": conflict_note,
        "risks": risks,
        "opportunities": opportunities,
        "mixed_swing": mixed_swing,
        "adjustment_candidates": candidates,
        "auto_order_created": False,
    }


def _alignment(user: dict, data_sign: int, decline_high: bool) -> tuple[str, str]:
    """사용자 견해 vs 데이터 부호 비교 → (alignment, 한글 설명)."""
    user_sign = user.get("overall_sign")
    if user_sign is None:
        return "none", "이 종목/테마에 대한 사용자 견해가 없습니다(데이터 기준만)."
    # 장기 긍정 ↔ 단기 부정/하락신호 = 전형적 충돌(mixed_swing 후보).
    if user.get("long_sign") == 1 and (decline_high or user.get("short_sign") == -1 or data_sign < 0):
        return "conflict", ("충돌: 사용자 장기 긍정 ↔ 단기 과열/하락 신호. "
                            "단순 매도/매수 단정이 아니라 mixed_swing 구조(long 유지 + 분할매수 + hedge 검토) 후보.")
    if user_sign > 0 and data_sign > 0:
        return "aligned", "일치: 사용자 긍정 견해와 데이터(긍정 자료/낮은 하락위험)가 같은 방향."
    if user_sign < 0 and data_sign < 0:
        return "aligned", "일치: 사용자 부정 견해와 데이터(부정 자료/하락 신호)가 같은 방향(보수적)."
    if user_sign != 0 and data_sign != 0 and (user_sign > 0) != (data_sign > 0):
        return "conflict", "충돌: 사용자 견해와 데이터 방향이 반대 — 단정 금지, mixed/관망 후보."
    return "mixed", "혼재: 사용자 견해와 데이터가 부분적으로만 일치 — 신중(약한 후보)."


def _instrument_candidates(*, held: bool, is_etf: bool, mixed_swing: bool, decline_high: bool,
                           decline_ok: bool, risk_score, user: dict, evidence: dict,
                           low_conf: bool, strong_conf: bool, conflict: bool) -> list[dict]:
    """종목 단위 조정 후보(주문 아님). 위험/기회·견해충돌·신뢰도에 따라 강도 조절."""
    out: list[dict] = []

    def add(kind, note, *, strength="weak"):
        out.append({"kind": kind, "note": note, "strength": strength,
                    "requires_user_approval": True, "auto_applied": False})

    # 신뢰도 낮으면 관망/데이터 추가만(단정 금지).
    if low_conf:
        add("observe", "신뢰도 낮음 — 관망/주의. 데이터 추가 수집 후 재판단(후보).")
        if held and (decline_high or conflict):
            add("hold_long", "보유 유지(관망) — 충분한 근거 전까지 매도 단정 금지(후보).")
        return out

    if mixed_swing:
        # 충돌(장기긍정↔단기과열): long 유지 + 분할매수 + hedge 검토 — net/gross 노출관리.
        add("hold_long", "보유(롱) 유지 — 장기 견해 존중(후보).", strength="moderate")
        add("staged_buy", "신규/추가 매수는 분할(예측 진입)·속도 완화 — 일·주 단위 지정가(후보).",
            strength="moderate")
        add("consider_hedge", "헤지(인버스 한도 내) 검토 — 단기 과열 노출 상쇄(후보).",
            strength="moderate" if strong_conf else "weak")
        add("slow_new_buy", "신규매수 속도 완화 후보 — 무릎 지점까지 분할(시장가 금지).")
        return out

    if decline_high:
        add("reduce_risk", "위험자산 축소 검토 후보(주문 아님) — 하락 신호 강함.",
            strength="moderate" if strong_conf else "weak")
        add("slow_new_buy", "신규매수 보류/속도 완화 후보 — 추세 훼손 신호.")
        if strong_conf:
            add("consider_hedge", "헤지 검토 후보(인버스 한도 내) — 강한 하락 신호.", strength="moderate")
    elif decline_ok and risk_score is not None and risk_score >= 25:
        add("slow_new_buy", "신규매수 속도 완화 후보 — 하락 선행신호 일부(주의).")

    # 사용자 부정 견해(데이터와 무관하게) → 노출 축소 후보(약하게).
    if user.get("overall_sign") == -1 and not decline_high:
        add("reduce_exposure", "사용자 부정 견해 반영 — 노출 축소 검토 후보(약함).")

    # 긍정 일치 + 보유 중 + 하락 낮음 → 유지(기회).
    if held and not decline_high and user.get("long_sign") == 1:
        add("hold_long", "보유 유지(기회) — 장기 긍정 + 하락 신호 약함(후보).",
            strength="moderate" if strong_conf else "weak")

    if not out:
        add("observe", "특이 신호 없음 — 현 운용기준 유지/관망(후보).")
    return out


# ============================================================
# 포트폴리오 집계
# ============================================================
def analyze_account(account_index: int) -> dict:
    """계좌 보유/관심 전체 → 종목별 영향 + 포트폴리오 차원 조정 후보(현금밴드/위험자산/헤지/속도)."""
    conn = store_db.connect()
    try:
        holdings = _load_holdings(conn, account_index)
        universe = _load_universe(conn, account_index)
        views = _load_user_views(conn, account_index)
        evidence = _load_evidence(conn, account_index)
    finally:
        conn.close()

    held_tickers = {_norm(h["ticker"]) for h in holdings}
    # 종목 집합(보유 ∪ 관심). ETF 여부는 asset_class 로 추정(etf 포함).
    items: dict[str, dict] = {}
    for h in holdings:
        items[h["ticker"]] = {"ticker": h["ticker"], "name": h.get("name"),
                              "is_etf": False, "theme": None}
    for u in universe:
        ac = _norm(u.get("asset_class"))
        items.setdefault(u["ticker"], {"ticker": u["ticker"], "name": u.get("name"),
                                       "is_etf": ("etf" in ac), "theme": u.get("asset_class")})
        if "etf" in ac:
            items[u["ticker"]]["is_etf"] = True

    instrument_impacts: list[dict] = []
    for tk, meta in items.items():
        held = _norm(tk) in held_tickers
        uv = _views_for(views, ticker=tk, theme=meta.get("theme"))
        ev = _evidence_for(evidence, ticker=tk, theme=meta.get("theme"))
        try:
            dec = scan_mod.scan_instrument(tk, sector=meta.get("theme"))
            if not dec.get("ok"):
                dec = None  # 데이터 부족 → analyze_instrument 가 정직 표기
        except Exception:  # noqa: BLE001 — 한 종목 실패가 전체를 멈추지 않게
            dec = None
        instrument_impacts.append(analyze_instrument(
            tk, held=held, is_etf=meta["is_etf"], theme=meta.get("theme"),
            user_views=uv, evidence=ev, decline=dec))

    theme_impacts = _theme_impacts(views, evidence, instrument_impacts)
    macro = _macro_context()           # 거시 우선 — 현금밴드/채권/달러/미국ETF/헤지 방향(후보)
    portfolio_candidates = _portfolio_candidates(instrument_impacts, macro)
    conflicts = [i for i in instrument_impacts if i["alignment"] == "conflict"]

    return {
        "ok": True, "account_index": account_index, "analyzed_at": _now(),
        "macro": macro,                # 거시→포트폴리오 해석(후보) 또는 거시 미연동(정직)
        "summary": {
            "instruments": len(instrument_impacts),
            "held": len(held_tickers),
            "user_views": len(views),
            "evidence_items": len(evidence),
            "conflicts": len(conflicts),
            "mixed_swing": sum(1 for i in instrument_impacts if i["mixed_swing"]),
        },
        "instrument_impacts": instrument_impacts,
        "theme_impacts": theme_impacts,
        "portfolio_candidates": portfolio_candidates,   # 현금밴드/위험자산/헤지/속도(후보)
        "requires_user_approval": True,
        "auto_order_created": False,
        "auto_applied": False,
        "note": ("포트폴리오 영향 분석입니다 — 전부 '조정 후보'이며 주문/정책/비중 자동변경 없음. "
                 "위험과 기회를 구분하고, 사용자 견해와 데이터의 일치/충돌을 명시합니다. "
                 "충돌 시 매도/매수 단정 대신 mixed_swing(long 유지·분할매수·hedge 검토)을 제안합니다."),
    }


def _theme_impacts(views: list[dict], evidence: list[dict], instrument_impacts: list[dict]) -> list[dict]:
    """테마 단위 영향 — user_views/evidence 의 테마를 묶어 방향·충돌 집계."""
    themes: set[str] = set()
    for v in views:
        if v.get("theme"):
            themes.add(v["theme"])
    for e in evidence:
        if e.get("related_theme"):
            themes.add(e["related_theme"])
    out = []
    for th in sorted(themes):
        uv = [v for v in views if _norm(v.get("theme")) == _norm(th)]
        ev = [e for e in evidence if _norm(e.get("related_theme")) == _norm(th)]
        u = _user_bias(uv)
        e = _evidence_bias(ev)
        alignment, note = _alignment(u, e.get("sign", 0), False)
        out.append({"theme": th, "user_view": u, "evidence_bias": e,
                    "alignment": alignment, "alignment_note": note,
                    "candidate": ("테마 노출 축소 후보(약함)" if alignment == "conflict"
                                  else "현 테마 노출 유지(관망)")})
    return out


def _macro_context() -> dict:
    """거시(ECOS/FRED) → 포트폴리오 해석(후보). 미연동이면 정직하게 connected=False.

    **거시 우선(CEO 강조)**: 종목 신호보다 먼저 현금밴드/채권/달러/미국ETF/헤지 방향을 본다.
    데이터 없으면 '거시 미연동'을 명시(가짜 점수 금지). 자동 적용/주문 0.
    """
    try:
        from . import macro_connect as mc
        return mc.macro_to_portfolio()
    except Exception as e:  # noqa: BLE001 — 거시 실패가 전체 분석을 막지 않게(정직 표기)
        return {"connected": False, "signals": [], "tilts": {},
                "note": f"거시 미연동(조회 실패) — {e}", "requires_user_approval": True,
                "auto_applied": False}


def _macro_candidates(macro: dict) -> list[dict]:
    """거시 tilt → 포트폴리오 차원 후보. 거시가 우선이라 종목 신호보다 앞에 둔다(후보만)."""
    out: list[dict] = []

    def add(kind, note, *, strength="weak"):
        out.append({"kind": kind, "note": note, "strength": strength, "source": "macro",
                    "requires_user_approval": True, "auto_applied": False})

    if not macro or not macro.get("connected"):
        add("macro_not_connected",
            "거시 미연동 — ECOS/FRED 키 설정·적재 후 현금밴드/채권/달러/미국ETF/헤지 방향을 연결합니다(정직).")
        return out

    tilts = macro.get("tilts", {})
    lean = macro.get("lean")
    strong = "moderate" if (macro.get("defensive_score") or 0) >= 3.0 else "weak"
    if tilts.get("cash_band", 0) >= 1.0:
        add("cash_band_raise", "거시 방어 신호 — 현금밴드 상향 검토 후보(금리/역전/인플레/유가).",
            strength=strong)
    if tilts.get("short_bond", 0) >= 1.0:
        add("prefer_short_bond", "거시 신호 — 단기채(짧은 듀레이션) 선호 검토 후보.", strength=strong)
    if tilts.get("risk_assets", 0) <= -1.0:
        add("reduce_risk_assets", "거시 신호 — 위험자산 비중/신규매수 속도 완화 후보.", strength=strong)
    if tilts.get("us_etf", 0) >= 1.0 or tilts.get("usd_exposure", 0) >= 1.0:
        add("us_etf_favorable", "거시 신호(달러 강세) — 미국ETF/달러노출 우호 후보(단 추격 경계).")
    elif tilts.get("us_etf", 0) <= -0.5:
        add("us_etf_caution", "거시 신호(달러 약세) — 미국ETF 신규 환노출 분할/관망 후보.")
    if tilts.get("hedge", 0) >= 1.0:
        add("consider_hedge", "거시 신호(역전/공포) — 헤지(인버스 한도 내) 검토 후보.", strength=strong)
    if not out:
        add("macro_neutral", f"거시 중립({lean}) — 거시발 특이 방향 없음(관망).")
    return out


def _portfolio_candidates(instrument_impacts: list[dict], macro: dict | None = None) -> list[dict]:
    """종목 영향 집계 → 포트폴리오 차원 조정 후보(현금밴드/위험자산/헤지/리밸런싱 속도). 주문 아님.

    **거시 우선**: macro 가 주어지면 거시발 후보를 앞에 배치(현금밴드/채권/달러/미국ETF/헤지).
    """
    high_decline = [i for i in instrument_impacts
                    if i["decline"].get("high") and not i["low_confidence"]]
    mixed = [i for i in instrument_impacts if i["mixed_swing"]]
    slow_buy = [i for i in instrument_impacts
                if any(c["kind"] in ("slow_new_buy", "staged_buy") for c in i["adjustment_candidates"])]
    hedge = [i for i in instrument_impacts
             if any(c["kind"] == "consider_hedge" for c in i["adjustment_candidates"])]

    # 거시 우선 — 거시발 후보를 맨 앞에(현금밴드/채권/달러/미국ETF/헤지). 미연동이면 정직 표기.
    out: list[dict] = _macro_candidates(macro) if macro is not None else []

    def add(kind, note, *, strength="weak"):
        out.append({"kind": kind, "note": note, "strength": strength,
                    "requires_user_approval": True, "auto_applied": False})

    if high_decline:
        names = ", ".join(i["instrument_code"] for i in high_decline[:5])
        add("cash_band_raise", f"현금밴드 상향 검토 후보 — 강한 하락 신호 종목: {names}.",
            strength="moderate")
        add("reduce_risk_assets", "위험자산 비중 축소 검토 후보(주문 아님).", strength="moderate")
    if hedge:
        add("consider_hedge", "헤지(인버스 한도 내) 검토 후보 — 강한 신호/충돌 종목 존재.")
    if slow_buy or mixed:
        add("slow_rebalance", "리밸런싱 속도 완화 후보 — 분할·예측 진입(시장가 금지).")
        add("hold_new_buy", "신규매수 보류/속도 완화 후보 — 일·주 단위 지정가.")
    if mixed:
        add("mixed_swing_structure",
            "mixed_swing 구조 후보 — long 유지 + 분할매수 + hedge 검토(노출관리 net/gross).",
            strength="moderate")
    if not out:
        add("hold", "포트폴리오 차원 특이 신호 없음 — 현 운용기준 유지/관망(후보).")
    return out


# ============================================================
# 같은 데이터 다른 해석 — 관점별 출력 포맷
# ============================================================
# CEO: 같은 데이터도 관점 따라 다르게 해석된다. 단정 금지 — 후보로 제시하고 사람이 승인.
# 포맷(불변 순서):
#   공통 사실 / 사용자 관점 / 관점에 따른 해석 / 포트폴리오 영향 /
#   선택 가능 후보 / 각 후보 장단점 / 사용자 승인 필요
def different_interpretations(account_index: int, *, perspectives: dict | None = None) -> dict:
    """**같은 데이터 다른 해석** 출력. 분석(analyze_account) + 관점안(A/B/C)을 묶어
    하나의 '관점별 해석' 블록으로 재구성한다. 자동 적용/주문 없음(후보만).

    perspectives: perspective_variants.generate 결과(있으면). 없으면 지연 import 로 생성.
    충돌(사용자 관점 vs 데이터)이면 단정 대신 mixed_swing 구조를 명시한다(불변)."""
    analysis = analyze_account(account_index)
    if not analysis.get("ok"):
        return analysis

    if perspectives is None:
        from . import perspective_variants as pv  # 지연 import (순환 회피)
        perspectives = pv.generate(account_index, save_draft=False)

    # 공통 사실 — 관점과 무관한 측정값(보유/견해/자료/하락 신호 집계).
    summ = analysis["summary"]
    high_decline = [i["instrument_code"] for i in analysis["instrument_impacts"]
                    if i["decline"].get("high") and not i["low_confidence"]]
    macro = analysis.get("macro") or {}
    common_facts = {
        "instruments": summ["instruments"],
        "held": summ["held"],
        "user_views": summ["user_views"],
        "evidence_items": summ["evidence_items"],
        "high_decline_instruments": high_decline,
        # 거시 우선 — 관점과 무관한 거시 사실(연동 여부·기울기). 가짜 점수 없음.
        "macro_connected": bool(macro.get("connected")),
        "macro_lean": macro.get("lean"),
        "macro_signals": [s["detail"] for s in macro.get("signals", [])],
        "note": ("관점과 무관한 측정값(데이터). 해석은 아래 관점별로 갈립니다. "
                 + ("거시 미연동 — ECOS/FRED 키 설정 후 거시 우선 해석이 붙습니다."
                    if not macro.get("connected") else "거시가 우선 신호로 반영됩니다.")),
    }

    # 사용자 관점 — 견해 요약 + 목적(있으면).
    obj = (perspectives or {}).get("objective", {})
    user_perspective = {
        "objective_set": obj.get("set"),
        "objective": obj.get("label"),
        "objective_note": obj.get("note"),
        "views_summary": (perspectives or {}).get("views_summary"),
        "themes_long": (perspectives or {}).get("themes_long", []),
    }

    # 관점에 따른 해석 — 충돌 종목은 단정 금지(mixed_swing).
    conflicts = [i for i in analysis["instrument_impacts"] if i["alignment"] == "conflict"]
    interpretations = []
    for i in analysis["instrument_impacts"]:
        interpretations.append({
            "instrument_code": i["instrument_code"],
            "alignment": i["alignment"],            # aligned|conflict|mixed|none
            "alignment_note": i["alignment_note"],
            "mixed_swing": i["mixed_swing"],
            "reading": ("관점 충돌 — 단정 금지. long 유지+분할매수+hedge(mixed_swing) 후보."
                        if i["alignment"] == "conflict"
                        else i["alignment_note"]),
        })

    # 선택 가능 후보 + 각 후보 장단점 — A/B/C.
    cand = (perspectives or {}).get("candidates", [])
    selectable = [{
        "perspective": c["perspective"], "label": c["label"], "summary": c["summary"],
        "objective": c.get("objective"), "weights": c.get("weights"),
    } for c in cand]
    pros_cons = [{
        "perspective": c["perspective"], "label": c["label"],
        "pros": c.get("pros", []), "risks": c.get("risks", []),
        "break_triggers": c.get("break_triggers", []),
        "more_to_confirm": c.get("more_to_confirm", []),
    } for c in cand]

    return {
        "ok": True,
        "account_index": account_index,
        "format": ["common_facts", "user_perspective", "interpretations",
                   "portfolio_impact", "selectable_candidates", "candidate_pros_cons",
                   "requires_user_approval"],
        "common_facts": common_facts,
        "macro": macro,                               # 거시→포트폴리오 해석(후보) 또는 미연동
        "user_perspective": user_perspective,
        "interpretations": interpretations,
        "portfolio_impact": analysis["portfolio_candidates"],
        "selectable_candidates": selectable,          # A/B/C 후보
        "candidate_pros_cons": pros_cons,
        "conflicts": len(conflicts),
        "requires_user_approval": True,
        "auto_applied": False,
        "auto_order_created": False,
        "note": ("같은 데이터도 관점에 따라 다르게 해석됩니다(하나의 정답 금지). "
                 "충돌 시 매도/매수 단정 대신 mixed_swing 을 제시하고, A/B/C 후보 중 "
                 "사람이 골라 승인해야 반영됩니다."),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--interpretations", action="store_true",
                    help="같은 데이터 다른 해석(관점별 A/B/C) 출력")
    args = ap.parse_args()
    try:
        out = (different_interpretations(args.account) if args.interpretations
               else analyze_account(args.account))
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
