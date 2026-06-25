"""커넥터 공통 인터페이스/결과 스키마(Agent 2 개선 1/3) 테스트.

검증:
  - ConnectorResult 8 표준필드 + dict/json/attr 호환
  - data_available=false → confidence/count 0 강제(가짜 데이터 금지, 개선 3 표준)
  - stale 기본 판정(freshness.stale 우선 / data 없으면 보수적 stale)
  - from_legacy_status 매핑, connector_status 집계
"""
from __future__ import annotations

import json

from main_mission.portfolio_os.connectors import (
    CONNECTOR_FIELDS, Connector, ConnectorResult, connector_result,
    connector_status, from_legacy_status)


def test_standard_fields_and_compat():
    r = connector_result("fred", source="FRED", data_available=True,
                         freshness={"age_days": 3, "stale": False}, confidence=0.8, count=14)
    for k in CONNECTOR_FIELDS:
        assert k in r, k
    assert isinstance(r, dict)
    assert r["count"] == 14 and r.confidence == 0.8           # dict + attr
    assert json.loads(json.dumps(r, ensure_ascii=False))["source"] == "FRED"


def test_data_unavailable_forces_zero():
    r = connector_result("dart", source="DART", data_available=False,
                         confidence=0.9, count=99)
    assert r["data_available"] is False
    assert r["confidence"] == 0.0 and r["count"] == 0          # 가짜 데이터 금지
    assert r["stale"] is True                                  # 미연동 → 보수적 stale


def test_stale_from_freshness():
    fresh = connector_result("ecos", data_available=True, freshness={"stale": False})
    stale = connector_result("ecos", data_available=True, freshness={"stale": True})
    assert fresh["stale"] is False and stale["stale"] is True


def test_from_legacy_status_mapping():
    assert from_legacy_status("manual_input", "available")["data_available"] is True
    assert from_legacy_status("dart_filings", "not_connected")["data_available"] is False


def test_connector_status_aggregates():
    st = connector_status()
    assert "connectors" in st and st["total"] >= 1
    assert all(isinstance(v, ConnectorResult) for v in st["connectors"].values())
    assert st["available_count"] == sum(
        1 for v in st["connectors"].values() if v["data_available"])
    # note(설명 텍스트)는 커넥터로 집계되지 않음
    assert "note" not in st["connectors"]


def test_protocol_is_runtime_checkable():
    class Dummy:
        name = "x"
        def fetch(self, **k): ...
        def normalize(self, raw): ...
        def validate(self, n): return True
        def store(self, n, **k): ...
        def mark_stale(self, **k): ...
        def status(self): return connector_result("x")
    assert isinstance(Dummy(), Connector)


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} CONNECTOR TESTS PASSED")
