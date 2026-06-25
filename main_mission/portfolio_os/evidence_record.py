"""통합 Evidence 구조(SSOT) — EvidenceRecord (Agent 2 개선 2).

재무·뉴스·공시·ETF구성·거시·수급·이벤트(섹터 포함)를 **하나의 evidence 구조**로 통합한다.
물리 저장소(evidence_items 테이블)는 그대로 두고, 소비측이 읽는 표준 레코드를 단일화한다
(AxisResult/CandidateEvaluation/ConnectorResult 와 동일한 dict 서브클래스 패턴).

evidence_items(VALID_SOURCE_TYPES: financials/filing/news/sector/etf/macro/flow)가
이미 디렉티브 7종 소스를 포괄하므로 이 모듈이 그 뷰를 표준 레코드로 매핑한다.

EvidenceRecord 표준 필드(SSOT, 디렉티브 명세):
  source           출처(매체/기관/입력자)
  source_date      자료 자체 날짜
  captured_at      우리가 수집한 시각
  related_stock    관련 종목(티커)
  related_etf      관련 ETF
  related_sector   관련 섹터
  related_theme    관련 테마
  fact             사실(요약된 객관 진술)
  interpretation   해석(포트폴리오 영향 — 단정 아님)
  uncertainty      불확실성(정직 표기)
  summary          한 줄 요약
  confidence       0~1 (decay 반영 effective)
  freshness        {stale, eff_confidence, base_confidence}
  stale_at         staleness 기준 시각(없으면 None — freshness.stale 로 판단)
"""
from __future__ import annotations

from typing import Any

EVIDENCE_FIELDS: tuple[str, ...] = (
    "source", "source_date", "captured_at",
    "related_stock", "related_etf", "related_sector", "related_theme",
    "fact", "interpretation", "uncertainty", "summary",
    "confidence", "freshness", "stale_at",
)


def _clamp01(x: Any) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


class EvidenceRecord(dict):
    """통합 evidence 표준 레코드(SSOT) — dict 서브클래스(json/dict/attr 호환)."""

    __slots__ = ()

    def __init__(self, *, source: str | None = None, source_date: str | None = None,
                 captured_at: str | None = None, related_stock: str | None = None,
                 related_etf: str | None = None, related_sector: str | None = None,
                 related_theme: str | None = None, fact: str = "",
                 interpretation: str = "", uncertainty: str = "", summary: str = "",
                 confidence: float = 0.0, freshness: dict | None = None,
                 stale_at: str | None = None) -> None:
        super().__init__(
            source=source, source_date=source_date, captured_at=captured_at,
            related_stock=related_stock, related_etf=related_etf,
            related_sector=related_sector, related_theme=related_theme,
            fact=fact, interpretation=interpretation, uncertainty=uncertainty,
            summary=summary, confidence=round(_clamp01(confidence), 3),
            freshness=freshness or {}, stale_at=stale_at,
        )

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as exc:  # noqa: TRY003
            raise AttributeError(key) from exc


def _join(parts) -> str:
    if isinstance(parts, (list, tuple)):
        return "; ".join(str(p) for p in parts if p)
    return str(parts) if parts else ""


def from_item_view(view: dict) -> EvidenceRecord:
    """evidence_summary._row_to_view() 출력 → 통합 EvidenceRecord.

    additive — 기존 evidence_items/뷰는 그대로 두고 표준 레코드로 변환만 제공.
    섹터형(source_type='sector')은 related_theme 를 섹터로도 노출(별도 섹터 컬럼 부재 — 정직 매핑).
    """
    source_type = view.get("source_type")
    related_sector = view.get("related_theme") if source_type == "sector" else None
    return EvidenceRecord(
        source=view.get("source"),
        source_date=view.get("source_date"),
        captured_at=view.get("captured_at") or view.get("source_date"),
        related_stock=view.get("related_ticker"),
        related_etf=view.get("related_etf"),
        related_sector=related_sector,
        related_theme=view.get("related_theme"),
        fact=view.get("summary") or "",
        interpretation=view.get("portfolio_impact") or "",
        uncertainty=_join(view.get("uncertainties")),
        summary=view.get("summary") or "",
        confidence=view.get("eff_confidence", view.get("base_confidence", 0.0)),
        freshness={"stale": bool(view.get("stale")),
                   "eff_confidence": view.get("eff_confidence"),
                   "base_confidence": view.get("base_confidence")},
        stale_at=None,
    )


def records_for_account(account_index: int, *, limit: int = 50, conn=None) -> list[EvidenceRecord]:
    """계좌 관련 evidence 를 통합 EvidenceRecord 리스트로 반환(소비측 단일 구조)."""
    from . import evidence_summary
    out = evidence_summary.evidence_for_account(account_index, limit=limit, conn=conn)
    return [from_item_view(v) for v in (out.get("items") or [])]
