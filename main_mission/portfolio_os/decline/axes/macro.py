"""거시축(macro) — 과열·금리·신용 팽창·인플레·환율.

하락을 부르는 거시 환경: 금리 인상기·장단기 금리 역전(yield curve inversion)·
신용/대출 팽창·고인플레·환율 급변동.

데이터: ECOS(한국은행)/FRED.
  context["macro_indicators"] = {
     "policy_rate": float,           # 기준금리 %
     "policy_rate_change_3m": float, # 최근 3개월 기준금리 변화 %p (인상기 판단)
     "yield_10y": float, "yield_2y": float,   # 장단기 금리 → 역전 판단
     "cpi_yoy": float,               # 소비자물가 전년比 %
     "credit_growth_yoy": float,     # 가계신용/대출 전년比 %
     "fx_usdkrw": float, "fx_usdkrw_change_1m": float,  # 환율 + 1개월 변화%
  }
  ⚠️ **ingestion 지점**: ECOS/FRED 적재 (미연동 — store schema 의 macro_indicators 테이블).
     지표가 하나도 없으면 data_available=False (가짜 점수 금지).
     일부만 있으면 가용 지표로만 계산하고 confidence 를 그만큼 낮춘다(정직).
"""
from __future__ import annotations

from .base import axis_result, clamp, sig

AXIS = "macro"

THRESHOLDS = {
    "rate_hiking_3m_pp": 0.25,     # 3개월 +0.25%p↑ → 인상기
    "rate_hiking_max_pp": 1.5,     # +1.5%p → severity 만점
    "curve_invert_warn": 0.0,      # 10y-2y ≤ 0 → 역전(경기침체 선행)
    "cpi_high": 4.0,               # CPI yoy 4%↑ → 고인플레
    "cpi_max": 8.0,                # 8% → severity 만점
    "credit_high": 8.0,            # 신용 yoy 8%↑ → 과잉 팽창
    "credit_max": 20.0,
    "fx_shock_1m": 3.0,            # 환율 1개월 ±3%↑ → 충격
    "fx_shock_max": 10.0,
}


def _has(m: dict, *keys: str) -> bool:
    return all(m.get(k) is not None for k in keys)


def score(context: dict) -> dict:
    m = context.get("macro_indicators")
    if not m or not isinstance(m, dict):
        return axis_result(AXIS, data_available=False,
                           detail="거시지표 미연동 — macro 데이터 없음 (ECOS/FRED)")

    t = THRESHOLDS
    signals = []
    available_factors = 0   # 계산에 실제 쓰인 지표 군 수 → confidence 산정

    # 1) 금리 인상기
    if m.get("policy_rate_change_3m") is not None:
        available_factors += 1
        chg = float(m["policy_rate_change_3m"])
        fired = chg >= t["rate_hiking_3m_pp"]
        sev = clamp((chg - t["rate_hiking_3m_pp"]) / (t["rate_hiking_max_pp"] - t["rate_hiking_3m_pp"])) if fired else 0.0
        signals.append(sig("rate_hiking", fired, round(chg, 2), sev,
                           f"3개월 기준금리 변화 {chg:+.2f}%p"))

    # 2) 장단기 금리 역전
    if _has(m, "yield_10y", "yield_2y"):
        available_factors += 1
        spread = float(m["yield_10y"]) - float(m["yield_2y"])
        fired = spread <= t["curve_invert_warn"]
        # 역전 폭 -1.0%p 에서 만점
        sev = clamp(-spread / 1.0) if fired else 0.0
        signals.append(sig("yield_curve_inversion", fired, round(spread, 2), sev,
                           f"10y-2y 스프레드 {spread:+.2f}%p" + (" (역전)" if fired else "")))

    # 3) 고인플레
    if m.get("cpi_yoy") is not None:
        available_factors += 1
        cpi = float(m["cpi_yoy"])
        fired = cpi >= t["cpi_high"]
        sev = clamp((cpi - t["cpi_high"]) / (t["cpi_max"] - t["cpi_high"])) if fired else 0.0
        signals.append(sig("high_inflation", fired, round(cpi, 2), sev,
                           f"CPI 전년比 {cpi:.1f}%"))

    # 4) 신용 팽창
    if m.get("credit_growth_yoy") is not None:
        available_factors += 1
        cg = float(m["credit_growth_yoy"])
        fired = cg >= t["credit_high"]
        sev = clamp((cg - t["credit_high"]) / (t["credit_max"] - t["credit_high"])) if fired else 0.0
        signals.append(sig("credit_expansion", fired, round(cg, 2), sev,
                           f"신용/대출 전년比 {cg:.1f}%"))

    # 5) 환율 충격
    if m.get("fx_usdkrw_change_1m") is not None:
        available_factors += 1
        fx = abs(float(m["fx_usdkrw_change_1m"]))
        fired = fx >= t["fx_shock_1m"]
        sev = clamp((fx - t["fx_shock_1m"]) / (t["fx_shock_max"] - t["fx_shock_1m"])) if fired else 0.0
        signals.append(sig("fx_shock", fired, round(float(m["fx_usdkrw_change_1m"]), 2), sev,
                           f"USD/KRW 1개월 변화 {float(m['fx_usdkrw_change_1m']):+.1f}%"))

    if available_factors == 0:
        return axis_result(AXIS, data_available=False,
                           detail="거시지표 dict 있으나 인식 가능한 지표 없음")

    # 축 위험: 발화 신호 severity 평균(가용 지표군 수로 정규화) → 0~100
    weights = {
        "rate_hiking": 0.22, "yield_curve_inversion": 0.28, "high_inflation": 0.20,
        "credit_expansion": 0.18, "fx_shock": 0.12,
    }
    present = {s["name"] for s in signals}
    score01 = sum(s["severity"] * weights.get(s["name"], 0.0) for s in signals if s["fired"])
    norm = sum(w for k, w in weights.items() if k in present) or 1.0
    risk = clamp(score01 / norm) * 100.0

    # confidence: 5개 지표군 중 몇 개나 가용한가 (정직 — 적으면 낮춤)
    conf = 0.3 + 0.7 * (available_factors / 5.0)
    fired_names = [s["name"] for s in signals if s["fired"]]
    detail = (f"거시축 위험 {risk:.0f} (지표 {available_factors}/5 가용), "
              + (f"발화: {', '.join(fired_names)}" if fired_names else "거시 경고 없음"))
    return axis_result(AXIS, risk_0_100=risk, signals=signals,
                       data_available=True, confidence=conf, detail=detail)
