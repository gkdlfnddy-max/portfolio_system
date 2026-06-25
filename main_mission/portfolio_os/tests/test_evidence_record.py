"""통합 EvidenceRecord(Agent 2 개선 2) 테스트.

검증:
  - 14 표준필드 + dict/json/attr 호환
  - evidence_items(add_evidence) → evidence_for_account → EvidenceRecord 매핑(end-to-end)
  - 섹터형 source_type=sector → related_sector 노출
  - confidence clamp / freshness.stale 전달
"""
from __future__ import annotations

import json
import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_evidence_record.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import evidence_summary as es
from main_mission.portfolio_os import evidence_record as er


def setup():
    store_db.init()


def _seed_universe(ticker="005930", theme="반도체"):
    """계좌 1 유니버스에 종목 등록 — evidence_for_account 가 관련 evidence 를 포함하도록."""
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO universe_instruments(account_index, ticker, name, market, "
            "asset_class, is_inverse, is_active) VALUES(?,?,?,?,?,?,?)",
            (1, ticker, "삼성전자", "KR", "stock", 0, 1))
        conn.commit()
    finally:
        conn.close()


def test_fields_and_compat():
    rec = er.EvidenceRecord(source="DART", source_date="2026-06-01", confidence=0.7,
                            related_stock="005930", summary="영업이익 증가")
    for k in er.EVIDENCE_FIELDS:
        assert k in rec, k
    assert isinstance(rec, dict)
    assert rec["related_stock"] == "005930" and rec.confidence == 0.7
    assert json.loads(json.dumps(rec, ensure_ascii=False))["source"] == "DART"


def test_confidence_clamped():
    assert er.EvidenceRecord(confidence=9)["confidence"] == 1.0
    assert er.EvidenceRecord(confidence=-3)["confidence"] == 0.0


def test_end_to_end_from_evidence_items():
    setup()
    _seed_universe()
    es.add_evidence("news", source="한경", source_date="2026-06-20",
                    summary="반도체 업황 개선", confidence=0.6,
                    related_ticker="005930", related_theme="반도체",
                    positive_factors=["수요 증가"], uncertainties=["환율 변동"])
    recs = er.records_for_account(1)
    assert recs, "evidence_for_account 항목이 EvidenceRecord 로 매핑돼야 함"
    r = next(x for x in recs if x["related_stock"] == "005930")
    assert r["source"] == "한경" and r["related_theme"] == "반도체"
    assert r["captured_at"] is not None          # created_at 매핑
    assert r["uncertainty"] == "환율 변동"
    assert isinstance(r["freshness"], dict) and "stale" in r["freshness"]
    assert 0.0 <= r["confidence"] <= 1.0


def test_sector_source_maps_to_related_sector():
    setup()
    _seed_universe()
    es.add_evidence("sector", source="리포트", source_date="2026-06-18",
                    summary="반도체 섹터 비중확대", confidence=0.5,
                    related_ticker="005930", related_theme="반도체")
    recs = er.records_for_account(1)
    sec = [r for r in recs if r["related_sector"] == "반도체"]
    assert sec, [dict(r) for r in recs]


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} EVIDENCE-RECORD TESTS PASSED")
