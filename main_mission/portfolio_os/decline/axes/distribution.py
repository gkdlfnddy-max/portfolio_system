"""분산축(distribution) — 하락 전 '분산(distribution)' 패턴.

하락 직전 전형: **거래량 급증 + 개인(retail) 순매수 / 외국인·기관 순매도**.
스마트머니(외국인·기관)가 개미에게 넘기며 빠져나가는 구간.

데이터: KR 종목별 투자자 매매동향(KIS/키움 종목별 투자자 API).
  context["investor_flows"] = [{trade_date, foreign_net, institution_net, retail_net, volume}, ...]
    (오래된→최신, 순매수=양수/순매도=음수, 금액 또는 수량 — 부호만 사용).
  ✅ **ingestion 지점**: broker/kis_investor.py(KisInvestorFetcher) 가 KIS `inquire-investor`
     (tr_id FHKST01010900, read-only)로 외국인/기관/개인 순매수+거래량을 investor_flows 에 적재.
     데이터 없으면 data_available=False (가짜 점수 금지).

신호:
  smart_money_distribution — 최근 window 일 외국인+기관 순매도 누적 & 개인 순매수 누적
  volume_surge_on_distribution — 분산 구간에 거래량이 평균 대비 급증
  institution_buy_buffer — 기관(연기금 등 포함되는 기관계)이 순매수로 하방을 받치는 완충
    (위험 신호가 아니라 **완충** — 분산 위험 점수를 일부 감쇄. 단정 아님, 설명 중심.)

⚠️ 정직: KIS inquire-investor 는 외국인/기관/개인 **3주체만** 제공한다. '연기금'은 기관계
   (institution)에 포함될 뿐 별도 분리 데이터가 아니다 → 별도 연기금 항목을 만들지 않는다.
   "외국인 매도 = 무조건 매도" 식 단정 금지(설명·확률적 해석만).
"""
from __future__ import annotations

from .base import axis_result, clamp, sig

AXIS = "distribution"

THRESHOLDS = {
    "window_days": 10,            # 최근 N일 누적 흐름
    "min_days": 5,                # 최소 데이터 일수
    "vol_surge_ratio": 1.3,       # 거래량이 장기평균의 1.3배↑ → 급증
    "vol_surge_max_ratio": 2.5,   # 2.5배 → severity 만점
    "dist_days_frac": 0.5,        # window 중 '분산일(스마트 매도+개인 매수)' 비율 50%↑
    "buffer_inst_days_frac": 0.5, # window 중 기관 순매수일 비율 50%↑ → 완충 신호
    "buffer_max_damp": 0.4,       # 완충이 분산 위험을 최대 40%까지 감쇄(전면 상쇄 금지)
}


def _net(row: dict, key: str) -> float | None:
    v = row.get(key)
    return None if v is None else float(v)


