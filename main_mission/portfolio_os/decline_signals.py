"""하락 징후 분석 엔진(Pre-Decline Signal Engine) — 순수 함수.

가격이력 시계열(list of {date, close, high, low, volume})에서 **하락 전 특징**을
계산한다. Anthropic API 미사용 — 전부 결정론 규칙(이동평균/RSI/변동성/거래량/낙폭).
"지능"은 이 규칙 신호 + Claude+메모리 성장(노하우 누적)으로 구성된다.

설계 원칙:
  - 부수효과 없음(DB 접근 없음). 입력=시계열, 출력=신호 dict. 테스트·백테스트 재사용.
  - 데이터 부족 시 발화하지 않음(NotEnoughData) — 거짓 경보 금지.
  - 임계값은 모듈 상수(THRESHOLDS) + 설명. 하드코딩이 아니라 "config 의미"(조정 가능).
  - 자동매매 절대 없음 — 신호는 사실(fact)만, 행동(주문)은 사람 승인.

각 신호: {name, fired: bool, value, severity(0~1), detail}
종합: compute_signals() → {risk_score 0~100, fired: [names], signals: [...], data_points}
"""
from __future__ import annotations

import math
from typing import Any

# ============================================================
# 임계값 (config 의미 — KIS/키움 일봉 데이터에 맞춰 조정 가능. 하드코딩 아님)
# ============================================================
THRESHOLDS: dict[str, float] = {
    # 가격-MA 이격(과열): 200일선 대비 +N% 이상이면 과열 경계
    "ext_ma200_warn_pct": 20.0,      # 20% 이상 위 → 과열 시작
    "ext_ma200_max_pct": 50.0,       # 50% 이상 위 → severity 만점
    # RSI 과매수
    "rsi_period": 14.0,
    "rsi_overbought": 70.0,          # 70 이상 과매수
    "rsi_extreme": 80.0,             # 80 이상 → severity 만점
    # 변동성 급증: 최근 ATR%(또는 표준편차)가 장기 평균 대비 배수
    "vol_spike_ratio": 1.5,          # 단기 변동성이 장기의 1.5배 이상
    "vol_spike_max_ratio": 3.0,      # 3배 → severity 만점
    # MA 기울기 둔화/하락 전환 (20일선 기울기, % per day)
    "ma_slope_flat_pct": 0.0,        # 0 이하(하락 전환) → 발화
    # 데드크로스 근접: 단기 MA 가 장기 MA 위에 있으나 격차가 좁아짐(% 이내)
    "deadcross_near_pct": 1.0,       # 단기-장기 격차가 종가의 1% 이내로 좁아짐
    # 고점대비 낙폭(이미 하락 진행): 최근 고점 대비
    "drawdown_warn_pct": 7.0,        # -7% → 추세 약화 경계
    "drawdown_max_pct": 25.0,        # -25% → severity 만점
    # 거래량 다이버전스: 가격 N일 상승했는데 거래량이 평균 대비 감소
    "vol_div_lookback": 10.0,        # 최근 10일 가격 추세
    "vol_div_drop_ratio": 0.8,       # 최근 거래량이 장기평균의 80% 미만
}

# 종합 위험점수에서 각 신호의 가중치 (합 = 1.0). 발화 신호의 severity 가중합 → 0~100.
SIGNAL_WEIGHTS: dict[str, float] = {
    "overextended_ma200": 0.18,      # 200일선 과열
    "rsi_overbought": 0.16,          # RSI 과매수
    "volatility_spike": 0.16,        # 변동성 급증
    "ma_trend_weakening": 0.16,      # 추세(20일 기울기) 둔화/전환
    "deadcross_proximity": 0.14,     # 데드크로스 근접
    "drawdown_from_high": 0.12,      # 고점대비 낙폭
    "volume_divergence": 0.08,       # 거래량 다이버전스
}


class NotEnoughData(Exception):
    """신호 계산에 필요한 최소 데이터(기간)가 없을 때."""


