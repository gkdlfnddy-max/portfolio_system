"""국채 ETF 후보 — 실 지표(가격/거래량) 연동 + 후보 비교 강화.

CEO 방침(불변):
  - 채권은 **국채만(government_only)**. 회사채/하이일드 금지. (bond_bucket 시드 재사용)
  - 본 모듈은 **운용 수단(상품) 설명·비교 전용**이다. 자동 주문/policy 변경 0.
  - 후보 비교는 *추천일 뿐* — C안(특정 ETF) 바로 확정 아님. 사용자가 외워 고르는 게 아니라
    시스템이 거시·계좌·확정안 기준으로 설명한다.

정직성(가짜 지표 0):
  - **KR 국채 ETF 5종**(153130·114260·471230·439870·451530)은 KIS 국내 일봉으로
    **가격/거래량 실연동**(price_history fetcher 재사용). 가격 있으면 recent_volatility 계산.
  - **미국 3종**(SHY/IEF/TLT)은 KIS 해외 미연동 → price/volume **unknown 정직**(가짜 0).
  - 보수율(expense_ratio)/듀레이션(duration_years)/만기수익률(yield) 은 무료 KR API 제한 →
    **unknown 정직**(임의 수치 금지). 단, region/duration_bucket/tracking_index(추적지수)/
    hedged_or_unhedged(환헤지 여부)는 **알려진 정성 사실**이므로 표기한다.

  python -m main_mission.portfolio_os.govbond_etf --account 1
  python -m main_mission.portfolio_os.govbond_etf --fetch --account 1   # KR 일봉 실적재
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone

from . import price_history
from .candidate import candidate_evaluation

# --- ETF universe (정성 사실 + 실연동 가능 여부) ------------------------------
# duration_bucket: short | intermediate | long
# data_source: "kis_domestic_daily"(KR 일봉 실연동) | None(미연동 — 미국)
# expense_ratio/duration_years/yield 는 **연동 안 함** → 항상 unknown(가짜 금지).
_UNKNOWN = "unknown"

_UNIVERSE = [
    # ---- 미국 국채 ETF (iShares) — KIS 해외 미연동(가격/거래량 unknown 정직) ----
    {"ticker": "SHY", "name": "iShares 1-3Y Treasury", "region": "미국",
     "duration_bucket": "short", "tracking_index": "ICE U.S. Treasury 1-3 Year Bond Index",
     "hedged_or_unhedged": "USD(달러 표시·환노출)", "data_source": None,
     "instrument_code": None},
    {"ticker": "IEF", "name": "iShares 7-10Y Treasury", "region": "미국",
     "duration_bucket": "intermediate", "tracking_index": "ICE U.S. Treasury 7-10 Year Bond Index",
     "hedged_or_unhedged": "USD(달러 표시·환노출)", "data_source": None,
     "instrument_code": None},
    {"ticker": "TLT", "name": "iShares 20Y+ Treasury", "region": "미국",
     "duration_bucket": "long", "tracking_index": "ICE U.S. Treasury 20+ Year Bond Index",
     "hedged_or_unhedged": "USD(달러 표시·환노출)", "data_source": None,
     "instrument_code": None},
    # ---- 한국 국채 ETF (KRX 상장) — KIS 국내 일봉 실연동(가격/거래량) ----
    {"ticker": "153130", "name": "KODEX 단기채권", "region": "한국",
     "duration_bucket": "short", "tracking_index": "KIS 단기통안채 지수(단기)",
     "hedged_or_unhedged": "원화(KRW)·환노출 없음", "data_source": "kis_domestic_daily",
     "instrument_code": "153130"},
    {"ticker": "114260", "name": "KODEX 국고채3년", "region": "한국",
     "duration_bucket": "short", "tracking_index": "MKF 국고채 지수(3년)",
     "hedged_or_unhedged": "원화(KRW)·환노출 없음", "data_source": "kis_domestic_daily",
     "instrument_code": "114260"},
    {"ticker": "471230", "name": "KODEX 국고채10년액티브", "region": "한국",
     "duration_bucket": "intermediate", "tracking_index": "KAP 국고채 10년 지수(액티브)",
     "hedged_or_unhedged": "원화(KRW)·환노출 없음", "data_source": "kis_domestic_daily",
     "instrument_code": "471230"},
    {"ticker": "439870", "name": "KODEX 국고채30년액티브", "region": "한국",
     "duration_bucket": "long", "tracking_index": "KAP 국고채 30년 지수(액티브)",
     "hedged_or_unhedged": "원화(KRW)·환노출 없음", "data_source": "kis_domestic_daily",
     "instrument_code": "439870"},
    {"ticker": "451530", "name": "TIGER 국고채30년스트립액티브", "region": "한국",
     "duration_bucket": "long", "tracking_index": "KAP 국고채 30년 STRIP 지수(액티브)",
     "hedged_or_unhedged": "원화(KRW)·환노출 없음", "data_source": "kis_domestic_daily",
     "instrument_code": "451530"},
]

# duration_bucket 별 역할/리스크 (정성 — 거시 무관 기본 성격)
_BUCKET_ROLE = {
    "short": {
        "role": "방어/유동성 — 현금 대체에 가까움",
        "pros": ["금리변동에 가격 둔감(낮은 듀레이션)", "현금성·유동성 높음", "금리 재투자 유리"],
        "risks": ["수익(이자) 기여 작음", "큰 자본차익 기대 어려움"],
    },
    "intermediate": {
        "role": "완충 — 방어와 금리대응의 중간",
        "pros": ["단기 대비 이자수익 가산", "장기 대비 변동성 완화", "사다리(ladder) 중심"],
        "risks": ["금리상승 시 단기보다 평가손 큼"],
    },
    "long": {
        "role": "금리대응/베팅 — 금리하락 시 자본차익 큼",
        "pros": ["금리하락기 가격 상승폭 큼(높은 듀레이션)", "주식과 음의 상관 기대(위기 헤지)"],
        "risks": ["**금리상승 시 평가손 큼**", "**가격 변동성 큼(장기채 경고)**", "타이밍 의존도 높음"],
    },
}

# 장기국채 변동성 경고(불변 — 항상 동봉)
LONG_BOND_VOLATILITY_WARNING = (
    "장기국채(long, 예: 471230 중기 이상·439870·451530·TLT)는 듀레이션이 길어 "
    "**금리 1%p 상승 시 가격 손실이 크고 변동성이 높다.** 방어자산이라도 '안전 = 무변동'이 아니며, "
    "금리 인상/불확실 국면에서는 단기채 대비 평가손 위험을 반드시 감안할 것."
)

_PRODUCT_NOTE = (
    "국채 ETF 는 **방어자산(현금+국채)을 실제로 담는 운용 수단(상품)**일 뿐이다. "
    "여기 비교표는 '어떤 수단이 현 거시·계좌 목적에 맞나'를 설명하는 **추천**이며, "
    "특정 ETF 를 바로 확정하지 않는다(C안 확정 아님). 자동 주문/policy 변경 없음."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# 실 지표 연동 (KR 가격/거래량) + 정직 미연동(미국/보수율/듀레이션)
# ============================================================
def fetch_metrics(account_index: int, *, count: int = 200,
                  fetcher=None) -> dict:
    """KR 국채 ETF 5종 KIS 국내 일봉(가격/거래량)을 price_history 에 **실적재**(read-only).

    미국 3종(SHY/IEF/TLT)은 KIS 해외 미연동 → 적재 안 함(가짜 0).
    fetcher 미지정 시 price_history.KisDailyBarFetcher 사용(키는 .env·broker 가 마스킹).
    키 없거나 조회 실패는 per-code error 로 **정직히** 기록(가짜 성공 금지).
    """
    if fetcher is None:
        fetcher = price_history.KisDailyBarFetcher(account_index=account_index)
    results = []
    skipped_us = []
    for e in _UNIVERSE:
        if e["data_source"] != "kis_domestic_daily":
            skipped_us.append({"ticker": e["ticker"], "region": e["region"],
                               "reason": "KIS 해외 미연동 — 가격/거래량 unknown(가짜 0)"})
            continue
        code = e["instrument_code"]
        try:
            results.append(fetcher.fetch_and_store(code, count=count))
        except Exception as ex:  # noqa: BLE001 — 키 부재/조회 실패 정직 기록
            results.append({"ok": False, "instrument_code": code, "error": str(ex)})
    ok_n = sum(1 for r in results if r.get("ok"))
    return {
        "ok": ok_n > 0,
        "account_index": account_index,
        "read_only": True,
        "auto_order_created": False,
        "kr_fetched_codes": ok_n,
        "kr_total_codes": len(results),
        "results": results,
        "us_skipped": skipped_us,
        "note": "KR 국채 ETF 가격/거래량만 KIS 국내 일봉 실연동. 미국·보수율·듀레이션은 미연동(unknown).",
    }


def _recent_volatility(closes: list[float], window: int = 20) -> float | None:
    """최근 window 일 일간수익률 표준편차(%) — 가격 있을 때만. 데이터 부족이면 None(가짜 금지)."""
    if len(closes) < 3:
        return None
    rets = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev and prev > 0:
            rets.append(closes[i] / prev - 1.0)
    rets = rets[-window:]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round(math.sqrt(var) * 100.0, 3)


def etf_profile(ticker: str) -> dict:
    """단일 ETF 프로필. 실데이터(KR 가격/거래량) 있으면 채우고, 없으면 unknown 정직.

    반환: {ticker, name, region, duration_bucket, tracking_index, hedged_or_unhedged,
           price, volume, recent_volatility, expense_ratio, duration_years, yield,
           data_available, confidence, last_verified_at, source}
    """
    e = next((x for x in _UNIVERSE if x["ticker"] == ticker), None)
    if e is None:
        return {"ok": False, "ticker": ticker, "error": "universe 에 없는 티커(국채 ETF 후보 아님)."}

    base = {
        "ticker": e["ticker"], "name": e["name"], "region": e["region"],
        "duration_bucket": e["duration_bucket"],
        "tracking_index": e["tracking_index"],            # 정성 사실
        "hedged_or_unhedged": e["hedged_or_unhedged"],    # 정성 사실
        # 미연동(임의 수치 금지) — 항상 unknown:
        "expense_ratio": _UNKNOWN,
        "duration_years": _UNKNOWN,
        "yield": _UNKNOWN,
    }

    price = volume = vol = None
    last_dt = None
    source = None
    bars = []
    if e["data_source"] == "kis_domestic_daily" and e["instrument_code"]:
        bars = price_history.load_history(e["instrument_code"], limit=60)
        source = "kis_domestic_daily(price_history)"
    if bars:
        last = bars[-1]
        price = last.get("close")
        volume = last.get("volume")
        last_dt = last.get("date")
        closes = [b["close"] for b in bars if b.get("close") is not None]
        vol = _recent_volatility(closes)

    price_connected = price is not None
    base.update({
        "price": price if price_connected else _UNKNOWN,
        "volume": volume if (volume is not None) else _UNKNOWN,
        "recent_volatility": vol if vol is not None else _UNKNOWN,
        # data_available = 핵심 실데이터(가격) 연동 여부. 정성 사실은 별개로 항상 있음.
        "data_available": price_connected,
        "confidence": ("medium" if price_connected else "low"),
        "last_verified_at": (last_dt if price_connected else None),
        "source": (source if price_connected else
                   ("KIS 해외 미연동(미국)" if e["region"] == "미국" else "가격 미적재 — fetch_metrics 먼저")),
        "data_note": (
            "가격/거래량 KIS 국내 일봉 실연동. 보수율/듀레이션/만기수익률은 미연동(unknown)."
            if price_connected else
            ("미국 ETF — KIS 해외 미연동: 가격/거래량 unknown(가짜 0)." if e["region"] == "미국"
             else "KR ETF 이나 아직 가격 미적재 — `--fetch --account N` 실행 필요.")),
    })
    return base


# ============================================================
# 후보 비교 (거시 적합성 + 계좌 목적 적합성)
# ============================================================
def _rate_regime() -> dict:
    """현 거시 금리 국면(rate_regime) — macro_connect 실데이터 기반. 미연동이면 unknown 정직.

    return {regime: hiking|easing|elevated|uncertain|unknown, connected, detail}
    """
    try:
        from . import macro_connect
        mp = macro_connect.macro_to_portfolio()
    except Exception:  # noqa: BLE001
        return {"regime": "unknown", "connected": False,
                "detail": "거시 매핑 호출 실패 — rate_regime unknown(정직)."}
    if not mp.get("connected"):
        return {"regime": "unknown", "connected": False,
                "detail": "거시 미연동 — 금리국면 판단 불가(정직). ECOS/FRED 적재 필요."}
    tilts = mp.get("tilts", {}) or {}
    # short_bond/cash 가산 + bond_duration 음(짧게) = 고금리/인상 압력 신호.
    high_rate = any(s["name"].startswith("high_rate_") for s in mp.get("signals", []))
    inflation = any(s["name"] == "high_inflation" for s in mp.get("signals", []))
    if high_rate or inflation:
        regime = "elevated"   # 고금리/인플레 — 듀레이션 짧게 선호
    elif tilts.get("bond_duration", 0.0) < 0:
        regime = "elevated"
    else:
        regime = "uncertain"
    return {"regime": regime, "connected": True,
            "lean": mp.get("lean"),
            "detail": f"거시 lean={mp.get('lean')}, 고금리신호={high_rate}, 인플레신호={inflation}."}


def _account_purpose(account_index: int) -> dict:
    """계좌 목적/성향 — 확정안 방어구성 우선, 없으면 프로필 base. 정직 표기."""
    out = {"defensive_pct": None, "bond_ratio_pct": None,
           "duration_pref": None, "risk_tolerance": None,
           "confirmed": False, "source": "미확정"}
    try:
        from . import bond_bucket, profile as profile_mod
        prof = profile_mod.get(account_index) or {}
        out["duration_pref"] = (prof.get("bond_duration_pref") or None)
        out["risk_tolerance"] = (prof.get("risk_tolerance") or None)
        confirmed = bond_bucket._confirmed_defensive(account_index)
        if confirmed is not None:
            out.update({"defensive_pct": confirmed["defensive_pct"],
                        "bond_ratio_pct": confirmed["bond_ratio_pct"],
                        "confirmed": True,
                        "source": f"확정안({confirmed.get('variant')}) 기준 — 단일 진실"})
        else:
            br = prof.get("bond_target_pct")
            out["bond_ratio_pct"] = float(br) if br is not None else None
            out["source"] = "미확정 — 프로필 기준 미리보기"
    except Exception:  # noqa: BLE001
        pass
    return out


def _macro_fit(bucket: str, regime: str) -> tuple[str, str]:
    """duration_bucket × rate_regime → (적합성 라벨, 사유). regime unknown 이면 정직 보류."""
    if regime == "unknown":
        return ("판단보류", "거시 미연동 — 현 금리국면 기준 적합성 판단 불가(정직).")
    if regime == "elevated":   # 고금리/인플레/인상 압력
        if bucket == "short":
            return ("적합", "고금리/인상 국면 — 단기채는 금리 재투자 유리·평가손 작음.")
        if bucket == "intermediate":
            return ("중립", "고금리 국면 — 중기채는 단기 대비 이자 가산이나 평가손 위험 일부.")
        return ("주의", "고금리/인상 국면 — 장기채는 금리상승 평가손·변동성 큼(타이밍 의존).")
    # uncertain
    if bucket == "short":
        return ("적합", "금리방향 불확실 — 단기채로 방향 베팅 회피·유동성 확보.")
    if bucket == "intermediate":
        return ("적합", "불확실 — 단기·중기 사다리 분산의 중심.")
    return ("조건부", "불확실 — 장기채 단독 확대는 위험. 금리하락 베팅 시에만 일부.")


def _purpose_fit(bucket: str, purpose: dict) -> tuple[str, str]:
    """duration_bucket × 계좌 목적(선호 듀레이션/성향) → (적합성, 사유)."""
    pref = (purpose.get("duration_pref") or "").strip().lower()
    risk = (purpose.get("risk_tolerance") or "").strip().lower()
    if pref in ("short", "intermediate", "long") and pref == bucket:
        base = ("적합", f"계좌 선호 듀레이션({pref})과 일치.")
    elif pref == "mixed" and bucket in ("short", "long"):
        base = ("적합", "계좌가 mixed(사다리) — 단기/장기 조합 구성요소.")
    elif pref:
        base = ("중립", f"계좌 선호({pref})와 다른 만기대 — 보완용으로만.")
    else:
        base = ("판단보류", "계좌 듀레이션 선호 미설정 — 거시·확정안 우선.")
    # 방어적 성향인데 장기채면 주의 가산
    if bucket == "long" and risk in ("defensive", "conservative", "low", "방어"):
        return ("주의", base[1] + " 단, 방어 성향 계좌엔 장기채 변동성 부담.")
    return base


def _strength(macro_fit: str, purpose_fit: str, data_available: bool) -> str:
    """추천 강도 — 거시/계좌 적합성 + 데이터 품질 종합(보수적)."""
    score = 0
    for f in (macro_fit, purpose_fit):
        score += {"적합": 2, "중립": 1, "조건부": 0, "주의": -1,
                  "판단보류": 0}.get(f, 0)
    if not data_available:
        score -= 1   # 가격 미연동(미국 등)은 강도 낮춤(정직)
    if score >= 3:
        return "강함"
    if score >= 1:
        return "보통"
    if score <= -1:
        return "약함(주의)"
    return "낮음"


def _row_to_candidate_eval(row: dict):
    """국채 ETF 비교 행 → CandidateEvaluation(공통 SSOT). additive — 기존 출력 무변경.

    국채 ETF 비교 단계는 비중을 확정하지 않으므로 suggested_weight/max_weight=None(가짜 숫자 금지).
    """
    dq = row.get("data_quality") or {}
    available = bool(dq.get("data_available"))
    strength = row.get("recommendation_strength")
    # data_quality.confidence 는 라벨('low'/'medium'/'high') → 표준 0~1 로 변환(원 라벨 보존).
    conf_label = dq.get("confidence")
    conf_num = {"low": 0.3, "medium": 0.6, "high": 0.85}.get(str(conf_label).lower(), 0.0)
    return candidate_evaluation(
        "treasury", row.get("ticker"),
        display_name=row.get("name") or row.get("ticker") or "",
        bucket="treasury",
        fit_to_account=row.get("purpose_fit"),
        data_quality={"available": available,
                      "level": "connected" if available else "unavailable",
                      "confidence": conf_label, "source": dq.get("source")},
        confidence=conf_num,
        risk_summary={"role": row.get("role"), "risks": row.get("risks"),
                      "macro_fit": row.get("macro_fit"),
                      "recommendation_strength": strength},
        evidence_summary={"duration_bucket": row.get("duration_bucket"),
                          "classification": row.get("classification"),
                          "region": row.get("region"),
                          "tracking_index": row.get("tracking_index"),
                          "hedged_or_unhedged": row.get("hedged_or_unhedged"),
                          "expense_ratio": dq.get("expense_ratio"),
                          "yield": dq.get("yield"),
                          "duration_years": dq.get("duration_years")},
        reason_to_include=(f"{row.get('role','')} · 거시적합:{(row.get('macro_fit') or {}).get('label')}"
                           f" · 목적적합:{(row.get('purpose_fit') or {}).get('label')} (강도:{strength})"),
    )


def _excluded_to_candidate_eval(e: dict):
    return candidate_evaluation(
        "treasury", e.get("ticker"), bucket="treasury",
        data_quality={"available": False, "level": "filtered"},
        reason_to_exclude=e.get("reason", ""))


def compare_govbond_candidates(account_index: int, *,
                               duration_pref: str | None = None,
                               region: str | None = None) -> dict:
    """국채 ETF 후보 비교표 — 거시·계좌 목적 기준. **C안 바로 확정 아님(비교 제시)**.

    각 후보: 역할/장점/리스크 + 현 거시 적합성(rate_regime) + 계좌 목적 적합성 +
    추천 강도 + 데이터 품질 + 대안 + 제외 사유. 자동주문/policy 0.
    """
    regime_info = _rate_regime()
    regime = regime_info["regime"]
    purpose = _account_purpose(account_index)

    dp = (duration_pref or "").strip().lower()
    if dp == "mixed":
        wanted = {"short", "long"}
    elif dp in ("short", "intermediate", "long"):
        wanted = {dp}
    else:
        wanted = None
    rg = (region or "").strip()

    rows = []
    excluded = []
    for e in _UNIVERSE:
        # 필터(제외 사유 정직 기록)
        if wanted is not None and e["duration_bucket"] not in wanted:
            excluded.append({"ticker": e["ticker"], "reason": f"듀레이션 필터({dp}) 불일치"})
            continue
        if rg and e["region"] != rg:
            excluded.append({"ticker": e["ticker"], "reason": f"지역 필터({rg}) 불일치"})
            continue

        prof = etf_profile(e["ticker"])
        bucket = e["duration_bucket"]
        role = _BUCKET_ROLE[bucket]
        m_label, m_reason = _macro_fit(bucket, regime)
        p_label, p_reason = _purpose_fit(bucket, purpose)
        strength = _strength(m_label, p_label, prof.get("data_available", False))

        risks = list(role["risks"])
        if bucket == "long":
            risks.append("장기채 변동성 경고 적용 — 아래 long_bond_volatility_warning 참조.")

        rows.append({
            "ticker": e["ticker"], "name": e["name"], "region": e["region"],
            "duration_bucket": bucket,
            "classification": f"{e['region']}/{ {'short':'단기','intermediate':'중기','long':'장기'}[bucket] }",
            "role": role["role"],
            "pros": role["pros"],
            "risks": risks,
            "macro_fit": {"label": m_label, "reason": m_reason, "rate_regime": regime},
            "purpose_fit": {"label": p_label, "reason": p_reason,
                            "account_source": purpose["source"]},
            "recommendation_strength": strength,
            "data_quality": {
                "price": prof["price"], "volume": prof["volume"],
                "recent_volatility": prof["recent_volatility"],
                "data_available": prof["data_available"],
                "confidence": prof["confidence"],
                "expense_ratio": prof["expense_ratio"],
                "duration_years": prof["duration_years"],
                "yield": prof["yield"],
                "last_verified_at": prof["last_verified_at"],
                "source": prof["source"],
            },
            "tracking_index": e["tracking_index"],
            "hedged_or_unhedged": e["hedged_or_unhedged"],
        })

    # 대안: 같은 region 내 다른 만기대 후보를 ticker 로 제시(상호 보완).
    by_region: dict[str, list[str]] = {}
    for r in rows:
        by_region.setdefault(r["region"], []).append(r["ticker"])
    for r in rows:
        r["alternatives"] = [t for t in by_region.get(r["region"], []) if t != r["ticker"]]

    # 분류 요약(단기/장기·한국/미국)
    buckets = {"단기": [], "중기": [], "장기": []}
    by_country = {"한국": [], "미국": []}
    for r in rows:
        buckets[{"short": "단기", "intermediate": "중기", "long": "장기"}[r["duration_bucket"]]].append(r["ticker"])
        by_country.setdefault(r["region"], []).append(r["ticker"])

    return {
        "ok": True,
        "account_index": account_index,
        "rate_regime": regime_info,                 # 현 거시 금리국면(실데이터/unknown 정직)
        "account_purpose": purpose,                 # 계좌 목적(확정안/프로필)
        "candidates": rows,
        "excluded": excluded,                       # 제외 사유 정직
        # additive: 공통 CandidateEvaluation 정규화(기존 candidates/excluded 무변경).
        "normalized": ([_row_to_candidate_eval(r) for r in rows]
                       + [_excluded_to_candidate_eval(e) for e in excluded]),
        "classification": {"by_duration": buckets, "by_country": by_country},
        "long_bond_volatility_warning": LONG_BOND_VOLATILITY_WARNING,
        "decision_note": "비교 제시일 뿐 — 특정 ETF 를 바로 확정하지 않는다(C안 확정 아님).",
        "product_note": _PRODUCT_NOTE,
        "auto_order_created": False,
        "policy_changed": False,
        "as_of": _now(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int)
    ap.add_argument("--fetch", action="store_true",
                    help="KR 국채 ETF 5종 KIS 국내 일봉(가격/거래량) 실적재 (--account 필요)")
    ap.add_argument("--profile", metavar="TICKER", help="단일 ETF 프로필")
    ap.add_argument("--duration", help="short|intermediate|long|mixed (비교 필터)")
    ap.add_argument("--region", help="한국|미국 (비교 필터)")
    ap.add_argument("--count", type=int, default=200)
    args = ap.parse_args()
    try:
        if args.profile:
            out = etf_profile(args.profile)
        elif args.fetch:
            if args.account is None:
                out = {"ok": False, "error": "--fetch 에는 --account N 필요(KIS 키 소스)."}
            else:
                out = fetch_metrics(args.account, count=args.count)
        elif args.account is not None:
            out = compare_govbond_candidates(args.account, duration_pref=args.duration,
                                             region=args.region)
        else:
            out = {"ok": False,
                   "error": "--account N [--duration D --region R] | --fetch --account N | --profile TICKER"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