def score(context: dict) -> dict:
    flows = context.get("investor_flows")
    if not flows or not isinstance(flows, list):
        return axis_result(AXIS, data_available=False,
                           detail="투자자 매매동향 미연동 — 분산축 데이터 없음")

    t = THRESHOLDS
    flows = sorted(flows, key=lambda r: r.get("trade_date", ""))
    window = flows[-t["window_days"]:]
    if len(window) < t["min_days"]:
        return axis_result(AXIS, data_available=False,
                           detail=f"투자자 매매동향 {len(window)}일 < 최소 {t['min_days']}일")

    # 분산일: 외국인+기관 순매도(<0) AND 개인 순매수(>0)
    dist_days = 0
    valid_days = 0
    smart_cum = 0.0
    retail_cum = 0.0
    inst_cum = 0.0          # 기관계 누적(연기금 포함 — 분리 데이터 아님)
    inst_buy_days = 0       # 기관 순매수일 수(방어 매수 완충 판단)
    inst_valid_days = 0
    for r in window:
        fn = _net(r, "foreign_net")
        inn = _net(r, "institution_net")
        rt = _net(r, "retail_net")
        if fn is None and inn is None and rt is None:
            continue
        valid_days += 1
        smart = (fn or 0.0) + (inn or 0.0)
        smart_cum += smart
        if rt is not None:
            retail_cum += rt
        if inn is not None:
            inst_valid_days += 1
            inst_cum += inn
            if inn > 0:
                inst_buy_days += 1
        if smart < 0 and (rt is None or rt > 0):
            dist_days += 1
    if valid_days < t["min_days"]:
        return axis_result(AXIS, data_available=False,
                           detail=f"유효 투자자 데이터 {valid_days}일 부족")

    dist_frac = dist_days / valid_days
    signals = []

    # 신호1: 스마트머니 분산
    smart_distribution_fired = dist_frac >= t["dist_days_frac"] and smart_cum < 0
    sev1 = clamp((dist_frac - t["dist_days_frac"]) / (1.0 - t["dist_days_frac"])) if smart_distribution_fired else 0.0
    signals.append(sig(
        "smart_money_distribution", smart_distribution_fired, round(dist_frac, 2), sev1,
        f"최근 {valid_days}일 중 분산일(외국인·기관 순매도+개인 순매수) {dist_days}일 "
        f"({dist_frac*100:.0f}%), 스마트머니 누적 {smart_cum:+.0f}"))

    # 신호2: 분산 구간 거래량 급증 (volume 있을 때만)
    vols = [float(r["volume"]) for r in flows if r.get("volume") is not None]
    if len(vols) >= 20:
        recent_vol = sum(float(r["volume"]) for r in window if r.get("volume") is not None)
        recent_n = sum(1 for r in window if r.get("volume") is not None)
        long_vol = sum(vols[-40:]) / min(40, len(vols))
        if recent_n > 0 and long_vol > 0:
            ratio = (recent_vol / recent_n) / long_vol
            surge_fired = ratio >= t["vol_surge_ratio"] and smart_cum < 0
            sev2 = clamp((ratio - t["vol_surge_ratio"]) / (t["vol_surge_max_ratio"] - t["vol_surge_ratio"])) if surge_fired else 0.0
            signals.append(sig(
                "volume_surge_on_distribution", surge_fired, round(ratio, 2), sev2,
                f"분산 구간 거래량 평균比 {ratio:.2f}x"))

    # 신호3(완충): 기관 방어 매수 — 기관계가 순매수로 하방을 받침(위험 감쇄).
    #   '세력 이탈'의 반대 — 연기금 등 기관계가 조정 중에 받아주는 구간.
    #   위험 신호가 아니므로 fired 이어도 위험점수에 더하지 않고, 분산 위험을 감쇄한다.
    inst_buy_frac = (inst_buy_days / inst_valid_days) if inst_valid_days else 0.0
    buffer_fired = (inst_valid_days >= t["min_days"]
                    and inst_buy_frac >= t["buffer_inst_days_frac"]
                    and inst_cum > 0)
    buf_strength = clamp((inst_buy_frac - t["buffer_inst_days_frac"])
                         / (1.0 - t["buffer_inst_days_frac"])) if buffer_fired else 0.0
    signals.append(sig(
        "institution_buy_buffer", buffer_fired, round(inst_buy_frac, 2), buf_strength,
        f"기관계(연기금 등 포함) 순매수일 {inst_buy_days}/{inst_valid_days}"
        f"({inst_buy_frac*100:.0f}%), 기관 누적 {inst_cum:+.0f}"
        + (" — 하방 방어 매수(완충)" if buffer_fired else "")))

    # 축 위험점수: 두 위험신호 severity 가중(분산 0.65 + 거래량 0.35) → 0~100
    weights = {"smart_money_distribution": 0.65, "volume_surge_on_distribution": 0.35}
    risk_signals = [s for s in signals if s["name"] in weights]
    score01 = sum(s["severity"] * weights[s["name"]] for s in risk_signals if s["fired"])
    # 거래량 신호가 없으면(데이터 부족) 분산 신호만으로 정규화
    present = {s["name"] for s in risk_signals}
    norm = sum(w for k, w in weights.items() if k in present) or 1.0
    risk = clamp(score01 / norm) * 100.0

    # 완충 적용: 기관 방어 매수가 있으면 분산 위험을 일부 감쇄(전면 상쇄 금지 — 단정 회피).
    if buffer_fired and risk > 0:
        damp = t["buffer_max_damp"] * buf_strength
        risk = risk * (1.0 - damp)

    # confidence: 데이터 일수 + 거래량 보유 여부
    conf = 0.4 + 0.4 * min(1.0, valid_days / float(t["window_days"]))
    if "volume_surge_on_distribution" in present:
        conf += 0.1
    fired_names = [s["name"] for s in signals if s["fired"]]
    # 설명 중심(단정 금지) — 무엇이 발화했고 어떻게 해석하는지 한글로.
    if smart_distribution_fired:
        parts = [f"외국인·기관 동반 순매도 + 개인 순매수가 {dist_days}일 — "
                 f"스마트머니가 개인에게 넘기는 분산 패턴 의심(확정 아님)"]
        if "volume_surge_on_distribution" in [s["name"] for s in signals if s["fired"]]:
            parts.append("거래량 급증 동반 — 분산 강도↑")
        if buffer_fired:
            parts.append("다만 기관 방어 매수가 일부 받쳐 위험 감쇄")
        detail = "분산축 위험 {:.0f}: ".format(risk) + "; ".join(parts)
    elif buffer_fired:
        detail = (f"분산축 위험 {risk:.0f}: 분산 신호 약함, 기관계 순매수로 하방 방어 우세 — "
                  "수급상 비교적 견조(단정 아님)")
    else:
        detail = f"분산축 위험 {risk:.0f}: 뚜렷한 분산/방어 신호 없음(중립)"
    return axis_result(AXIS, risk_0_100=risk, signals=signals,
                       data_available=True, confidence=conf, detail=detail)