# ============================================================
# 기본 시계열 헬퍼 (순수)
# ============================================================
def _closes(history: list[dict]) -> list[float]:
    return [float(h["close"]) for h in history if h.get("close") is not None]


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def sma(values: list[float], period: int) -> float | None:
    """단순이동평균. 데이터 부족이면 None."""
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def sma_series(values: list[float], period: int) -> list[float]:
    """각 시점의 SMA 시계열(앞쪽 period-1 개는 제외). 기울기 계산용."""
    if period <= 0 or len(values) < period:
        return []
    out = []
    run = sum(values[:period])
    out.append(run / period)
    for i in range(period, len(values)):
        run += values[i] - values[i - period]
        out.append(run / period)
    return out


def rsi(values: list[float], period: int = 14) -> float | None:
    """Wilder RSI. 데이터 부족이면 None."""
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    # 초기 평균 (첫 period 변화)
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    # Wilder smoothing
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr_pct(history: list[dict], period: int = 14) -> float | None:
    """ATR 를 종가 대비 %로. high/low 없으면 종가 변화폭으로 근사."""
    if len(history) < period + 1:
        return None
    trs = []
    for i in range(1, len(history)):
        h = history[i]
        prev_close = float(history[i - 1]["close"])
        hi = float(h["high"]) if h.get("high") is not None else float(h["close"])
        lo = float(h["low"]) if h.get("low") is not None else float(h["close"])
        tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[-period:]) / period
    last_close = float(history[-1]["close"])
    if last_close == 0:
        return None
    return (atr / last_close) * 100.0


def stdev_pct(values: list[float], period: int) -> float | None:
    """최근 period 일 일간수익률의 표준편차(%) — ATR 대체/보조."""
    if len(values) < period + 1:
        return None
    rets = []
    for i in range(len(values) - period, len(values)):
        if i == 0 or values[i - 1] == 0:
            continue
        rets.append((values[i] - values[i - 1]) / values[i - 1])
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * 100.0


# ============================================================
# 개별 신호 (각각 {name, fired, value, severity, detail})
# ============================================================
def _sig(name: str, fired: bool, value: Any, severity: float, detail: str) -> dict:
    return {"name": name, "fired": bool(fired), "value": value,
            "severity": round(_clamp(severity), 3), "detail": detail}


def signal_overextended_ma200(history: list[dict]) -> dict:
    """가격이 200일선 대비 과도하게 위(과열). 200일 없으면 가능한 최장 MA 로 폴백."""
    closes = _closes(history)
    name = "overextended_ma200"
    ma = sma(closes, 200) or sma(closes, 120) or sma(closes, 60)
    if ma is None or ma == 0:
        return _sig(name, False, None, 0.0, "장기 이동평균 계산 불가(데이터 부족)")
    last = closes[-1]
    ext_pct = (last / ma - 1.0) * 100.0
    warn = THRESHOLDS["ext_ma200_warn_pct"]
    mx = THRESHOLDS["ext_ma200_max_pct"]
    fired = ext_pct >= warn
    sev = _clamp((ext_pct - warn) / (mx - warn)) if fired else 0.0
    return _sig(name, fired, round(ext_pct, 2), sev,
                f"장기선 대비 이격 +{ext_pct:.1f}% (경계 +{warn:.0f}%)")


def signal_rsi_overbought(history: list[dict]) -> dict:
    closes = _closes(history)
    name = "rsi_overbought"
    period = int(THRESHOLDS["rsi_period"])
    r = rsi(closes, period)
    if r is None:
        return _sig(name, False, None, 0.0, f"RSI({period}) 계산 불가(데이터 부족)")
    ob = THRESHOLDS["rsi_overbought"]
    ex = THRESHOLDS["rsi_extreme"]
    fired = r >= ob
    sev = _clamp((r - ob) / (ex - ob)) if fired else 0.0
    return _sig(name, fired, round(r, 1), sev, f"RSI {r:.0f} (과매수 {ob:.0f}+)")


