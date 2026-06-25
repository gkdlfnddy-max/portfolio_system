"""후보 평가 공통 스키마(SSOT) — CandidateEvaluation.

종목 / ETF / 국채 / 인버스 등 **모든 후보 평가**를 하나의 표준 포맷으로 통일한다.
새 후보 종류가 추가돼도 소비측(후보 비교 UI · 리밸런싱 · 주문 후보)은 동일 구조만 읽으면 된다.

`decline/axes/base.py` 의 AxisResult 와 **같은 이유로 dict 서브클래스**로 구현한다:
  - 소비측이 `r["k"]` · `dict(r)` · `json.dumps` 를 쓰므로 하위호환 100% 보존.
  - 동시에 단일 타입 스키마 + **안전 불변식**을 타입 차원에서 강제.

안전 불변식(하드 — 이 스키마로는 우회 불가):
  - `approval_required` 는 **항상 True** (사용자 승인 기본값 — 무승인 자동매매 금지).
  - `auto_order_created` / `auto_applied` 는 **항상 False**
    (후보 평가가 곧 주문/적용이 아님 — 자동 주문·자동 적용 금지 원칙을 코드로 봉인).
  - 데이터 없으면 **가짜 점수 금지** — suggested_weight/max_weight 는 None(미정), confidence 0.0.

표준 필드(SSOT, 17종):
  candidate_type     종목|etf|treasury|inverse|...      (후보 종류)
  candidate_id       티커/식별자
  display_name       사람이 보는 이름(없으면 id)
  bucket             소속 bucket(예: treasury, semiconductor_inverse, ...)
  fit_to_account     계좌 목적/policy 적합성(없으면 None)
  fit_to_allocation  확정안(selected_allocation) 적합성(없으면 None)
  data_quality       {available: bool, level: str, detail: str}  데이터 가용성(정직)
  confidence         0~1 (데이터 얇으면 낮음 → 단정 회피)
  risk_summary       위험 요약(없으면 None)
  evidence_summary   근거 요약(없으면 None)
  suggested_weight   제안 비중(미정이면 None — 가짜 숫자 금지)
  max_weight         상한 비중(미정이면 None)
  reason_to_include  편입 사유
  reason_to_exclude  제외 사유
  approval_required  항상 True
  auto_order_created 항상 False
  auto_applied       항상 False
"""
from __future__ import annotations

from typing import Any


def _clamp01(x: Any) -> float | None:
    """0~1 clamp. 숫자가 아니면(예: 'medium' 라벨) None — 가짜 점수 만들지 않음."""
    if x is None:
        return None
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return None


# confidence → 추천 강도 임계(SSOT, 단일 진실).
#   < low  : 단정 금지 — 관망/주의/후보만.
#   < mid  : 약한 제안(사람 승인).
#   ≥ mid  : 비교적 강한 제안(단, 항상 사람 승인).
# 6축(decline_scan.confidence_judgment)·후보평가·국채·Daily Review·UI 가 이 임계를 공유한다.
CONFIDENCE_BANDS: dict[str, float] = {"low": 0.3, "mid": 0.6}


def recommendation_strength(confidence: Any) -> dict:
    """confidence(0~1) → **공통 추천 강도**. 항상 사용자 승인 필요·자동 적용 없음.

    숫자가 아니거나 None 이면 가장 보수적(watch)으로 취급(가짜 강한 추천 금지).
    """
    c = _clamp01(confidence)
    if c is None or c < CONFIDENCE_BANDS["low"]:
        return {"level": "watch", "label": "관망/주의", "assert_ok": False,
                "approval_required": True,
                "note": "신뢰도 낮음 — 단정 금지, 관망/주의·후보로만(데이터 추가 필요)."}
    if c < CONFIDENCE_BANDS["mid"]:
        return {"level": "weak", "label": "약한 제안", "assert_ok": False,
                "approval_required": True,
                "note": "신뢰도 중간 — 약한 제안(항상 사람 승인)."}
    return {"level": "moderate", "label": "비교적 강한 제안", "assert_ok": True,
            "approval_required": True,
            "note": "신뢰도 높음 — 비교적 강한 제안 가능(단, 항상 사람 승인)."}


# 표준 후보 평가 필드(SSOT) — 직렬화/소비 순서의 단일 진실.
# recommendation_strength 는 confidence 에서 파생(공통 규칙) — factory 가 자동 부착.
CANDIDATE_FIELDS: tuple[str, ...] = (
    "candidate_type", "candidate_id", "display_name", "bucket",
    "fit_to_account", "fit_to_allocation", "data_quality", "confidence",
    "recommendation_strength",
    "risk_summary", "evidence_summary", "suggested_weight", "max_weight",
    "reason_to_include", "reason_to_exclude",
    "approval_required", "auto_order_created", "auto_applied",
)


class CandidateEvaluation(dict):
    """후보 평가 공통 스키마(SSOT) — 타입 있는 dict 서브클래스 + 안전 불변식.

    approval_required=True / auto_order_created=False / auto_applied=False 는
    생성자에서 **하드코딩**되어 호출자가 바꿀 수 없다(자동 주문·자동 적용 봉인).
    속성 접근(`c.confidence`)도 지원하되 기존 `c["confidence"]` 소비는 그대로 유지된다.
    """

    __slots__ = ()

    def __init__(self, candidate_type: str, candidate_id: str, *,
                 display_name: str = "", bucket: str | None = None,
                 fit_to_account: Any = None, fit_to_allocation: Any = None,
                 data_quality: dict | None = None, confidence: float = 0.0,
                 risk_summary: Any = None, evidence_summary: Any = None,
                 suggested_weight: float | None = None,
                 max_weight: float | None = None,
                 reason_to_include: str = "", reason_to_exclude: str = "") -> None:
        conf_val = round(_clamp01(confidence) or 0.0, 3)
        super().__init__(
            candidate_type=candidate_type,
            candidate_id=candidate_id,
            display_name=display_name or candidate_id,
            bucket=bucket,
            fit_to_account=fit_to_account,
            fit_to_allocation=fit_to_allocation,
            data_quality=(data_quality if data_quality is not None
                          else {"available": False, "level": "unknown"}),
            confidence=conf_val,
            # 공통 규칙(confidence→강도) 자동 부착 — 분석기마다 제각각 강하게 말하지 않게.
            recommendation_strength=recommendation_strength(conf_val),
            risk_summary=risk_summary,
            evidence_summary=evidence_summary,
            suggested_weight=(None if suggested_weight is None
                              else round(float(suggested_weight), 4)),
            max_weight=(None if max_weight is None else round(float(max_weight), 4)),
            reason_to_include=reason_to_include,
            reason_to_exclude=reason_to_exclude,
            # --- 안전 불변식(하드, 오버라이드 불가) ---
            approval_required=True,
            auto_order_created=False,
            auto_applied=False,
        )

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:  # noqa: TRY003
            raise AttributeError(name) from exc


def candidate_evaluation(candidate_type: str, candidate_id: str, **kw: Any) -> CandidateEvaluation:
    """표준 CandidateEvaluation 생성(안전 불변식 자동 적용)."""
    return CandidateEvaluation(candidate_type, candidate_id, **kw)
