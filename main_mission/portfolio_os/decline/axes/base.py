"""축 scorer 공통 인터페이스 + 헬퍼 (AxisResult SSOT).

각 축 모듈은 `score(context: dict) -> AxisResult` 를 노출한다.
모든 축은 **하나의 공통 스키마**(AxisResult)로 결과를 반환한다 → 새 축이 추가돼도
소비측(composite/daily_review/web/analysis_log)은 동일 구조만 읽으면 된다.

AxisResult 표준 필드(SSOT):
  axis              str        축 식별자(= axis_name).  technical|distribution|macro|event|sentiment|policy
  risk_0_100        float      이 축이 본 하락 위험 0~100.  data 없으면 0.0
  confidence        float      0~1 — 이 축 점수의 자기확신(데이터 양/질).  data 없으면 0.0
  data_available    bool       **정직** — 실데이터로 계산했는가? False 면 가짜 점수 아님
  signals           [dict]     발화 신호 [sig(), ...]
  missing_data      [str]      부족/미연동 데이터 항목(정직한 공백 표기)
  portfolio_impact  str        포트폴리오 영향 한 줄(읽기전용·단정 금지)
  suggested_actions [str]      제안 행동 후보(자동적용 금지 — 사용자 승인 대상)
  source_refs       [str|id]   근거 출처 ref(evidence/데이터 출처)
  detail            str        사람이 읽는 한 줄 요약(한글)
  last_updated_at   str|None   데이터 기준 시각 ISO(없으면 None)

원칙:
  - 데이터 없으면 risk_0_100=0.0, data_available=False, confidence=0.0 → 가짜 점수 금지.
  - confidence 는 "이 축 자체"의 데이터 충분성. composite 의 가중치는 여기에 track record 를 곱한다.
  - 부수효과 없음(순수). context 만 읽는다 (DB 접근은 호출측이 미리 채워줌).
  - **자동 주문/자동 적용 0** — suggested_actions 는 후보일 뿐 사용자 승인 전 미반영.
"""
from __future__ import annotations

from typing import Any


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# 표준 축 결과 필드(SSOT) — 직렬화/소비 순서의 단일 진실.
AXIS_FIELDS: tuple[str, ...] = (
    "axis", "risk_0_100", "confidence", "data_available", "signals",
    "missing_data", "portfolio_impact", "suggested_actions", "source_refs",
    "detail", "last_updated_at",
)


class AxisResult(dict):
    """6축 공통 결과 스키마(SSOT) — 타입 있는 dict 서브클래스.

    **dict 서브클래스로 구현한 이유(의도적·하위호환 보존):**
      - composite() 는 결과를 `dict(r)` 로 복사하고,
        decline_scan / daily_review / analysis_log / decline.__main__ / web 은
        축·종합 결과를 그대로 `json.dumps` 한다.
      - 순수 @dataclass 나 비-dict Mapping 으로 만들면 위 두 경로(`dict(r)` 와 `json.dumps`)가
        모두 깨진다. dict 서브클래스는 `r["k"]` · `"k" in r` · `r.get(...)` · `dict(r)` · `json.dumps`
        를 100% 보존하면서도 **단일 타입 스키마 + 표준 필드 + data_available 정직 강제**를 제공한다.
    속성 접근(`r.risk_0_100`)도 지원하되 기존 `r["risk_0_100"]` 소비는 그대로 유지된다.
    """

    __slots__ = ()

    def __init__(self, axis: str, *, risk_0_100: float = 0.0,
                 confidence: float = 0.0, data_available: bool = False,
                 signals: list[dict] | None = None,
                 missing_data: list | None = None,
                 portfolio_impact: str = "",
                 suggested_actions: list | None = None,
                 source_refs: list | None = None,
                 detail: str = "", last_updated_at: str | None = None) -> None:
        # 정직: 데이터 없으면 점수/확신을 0 으로 강제(가짜 점수 금지).
        if not data_available:
            risk_0_100 = 0.0
            confidence = 0.0
        super().__init__(
            axis=axis,
            risk_0_100=round(clamp(risk_0_100, 0.0, 100.0), 1),
            confidence=round(clamp(confidence), 3),
            data_available=bool(data_available),
            signals=signals or [],
            missing_data=missing_data or [],
            portfolio_impact=portfolio_impact,
            suggested_actions=suggested_actions or [],
            source_refs=source_refs or [],
            detail=detail,
            last_updated_at=last_updated_at,
        )

    # 디렉티브 명세 별칭(axis_name) — 기존 축 키 'axis' 와 동의어(코드 접근용).
    @property
    def axis_name(self) -> str:
        return self["axis"]

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:  # noqa: TRY003
            raise AttributeError(name) from exc


def axis_result(axis: str, *, risk_0_100: float = 0.0, signals: list[dict] | None = None,
                data_available: bool = False, confidence: float = 0.0,
                detail: str = "", missing_data: list | None = None,
                portfolio_impact: str = "", suggested_actions: list | None = None,
                source_refs: list | None = None,
                last_updated_at: str | None = None) -> AxisResult:
    """표준 AxisResult 생성. 데이터 없으면(data_available=False) 점수/확신을 0 으로 강제.

    기존 호출부(axis/risk_0_100/signals/data_available/confidence/detail)는 100% 그대로 동작하고,
    새 표준 필드(missing_data/portfolio_impact/suggested_actions/source_refs/last_updated_at)는
    선택적으로 채울 수 있다(미지정 시 안전한 기본값).
    """
    return AxisResult(
        axis,
        risk_0_100=risk_0_100, confidence=confidence, data_available=data_available,
        signals=signals, missing_data=missing_data, portfolio_impact=portfolio_impact,
        suggested_actions=suggested_actions, source_refs=source_refs,
        detail=detail, last_updated_at=last_updated_at,
    )


def sig(name: str, fired: bool, value: Any, severity: float, detail: str) -> dict:
    """축 내부 신호 dict — decline_signals 와 동일 형태."""
    return {"name": name, "fired": bool(fired), "value": value,
            "severity": round(clamp(severity), 3), "detail": detail}