def signal_volatility_spike(history: list[dict]) -> dict:
    """단기 변동성(ATR%)이 장기 평균 변동성 대비 급증 — 추세 불안정."""
    name = "volatility_spike"
    short = atr_pct(history, 14)
    long = atr_pct(history, 60) or atr_pct(history, 40)
    if short is None or long is None or long == 0:
        # ATR 불가 시 표준편차로 폴백
        closes = _closes(history)
        short = stdev_pct(closes, 10)
        long = stdev_pct(closes, 40)
        if short is None or long is None or long == 0:
            return _sig(name, False, None, 0.0, "변동성 비교 불가(데이터 부족)")
    ratio = short / long
    warn = THRESHOLDS["vol_spike_ratio"]
    mx = THRESHOLDS["vol_spike_max_ratio"]
    fired = ratio >= warn
    sev = _clamp((ratio - warn) / (mx - warn)) if fired else 0.0
    return _sig(name, fired, round(ratio, 2), sev,
                f"단기/장기 변동성 비 {ratio:.2f}x (경계 {warn:.1f}x)")


def signal_ma_trend_weakening(history: list[dict]) -> dict:
    """20일선 기울기가 둔화/하락 전환 — 상승추세 약화."""
    name = "ma_trend_weakening"
    closes = _closes(history)
    series = sma_series(closes, 20)
    if len(series) < 6:
        return _sig(name, False, None, 0.0, "20일선 기울기 계산 불가(데이터 부족)")
    recent = series[-1]
    past = series[-6]  # 5거래일 전 MA
    if past == 0:
        return _sig(name, False, None, 0.0, "20일선 기울기 계산 불가")
    slope_pct = (recent / past - 1.0) * 100.0  # 5일간 MA 변화율
    flat = THRESHOLDS["ma_slope_flat_pct"]
    fired = slope_pct <= flat  # 0 이하 = 하락 전환/평탄
    # severity: 0%(평탄)에서 시작 -> -5%(가파른 하락)에서 만점
    sev = _clamp((-slope_pct) / 5.0) if fired else 0.0
    return _sig(name, fired, round(slope_pct, 2), sev,
                f"20일선 5일 기울기 {slope_pct:+.1f}% (둔화/하락 전환)")


def signal_deadcross_proximity(history: list[dict]) -> dict:
    """단기(20)·장기(60) MA 데드크로스 근접. 단기가 위에 있으나 격차가 좁아질 때."""
    name = "deadcross_proximity"
    closes = _closes(history)
    fast = sma(closes, 20)
    slow = sma(closes, 60)
    if fast is None or slow is None:
        return _sig(name, False, None, 0.0, "단/장기선 계산 불가(데이터 부족)")
    last = closes[-1]
    if last == 0:
        return _sig(name, False, None, 0.0, "종가 0")
    gap_pct = (fast - slow) / last * 100.0  # 양수=정배열(단기>장기), 음수=이미 역배열
    near = THRESHOLDS["deadcross_near_pct"]
    # 발화: 아직 정배열이지만 격차가 near% 이내로 좁음 (임박), 또는 막 역배열로 진입(음수)
    fired = gap_pct <= near
    if not fired:
        sev = 0.0
    elif gap_pct <= 0:
        sev = 1.0  # 이미 데드크로스
    else:
        sev = _clamp(1.0 - gap_pct / near)
    state = "이미 역배열" if gap_pct <= 0 else "데드크로스 임박"
    return _sig(name, fired, round(gap_pct, 2), sev,
                f"단기-장기선 격차 {gap_pct:+.2f}% ({state})")


def signal_drawdown_from_high(history: list[dict]) -> dict:
    """최근 고점(lookback 내) 대비 낙폭 — 이미 하락이 시작된 정도."""
    name = "drawdown_from_high"
    closes = _closes(history)
    if len(closes) < 20:
        return _sig(name, False, None, 0.0, "낙폭 계산 불가(데이터 부족)")
    window = closes[-60:] if len(closes) >= 60 else closes
    peak = max(window)
    if peak == 0:
        return _sig(name, False, None, 0.0, "고점 0")
    dd_pct = (closes[-1] / peak - 1.0) * 100.0  # 음수
    warn = THRESHOLDS["drawdown_warn_pct"]
    mx = THRESHOLDS["drawdown_max_pct"]
    drop = -dd_pct
    fired = drop >= warn
    sev = _clamp((drop - warn) / (mx - warn)) if fired else 0.0
    return _sig(name, fired, round(dd_pct, 2), sev,
                f"최근 고점대비 {dd_pct:.1f}% (경계 -{warn:.0f}%)")


