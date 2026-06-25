"""Track C — 재무/DART 커넥터(financials_connect) 테스트.

검증(불변 안전):
  - 키 없을 때 **안전 실패**(FinancialsConfigError) — 가짜 재무 0.
  - corp_code 매핑 없으면 정직 실패(corp_code 추측 금지). 8자리 직접 전달은 통과.
  - DART status='013'(무자료) → 빈 리스트(가짜 0). status≠'000' → 에러.
  - parse_fundamentals: 매출/영업이익/순이익/부채비율/영업이익률/현금흐름/재고만 추출,
    ROE/PER/PBR/EV-EBITDA/capex 는 None(가짜 0 금지).
  - upsert_fundamentals: 멱등(UNIQUE ticker,period) · 전부 None 이면 적재 거부.
  - **fundamentals 적재 → security_selection.quality_filter 자동 활성**(핵심 회귀).
  - HTTP fetcher 는 monkeypatch(네트워크 0) · Anthropic 미사용.
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_financials.sqlite3")
# WAL/SHM 사이드카까지 정리 — 미삭제 시 새 연결이 stale WAL 로 readonly/IO 오류(테스트 격리).
for _sfx in ("", "-wal", "-shm", "-journal"):
    if os.path.exists(_TMP + _sfx):
        os.remove(_TMP + _sfx)
os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_PATH"] = _TMP

import pytest

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import financials_connect as fc
from main_mission.portfolio_os import security_selection as ss


@pytest.fixture(autouse=True)
def _financials_db(monkeypatch):
    """각 테스트 전 이 모듈의 SQLITE_PATH 로 고정 + fundamentals 비움(격리).

    pytest 8+ 는 nose-style setup()/setup_function() 를 호출하지 않으므로 fixture 로 전환.
    conftest 의 autouse fixture 가 먼저 경로를 재핀하지만, 여기서 다시 못 박아(_TMP) 순서
    의존성을 제거하고, store_db 부트스트랩 캐시를 초기화해 스키마를 확실히 보장한다.
    """
    os.environ["SQLITE_PATH"] = _TMP
    store_db._bootstrapped_path = None
    store_db.init()
    conn = store_db.connect()
    try:
        conn.execute("DELETE FROM fundamentals")
        conn.commit()
    finally:
        conn.close()
    yield


# ---- 1) 안전 실패 (키/매핑 없음) ----
def test_dart_key_missing_raises(monkeypatch):
    monkeypatch.delenv("DART_API_KEY", raising=False)
    monkeypatch.setattr(fc, "_load_env", lambda: None)
    with pytest.raises(fc.FinancialsConfigError):
        fc.dart_api_key()


def test_resolve_corp_code_no_map_raises(monkeypatch):
    monkeypatch.setattr(fc, "load_corp_map", lambda: {})
    with pytest.raises(fc.FinancialsConfigError):
        fc.resolve_corp_code("005930")


def test_resolve_corp_code_direct_8digit():
    # 8자리 corp_code 직접 전달은 매핑 없이도 통과(추측 아님 — 사용자 제공).
    assert fc.resolve_corp_code("00126380") == "00126380"


def test_resolve_corp_code_from_map():
    assert fc.resolve_corp_code("005930", corp_map={"005930": "00126380"}) == "00126380"


def test_status_not_connected_without_key(monkeypatch):
    monkeypatch.delenv("DART_API_KEY", raising=False)
    monkeypatch.setattr(fc, "_load_env", lambda: None)
    monkeypatch.setattr(fc, "load_corp_map", lambda: {})
    st = fc.status()
    assert st["data_connected"] is False
    assert st["dart_api_key"] == "not_set"
    assert st["quality_filter_active"] is False


# ---- 2) fetch + 파싱 (monkeypatch, 네트워크 0) ----
def _fake_rows():
    return [
        {"sj_div": "IS", "account_nm": "매출액", "thstrm_amount": "1,000,000"},
        {"sj_div": "IS", "account_nm": "영업이익", "thstrm_amount": "150,000"},
        {"sj_div": "IS", "account_nm": "당기순이익", "thstrm_amount": "120,000"},
        {"sj_div": "BS", "account_nm": "부채총계", "thstrm_amount": "400,000"},
        {"sj_div": "BS", "account_nm": "자본총계", "thstrm_amount": "800,000"},
        {"sj_div": "BS", "account_nm": "재고자산", "thstrm_amount": "90,000"},
        {"sj_div": "CF", "account_nm": "영업활동현금흐름", "thstrm_amount": "200,000"},
    ]


def test_fetch_finstate_success(monkeypatch):
    monkeypatch.setattr(fc, "_http_get_json",
                        lambda url, timeout=20.0: {"status": "000", "list": _fake_rows()})
    rows = fc.fetch_finstate("00126380", 2024, "11011", api_key="x")
    assert len(rows) == 7


def test_fetch_finstate_no_data_013(monkeypatch):
    monkeypatch.setattr(fc, "_http_get_json",
                        lambda url, timeout=20.0: {"status": "013", "message": "no data"})
    assert fc.fetch_finstate("00126380", 2024, "11011", api_key="x") == []  # 가짜 0


def test_fetch_finstate_error_status_raises(monkeypatch):
    monkeypatch.setattr(fc, "_http_get_json",
                        lambda url, timeout=20.0: {"status": "020", "message": "limit"})
    with pytest.raises(fc.FinancialsConfigError):
        fc.fetch_finstate("00126380", 2024, "11011", api_key="x")


def test_parse_fundamentals_extracts_and_nulls():
    m = fc.parse_fundamentals(_fake_rows())
    assert m["revenue"] == 1_000_000.0
    assert m["op_income"] == 150_000.0
    assert m["net_income"] == 120_000.0
    assert m["inventory"] == 90_000.0
    assert m["cash_flow_op"] == 200_000.0
    assert m["op_margin"] == 15.0           # 150k/1000k*100
    assert m["debt_ratio"] == 50.0          # 400k/800k*100
    # 재무제표만으로 산출 불가 → None(가짜 0 금지).
    for k in ("roe", "per", "pbr", "ev_ebitda", "capex"):
        assert m[k] is None


def test_parse_net_income_prefix_variant():
    rows = [{"sj_div": "IS", "account_nm": "당기순이익(손실)", "thstrm_amount": "-50,000"}]
    assert fc.parse_fundamentals(rows)["net_income"] == -50_000.0


# ---- 3) upsert 멱등 + 빈행 거부 ----
def test_upsert_idempotent():
    m = fc.parse_fundamentals(_fake_rows())
    assert fc.upsert_fundamentals("005930", "2024", m) is True
    assert fc.upsert_fundamentals("005930", "2024", m) is True  # 재실행 OK(멱등)
    conn = store_db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM fundamentals WHERE ticker='005930'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1


def test_upsert_rejects_all_none():
    empty = {k: None for k in ("revenue", "op_income", "net_income", "op_margin",
                               "debt_ratio", "cash_flow_op", "roe", "per", "pbr",
                               "ev_ebitda", "inventory", "capex")}
    assert fc.upsert_fundamentals("999999", "2024", empty) is False


# ---- 4) load_financials end-to-end (monkeypatch) ----
def test_load_financials_writes(monkeypatch):
    monkeypatch.setattr(fc, "_http_get_json",
                        lambda url, timeout=20.0: {"status": "000", "list": _fake_rows()})
    out = fc.load_financials("005930", 2024, "11011",
                             corp_map={"005930": "00126380"}, api_key="x")
    assert out["written"] is True
    assert out["period"] == "2024"


# ---- 5) 핵심 회귀: fundamentals 적재 → quality_filter 자동 활성 ----
def test_quality_filter_inactive_without_fundamentals():
    # 적재 전: 우량주 필터 적용 불가(passed=None, 가짜 통과 0).
    qf = ss.quality_filter("123456")   # _TICKER_META 에 없는 임의 개별주 ticker
    assert qf["applicable"] is True
    assert qf["passed"] is None


def test_quality_filter_active_after_fundamentals_loaded():
    # 우량 재무 적재 → 통과(True). 가짜 점수 아니라 실제 수치 판정.
    good = fc.parse_fundamentals(_fake_rows())   # op_margin15·debt50·흑자·현금+
    fc.upsert_fundamentals("123456", "2024", good)
    qf = ss.quality_filter("123456")
    assert qf["applicable"] is True
    assert qf["passed"] is True, qf
    # 부실 재무 적재 → 미달(False). (적자 + 고부채)
    bad_rows = [
        {"sj_div": "IS", "account_nm": "매출액", "thstrm_amount": "100,000"},
        {"sj_div": "IS", "account_nm": "영업이익", "thstrm_amount": "-20,000"},
        {"sj_div": "IS", "account_nm": "당기순이익", "thstrm_amount": "-30,000"},
        {"sj_div": "BS", "account_nm": "부채총계", "thstrm_amount": "900,000"},
        {"sj_div": "BS", "account_nm": "자본총계", "thstrm_amount": "100,000"},  # 부채율 900%
    ]
    fc.upsert_fundamentals("654321", "2024", fc.parse_fundamentals(bad_rows))
    qf2 = ss.quality_filter("654321")
    assert qf2["passed"] is False, qf2


def test_structured_financials_reads_latest_period():
    fc.upsert_fundamentals("005930", "2023", {"net_income": 1.0})
    fc.upsert_fundamentals("005930", "2024", {"net_income": 2.0})
    conn = store_db.connect()
    try:
        fin = ss._structured_financials("005930", conn=conn)
    finally:
        conn.close()
    assert fin["net_income"] == 2.0   # 최신 period(2024)


# ---- 6) corp_code 매핑 빌드 (CORPCODE.xml zip 파싱, 네트워크 0) ----
def _fake_corpcode_zip() -> bytes:
    """CORPCODE.xml(zip) 합성 — 상장사 2건(stock_code 있음) + 비상장 1건(stock_code 빈값)."""
    import io
    import zipfile
    xml = (
        "<?xml version='1.0' encoding='utf-8'?><result>"
        "<list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
        "<stock_code>005930</stock_code></list>"
        "<list><corp_code>00164779</corp_code><corp_name>SK하이닉스</corp_name>"
        "<stock_code>000660</stock_code></list>"
        "<list><corp_code>99999999</corp_code><corp_name>비상장사</corp_name>"
        "<stock_code> </stock_code></list>"
        "</result>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", xml.encode("utf-8"))
    return buf.getvalue()


def test_build_corp_map_parses_listed_only(monkeypatch, tmp_path):
    # 네트워크 대신 합성 zip 바이트를 반환하도록 urlopen 을 monkeypatch.
    import contextlib

    class _Resp:
        def __init__(self, data): self._d = data
        def read(self): return self._d

    @contextlib.contextmanager
    def _fake_urlopen(req, timeout=30.0):
        yield _Resp(_fake_corpcode_zip())

    monkeypatch.setattr(fc.urllib.request, "urlopen", _fake_urlopen)
    out_path = tmp_path / "corp_code_map.json"
    monkeypatch.setattr(fc, "_corp_map_path", lambda: out_path)

    out = fc.build_corp_map(api_key="x")
    assert out["ok"] is True
    assert out["mapped"] == 2          # 상장사 2건만(비상장/빈 stock_code 제외).
    import json as _json
    m = _json.loads(out_path.read_text(encoding="utf-8"))
    assert m["005930"] == "00126380"
    assert m["000660"] == "00164779"
    assert "99999999" not in m.values() or len(m) == 2   # 비상장은 매핑에 없음.
    # 생성된 맵으로 resolve_corp_code 가 동작(추측 아님 — 공식 매핑).
    assert fc.resolve_corp_code("005930", corp_map=m) == "00126380"


def test_build_corp_map_non_zip_response_raises(monkeypatch):
    # 키 불량 등으로 zip 이 아닌(JSON 오류) 응답이면 정직 실패(FinancialsConfigError).
    import contextlib

    class _Resp:
        def read(self): return b'{"status":"010","message":"key invalid"}'

    @contextlib.contextmanager
    def _fake_urlopen(req, timeout=30.0):
        yield _Resp()

    monkeypatch.setattr(fc.urllib.request, "urlopen", _fake_urlopen)
    with pytest.raises(fc.FinancialsConfigError):
        fc.build_corp_map(api_key="x")


def test_build_corp_map_requires_key(monkeypatch):
    monkeypatch.delenv("DART_API_KEY", raising=False)
    monkeypatch.setattr(fc, "_load_env", lambda: None)
    with pytest.raises(fc.FinancialsConfigError):
        fc.build_corp_map()   # 키 없음 → 안전 실패(가짜 0).


# ---- 7) 멀티기간 일괄 적재 (연간+분기, 무자료 기간 skip) ----
def test_load_financials_many_writes_multiple(monkeypatch):
    monkeypatch.setattr(fc, "_http_get_json",
                        lambda url, timeout=20.0: {"status": "000", "list": _fake_rows()})
    out = fc.load_financials_many(
        "005930", years=[2023, 2024], reprts=["11011", "11012"],
        corp_map={"005930": "00126380"}, api_key="x")
    assert out["ok"] is True
    assert out["periods_attempted"] == 4
    assert out["periods_written"] == 4
    conn = store_db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM fundamentals WHERE ticker='005930'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 4


def test_load_financials_many_skips_no_data(monkeypatch):
    # 한 기간은 무자료(013) → 건너뛰고(가짜 0) 나머지만 적재.
    calls = {"i": 0}

    def _fake(url, timeout=20.0):
        calls["i"] += 1
        if calls["i"] == 1:
            return {"status": "013", "message": "no data"}  # 첫 기간 무자료.
        return {"status": "000", "list": _fake_rows()}

    monkeypatch.setattr(fc, "_http_get_json", _fake)
    out = fc.load_financials_many(
        "005930", years=[2023, 2024], reprts=["11011"],
        corp_map={"005930": "00126380"}, api_key="x")
    assert out["periods_attempted"] == 2
    assert out["periods_written"] == 1   # 무자료 1건 skip.


def test_no_anthropic_import():
    import pathlib
    src = pathlib.Path(fc.__file__).read_text(encoding="utf-8").lower()
    assert "import anthropic" not in src
    assert "anthropic_api_key" not in src
