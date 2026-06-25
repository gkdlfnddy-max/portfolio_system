"""심리축(sentiment) — VIX·VKOSPI·풋콜비율·신용잔고·거래대금 → 공포/과열 조정.

**거시축과 분리**: 거시(macro)=금리/환율/유가/인플레, 심리(sentiment)=변동성/공포/위험회피.

두 방향:
  - **공포(VIX/VKOSPI 급등·풋콜 급등)** = 이미 변동성 확대/하락 진행 → 위험↑.
  - **과열(신용잔고 급증·VIX 바닥)** = 레버리지 누적/안일 → 향후 하락 취약성↑.

데이터: sentiment_index 테이블 (적재는 event_calendar.upsert_sentiment).
  context["sentiment_index"] = {
     "vix": float,                       # 미국 변동성지수
     "vkospi": float,                    # 한국 변동성지수(코스피200 변동성)
     "put_call_ratio": float,            # 풋/콜 비율 (1.0↑ = 공포)
     "margin_balance_change_1m": float,  # 신용잔고 1개월 변화% (급증=레버리지 과잉)
     "trading_value_change": float,      # 거래대금 1일 변화% (급증=과열/투매 양방향 변동성)
  }
  ⚠️ **ingestion 지점**: VIX/VKOSPI/풋콜/신용/거래대금 적재(sentiment_index 테이블).
     지표 하나도 없으면 data_available=False.
  ⚠️ **과장 금지**: VIX 하나만 있으면 confidence 낮게 — "심리축 완성" 단정 금지.

신호:
  fear_spike       — VIX/VKOSPI/풋콜 급등(공포)
  leverage_buildup — 신용잔고 급증(과열 취약성)
  volume_surge     — 거래대금 급증(과열/투매 — 변동성 확대 신호)
"""
from __future__ import annotations

from .base import axis_result, clamp, sig

AXIS = "sentiment"

THRESHOLDS = {
    "vix_elevated": 20.0,      # VIX 20↑ → 불안
    "vix_panic": 40.0,         # 40 → severity 만점
    "vkospi_elevated": 20.0,   # VKOSPI 도 동일 스케일(변동성지수)
    "vkospi_panic": 40.0,
    "putcall_fear": 1.0,       # 풋콜 1.0↑ → 공포
    "putcall_max": 1.5,
    "margin_surge_1m": 5.0,    # 신용잔고 1개월 +5%↑ → 레버리지 과잉
    "margin_surge_max": 20.0,
    "volume_surge": 30.0,      # 거래대금 1일 +30%↑ → 변동성 확대(과열/투매)
    "volume_surge_max": 100.0,
}

# 심리축 정식 지표(데이터 충분성/confidence 계산 기준) — 과장 방지.
_FACTOR_KEYS = ["vix", "vkospi", "put_call_ratio", "margin_balance_change_1m",
                "trading_value_change"]


def score(context: dict) -> dict:
    s = context.get("sentiment_index")
    if not s or not isinstance(s, dict):
        return axis_result(AXIS, data_available=False,
                           detail="심리지표 미연동 — sentiment 데이터 없음 (VIX/풋콜/신용)")

    t = THRESHOLDS
    signals = []
    available_factors = 0

    # 공포: VIX / VKOSPI / 풋콜 중 큰 severity 채택 (변동성·공포 = 한 신호)
    fear_sev = 0.0
    fear_value = None
    fear_detail = []
    has_fear_factor = False
    if s.get("vix") is not None:
        available_factors += 1
        has_fear_factor = True
        vix = float(s["vix"])
        if vix >= t["vix_elevated"]:
            fear_sev = max(fear_sev, clamp((vix - t["vix_elevated"]) / (t["vix_panic"] - t["vix_elevated"])))
        fear_value = vix
        fear_detail.append(f"VIX {vix:.0f}")
    if s.get("vkospi") is not None:
        available_factors += 1
        has_fear_factor = True
        vk = float(s["vkospi"])
        if vk >= t["vkospi_elevated"]:
            fear_sev = max(fear_sev, clamp((vk - t["vkospi_elevated"]) / (t["vkospi_panic"] - t["vkospi_elevated"])))
        if fear_value is None:
            fear_value = vk
        fear_detail.append(f"VKOSPI {vk:.0f}")
    if s.get("put_call_ratio") is not None:
        available_factors += 1
        has_fear_factor = True
        pc = float(s["put_call_ratio"])
        if pc >= t["putcall_fear"]:
            fear_sev = max(fear_sev, clamp((pc - t["putcall_fear"]) / (t["putcall_max"] - t["putcall_fear"])))
        fear_detail.append(f"풋콜 {pc:.2f}")
    if has_fear_factor:
        signals.append(sig("fear_spike", fear_sev > 0, fear_value, fear_sev,
                           "공포지표 " + (", ".join(fear_detail) if fear_detail else "")))

    # 과열: 신용잔고 급증
    if s.get("margin_balance_change_1m") is not None:
        available_factors += 1
        mg = float(s["margin_balance_change_1m"])
        fired = mg >= t["margin_surge_1m"]
        sev = clamp((mg - t["margin_surge_1m"]) / (t["margin_surge_max"] - t["margin_surge_1m"])) if fired else 0.0
        signals.append(sig("leverage_buildup", fired, round(mg, 2), sev,
                           f"신용잔고 1개월 {mg:+.1f}%"))

    # 거래대금 급증 → 변동성 확대(과열/투매 양방향)
    if s.get("trading_value_change") is not None:
        available_factors += 1
        tv = float(s["trading_value_change"])
        fired = tv >= t["volume_surge"]
        sev = clamp((tv - t["volume_surge"]) / (t["volume_surge_max"] - t["volume_surge"])) if fired else 0.0
        signals.append(sig("volume_surge", fired, round(tv, 1), sev,
                           f"거래대금 {tv:+.0f}%"))

    if available_factors == 0:
        return axis_result(AXIS, data_available=False,
                           detail="심리지표 dict 있으나 인식 가능한 지표 없음")

    weights = {"fear_spike": 0.5, "leverage_buildup": 0.3, "volume_surge": 0.2}
    present = {s_["name"] for s_ in signals}
    score01 = sum(s_["severity"] * weights.get(s_["name"], 0.0) for s_ in signals if s_["fired"])
    norm = sum(w for k, w in weights.items() if k in present) or 1.0
    risk = clamp(score01 / norm) * 100.0

    # confidence = 데이터 충분성. 정식 지표 5종 기준 — VIX 하나면 낮음(과장 금지).
    total_factors = len(_FACTOR_KEYS)
    conf = 0.3 + 0.7 * (available_factors / float(total_factors))
    fired_names = [s_["name"] for s_ in signals if s_["fired"]]
    detail = (f"심리축 위험 {risk:.0f} (지표 {available_factors}/{total_factors}), "
              + (f"발화: {', '.join(fired_names)}" if fired_names else "심리 경고 없음"))
    return axis_result(AXIS, risk_0_100=risk, signals=signals,
                       data_available=True, confidence=conf, detail=detail)