def signal_volume_divergence(history: list[dict]) -> dict:
    """가격은 (최근 N일) 상승했는데 거래량이 평균 대비 감소 — 상승 동력 약화."""
    name = "volume_divergence"
    closes = _closes(history)
    vols = [float(h["volume"]) for h in history if h.get("volume") is not None]
    look = int(THRESHOLDS["vol_div_lookback"])
    if len(closes) < look + 1 or len(vols) < 40:
        return _sig(name, False, None, 0.0, "거래량 다이버전스 계산 불가(거래량/기간 부족)")
    price_change = (closes[-1] / closes[-1 - look] - 1.0) * 100.0
    recent_vol = sum(vols[-look:]) / look
    long_vol = sum(vols[-40:]) / 40
    if long_vol == 0:
        return _sig(name, False, None, 0.0, "거래량 평균 0")
    vol_ratio = recent_vol / long_vol
    drop_ratio = THRESHOLDS["vol_div_drop_ratio"]
    # 발화: 가격 상승(+) + 거래량이 평균의 drop_ratio 미만
    fired = price_change > 0 and vol_ratio < drop_ratio
    sev = _clamp((drop_ratio - vol_ratio) / drop_ratio) if fired else 0.0
    return _sig(name, fired, round(vol_ratio, 2), sev,
                f"가격 {price_change:+.1f}% / 거래량 평균比 {vol_ratio:.2f}x (동력 약화)")


# 등록된 모든 신호 (순서 = 보고 순서)
_SIGNAL_FUNCS = [
    signal_overextended_ma200,
    signal_rsi_overbought,
    signal_volatility_spike,
    signal_ma_trend_weakening,
    signal_deadcross_proximity,
    signal_drawdown_from_high,
    signal_volume_divergence,
]

MIN_DATA_POINTS = 20  # 어떤 신호든 의미있게 계산하려면 최소 20거래일


def compute_signals(history: list[dict], *, min_points: int = MIN_DATA_POINTS) -> dict:
    """가격이력 → 전체 신호 + 종합 위험점수(0~100).

    history: list of {date, close, (high), (low), (volume)} — 오래된 → 최신 순.
    raise NotEnoughData if 데이터가 min_points 미만.

    반환:
      {
        "data_points": int,
        "signals": [ {name, fired, value, severity, detail}, ... ],
        "fired": [name, ...],            # 발화한 신호 이름
        "risk_score": 0~100,             # severity*weight 가중합 (발화만)
        "risk_level": "low|elevated|high|severe",
      }
    """
    if not isinstance(history, list):
        raise NotEnoughData("history must be a list")
    # 날짜 정렬(있으면) — 오래된→최신 보장
    hist = sorted(history, key=lambda h: h.get("date", "")) if all(h.get("date") for h in history) else list(history)
    closes = _closes(hist)
    if len(closes) < min_points:
        raise NotEnoughData(f"가격이력 {len(closes)}개 < 최소 {min_points}개")

    signals = [fn(hist) for fn in _SIGNAL_FUNCS]
    fired = [s["name"] for s in signals if s["fired"]]

    # 종합 위험점수: 발화 신호의 severity * weight 합 (0~1) → *100
    score01 = 0.0
    for s in signals:
        if s["fired"]:
            score01 += s["severity"] * SIGNAL_WEIGHTS.get(s["name"], 0.0)
    risk_score = round(_clamp(score01) * 100.0, 1)

    return {
        "data_points": len(closes),
        "signals": signals,
        "fired": fired,
        "risk_score": risk_score,
        "risk_level": risk_level(risk_score),
    }


def risk_level(score: float) -> str:
    if score >= 60:
        return "severe"
    if score >= 35:
        return "high"
    if score >= 15:
        return "elevated"
    return "low"
