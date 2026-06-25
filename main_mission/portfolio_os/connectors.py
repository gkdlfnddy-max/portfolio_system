"""데이터 커넥터 공통 인터페이스 + 결과 스키마(SSOT) — Agent 2 개선 1/3.

KIS price · KIS investor flow · ECOS · FRED · DART · ETF constituents · event calendar ·
policy/news 등 모든 커넥터가 **같은 패턴**을 따르고 **같은 결과 구조**(ConnectorResult)로
상태/가용성을 보고하게 한다. 새 데이터 축이 추가돼도 소비측은 동일 구조만 읽으면 된다.

공통 인터페이스(Connector Protocol) — 커넥터가 노출하는 단계:
  fetch       외부 소스에서 원자료 가져오기(네트워크/파일)
  normalize   원자료 → 내부 표준 형태
  validate    정합성 검증(스키마/범위) — 실패 시 저장 안 함
  store       멱등 upsert(DB)
  mark_stale  staleness 표시/판정
  status      ConnectorResult 반환(source/freshness/confidence/data_available)

ConnectorResult 표준 필드(SSOT):
  name            커넥터 이름
  source          데이터 소스 라벨(예: FRED, ECOS, DART, KIS, manual)
  data_available  **정직** — 실데이터가 연동돼 있는가? False 면 가짜 점수/카운트 금지
  freshness       {age_days, stale, ...} 또는 None(신선도 정보 없음)
  confidence      0~1 — 데이터 양/질 자기확신. data 없으면 0.0
  count           가용 레코드 수. data 없으면 0
  stale           낡음 여부(freshness.stale 우선, 없으면 data_available 로 보수 판정)
  detail          사람이 읽는 한 줄(한글)

원칙: data_available=False 면 confidence=0.0·count=0 강제(가짜 데이터 금지, 개선 3 표준).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

CONNECTOR_FIELDS: tuple[str, ...] = (
    "name", "source", "data_available", "freshness", "confidence",
    "count", "stale", "detail",
)


def _clamp01(x: Any) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


class ConnectorResult(dict):
    """커넥터 상태 공통 스키마(SSOT) — 타입 있는 dict 서브클래스.

    AxisResult/CandidateEvaluation 과 동일 이유로 dict 서브클래스:
    소비측의 `r["k"]`·`dict(r)`·`json.dumps` 호환을 보존하면서 표준 필드 + data_available 정직 강제.
    """

    __slots__ = ()

    def __init__(self, name: str, *, source: str = "", data_available: bool = False,
                 freshness: dict | None = None, confidence: float = 0.0,
                 count: int = 0, stale: bool | None = None, detail: str = "") -> None:
        # 데이터 가용성 표준(SSOT) — data 없으면 confidence/count 0 강제(가짜 데이터 금지).
        from .data_availability import honest_confidence, honest_count
        data_available = bool(data_available)
        conf = honest_confidence(data_available, confidence)
        cnt = honest_count(data_available, count)
        if stale is None:
            stale = (freshness.get("stale", False) if isinstance(freshness, dict)
                     else (not data_available))
        super().__init__(
            name=name, source=source, data_available=data_available,
            freshness=freshness, confidence=round(conf, 3),
            count=cnt, stale=bool(stale), detail=detail,
        )

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as exc:  # noqa: TRY003
            raise AttributeError(key) from exc


def connector_result(name: str, **kw: Any) -> ConnectorResult:
    """표준 ConnectorResult 생성(data_available=False면 confidence/count 0 강제)."""
    return ConnectorResult(name, **kw)


@runtime_checkable
class Connector(Protocol):
    """커넥터 공통 인터페이스(문서/타입 계약). 기존 커넥터는 점진 채택 — 강제 rewrite 아님."""

    name: str

    def fetch(self, **kw: Any) -> Any: ...
    def normalize(self, raw: Any) -> Any: ...
    def validate(self, normalized: Any) -> bool: ...
    def store(self, normalized: Any, **kw: Any) -> Any: ...
    def mark_stale(self, **kw: Any) -> Any: ...
    def status(self) -> ConnectorResult: ...


# 레거시 data_source_status() 의 상태 문자열 → 표준 data_available.
_LEGACY_AVAILABLE = {"available", "connected"}


def from_legacy_status(name: str, raw_status: str, *, source: str = "",
                       detail: str = "") -> ConnectorResult:
    """evidence_summary.data_source_status() 의 'available'/'not_connected' → ConnectorResult.

    additive — 기존 status 출력은 두고 표준 구조로도 제공(개선 3: data_available 표준화).
    """
    available = str(raw_status).strip().lower() in _LEGACY_AVAILABLE
    return connector_result(
        name, source=source or name, data_available=available,
        detail=detail or (f"{name}: {raw_status}"))


def connector_status() -> dict:
    """모든 커넥터 상태를 ConnectorResult 로 통일 집계(additive view).

    현재는 evidence_summary.data_source_status()(정직한 미연동 표기)를 표준 구조로 매핑한다.
    실 커넥터가 status() 를 노출하면 여기서 그대로 수집하도록 확장한다.
    """
    from .evidence_summary import data_source_status
    raw = data_source_status()
    out: dict[str, ConnectorResult] = {}
    for key, val in raw.items():
        if key == "note" or not isinstance(val, str):
            continue
        out[key] = from_legacy_status(key, val)
    return {
        "connectors": out,
        "available_count": sum(1 for r in out.values() if r["data_available"]),
        "total": len(out),
        "note": raw.get("note", ""),
    }
