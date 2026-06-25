"""메타인지 종합(composite) — 6축을 입체 합성 + 자기 신뢰도 가늠 + 성장.

핵심:
  1. 6축 scorer 실행 → 가용 축(data_available=True)만 합성. 미연동 축 제외(가짜 점수 0).
  2. **가중치 = 데이터 가용성 × 과거 예측 적중 신뢰도(track record)**.
     - 데이터 가용성 = 그 축의 confidence(데이터 양/질).
     - track record = lessons(scope='axis')에서 읽은 적중률(reliability). 쓸수록 정교(성장).
  3. **메타인지 출력**: 신뢰 가능한 축 / 데이터 부족 축 / 상충 신호 + overall_confidence.
     데이터 얇으면 confidence↓·단정 회피.

산식:
  weight_i  = axis_confidence_i × reliability_i           (가용 축만)
  holistic  = Σ_i (risk_i × weight_i) / Σ_i weight_i      (가중평균, 0~100)
  overall_confidence = mean(axis_confidence_i) × coverage  (가용 축 / 전체 축 비율)

자동주문 0. Anthropic API 미사용.
"""
from __future__ import annotations

from . import track_record as tr
from .axes import AXES, AXIS_LABELS
from .axes.base import clamp

# 상충(conflict) 판단: 가용 축 중 위험이 뚜렷이 높은 축과 낮은 축이 공존하는지
_CONFLICT_HIGH = 50.0    # 한 축은 이 이상
_CONFLICT_LOW = 20.0     # 다른 축은 이 이하


def _risk_band(score: float) -> str:
    if score >= 60:
        return "severe"
    if score >= 35:
        return "high"
    if score >= 15:
        return "elevated"
    return "low"


def composite(context: dict, *, use_track_record: bool = True) -> dict:
    """6축 메타인지 종합. context 는 각 축이 읽을 데이터를 담는다(순수 — 호출측이 채움).

    반환:
      {
        "holistic_risk": 0~100,            # 메타인지 가중 종합 위험
        "risk_band": low|elevated|high|severe,
        "overall_confidence": 0~1,         # 데이터 얇으면 낮음(단정 회피)
        "axes": {axis: AxisResult+weight+reliability, ...},
        "breakdown": [per-axis 요약(가중순)],
        "metacognition": {
            "reliable_axes": [...],        # 데이터 있고 신뢰도 높은 축
            "data_missing_axes": [...],    # 미연동(data_available=False)
            "conflicting_signals": bool, "conflict_detail": str,
            "coverage": 가용축/전체축,
            "note": 한글 메타 코멘트(정직),
        },
        "auto_order_created": False,
      }
    """
    axis_results: dict[str, dict] = {}
    for name, fn in AXES.items():
        try:
            axis_results[name] = fn(context)
        except Exception as e:  # noqa: BLE001 — 한 축 실패가 전체를 멈추지 않게(정직: 그 축 제외)
            from .axes.base import axis_result
            axis_results[name] = axis_result(name, data_available=False,
                                             detail=f"축 계산 오류: {e}")

    available = {n: r for n, r in axis_results.items() if r["data_available"]}
    missing = [n for n, r in axis_results.items() if not r["data_available"]]

    # track record(적중 신뢰도) — 가용 축만 조회
    rels = tr.reliabilities(list(available.keys())) if (use_track_record and available) else {}

    # 가중치 = data confidence × reliability
    weighted = []
    total_w = 0.0
    weighted_risk = 0.0
    for n, r in available.items():
        rel = (rels.get(n, {}) or {}).get("reliability", 0.5) if use_track_record else 1.0
        w = r["confidence"] * rel
        total_w += w
        weighted_risk += r["risk_0_100"] * w
        ar = dict(r)
        ar["reliability"] = round(rel, 3)
        ar["weight"] = round(w, 4)
        ar["reliability_source"] = (rels.get(n, {}) or {}).get("source", "n/a") if use_track_record else "disabled"
        axis_results[n] = ar
        weighted.append((n, ar))

    holistic = round(weighted_risk / total_w, 1) if total_w > 0 else 0.0

    # overall_confidence: 가용 축 평균 confidence × coverage(가용/전체)
    coverage = len(available) / len(AXES) if AXES else 0.0
    mean_conf = (sum(r["confidence"] for r in available.values()) / len(available)) if available else 0.0
    overall_conf = round(clamp(mean_conf * coverage), 3)

    # 메타인지: 신뢰 가능한 축(데이터 있고 reliability≥0.5 또는 risk 발화)
    reliable = sorted(
        [n for n, r in available.items() if r["confidence"] >= 0.4],
        key=lambda n: axis_results[n].get("weight", 0.0), reverse=True)

    # 상충 신호: 가용 축 위험 분포에 high 와 low 가 공존
    risks = [r["risk_0_100"] for r in available.values()]
    conflicting = bool(risks) and max(risks) >= _CONFLICT_HIGH and min(risks) <= _CONFLICT_LOW
    conflict_detail = ""
    if conflicting:
        hi = max(available.items(), key=lambda kv: kv[1]["risk_0_100"])
        lo = min(available.items(), key=lambda kv: kv[1]["risk_0_100"])
        conflict_detail = (f"{AXIS_LABELS.get(hi[0],hi[0])}축 위험 {hi[1]['risk_0_100']:.0f} vs "
                           f"{AXIS_LABELS.get(lo[0],lo[0])}축 {lo[1]['risk_0_100']:.0f} — 신호 상충")

    note_parts = []
    if not available:
        note_parts.append("가용 축 없음 — 데이터 미연동(정직: 분석 불가, 단정 회피).")
    else:
        note_parts.append(f"가용 축 {len(available)}/{len(AXES)} (coverage {coverage*100:.0f}%).")
        if missing:
            note_parts.append("미연동: " + ", ".join(AXIS_LABELS.get(m, m) for m in missing) + ".")
        if overall_conf < 0.3:
            note_parts.append("데이터 얇음 — confidence 낮음, 단정 회피.")
        if conflicting:
            note_parts.append("상충 신호 존재 — 종합 해석 주의.")

    breakdown = [{
        "axis": n, "label": AXIS_LABELS.get(n, n),
        "risk_0_100": axis_results[n]["risk_0_100"],
        "confidence": axis_results[n]["confidence"],
        "reliability": axis_results[n].get("reliability"),
        "weight": axis_results[n].get("weight"),
        "fired": [s["name"] for s in axis_results[n]["signals"] if s["fired"]],
        "detail": axis_results[n]["detail"],
    } for n, _ in sorted(weighted, key=lambda kv: kv[1].get("weight", 0.0), reverse=True)]

    return {
        "holistic_risk": holistic,
        "risk_band": _risk_band(holistic),
        "overall_confidence": overall_conf,
        "axes": axis_results,
        "breakdown": breakdown,
        "metacognition": {
            "reliable_axes": reliable,
            "data_missing_axes": missing,
            "conflicting_signals": conflicting,
            "conflict_detail": conflict_detail,
            "coverage": round(coverage, 3),
            "note": " ".join(note_parts),
        },
        "auto_order_created": False,
    }
