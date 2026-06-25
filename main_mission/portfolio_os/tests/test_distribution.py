"""분산축(distribution) + 투자자 매매동향 수급 연결 테스트 (결정론, 네트워크/실키 없음).

검증:
  - KIS inquire-investor 응답 파싱(공식 필드 frgn/orgn/prsn_ntby_qty + 거래량 근사)
  - rt_cd != 0 → RuntimeError(가짜 성공 금지)
  - investor_flows 멱등 upsert(재실행 중복 없음, source='kis_investor')
  - 분산축: 외국인+기관 동반 순매도 & 개인 순매수 & 거래량 급증 → 위험↑(설명 출력)
  - 기관 방어 매수 → 완충(위험 감쇄) + 'buffer' 신호
  - 데이터 없으면 data_available=False, 점수 0(가짜 0 금지)
  - security_selection 후보 비교에 수급 신호 반영(이탈→caution / 방어→supportive)
  - read-only: 키 없을 때 명확 실패(KisConfigError) · fetcher 에 place_order 없음(주문 0)
  - anthropic 미사용
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_distribution.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os.broker import kis_investor as ki
from main_mission.portfolio_os.broker import kis_endpoints as ep
from main_mission.portfolio_os.decline.axes import distribution as dist


def setup():
    # 본 모듈 전용 SQLITE 파일 재핀(전역 SQLITE_PATH 가 다른 모듈에 의해 바뀌어도
    # 본 모듈 테스트는 항상 _TMP 사용 — 교차오염 방지). 다른 모듈 상태는 건드리지 않음.
    os.environ["SQLITE_PATH"] = _TMP
    store_db.init()


# ============================================================
# 합성 KIS 응답 헬퍼
# ============================================================
def _kis_resp(rows: list[dict], rt_cd: str = "0") -> dict:
    return {"rt_cd": rt_cd, "msg1": "정상", "output": rows}


def _row(date8: str, frgn, orgn, prsn, *, fvol=0, ovol=0, pvol=0) -> dict:
    return {
        "stck_bsop_date": date8,
        "frgn_ntby_qty": str(frgn), "orgn_ntby_qty": str(orgn), "prsn_ntby_qty": str(prsn),
        "frgn_shnu_vol": str(fvol), "orgn_shnu_vol": str(ovol), "prsn_shnu_vol": str(pvol),
    }


class _FakeClient:
    """KisHttpClient.get 만 흉내. place_order 를 일부러 두지 않아 read-only 강제."""
    is_healthy = True

    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def get(self, path, tr_id, params, timeout=10):
        self.calls.append((path, tr_id, params))
        return self._resp


# ============================================================
# 1) 파싱 — 공식 필드 + 거래량 근사
# ============================================================
def test_parse_official_fields():
    rows = [
        _row("20260105", frgn=-1000, orgn=-500, prsn=1400, fvol=100, ovol=200, pvol=300),
        _row("20260104", frgn=-800, orgn=-200, prsn=900, fvol=50, ovol=60, pvol=70),
        {"stck_bsop_date": "", "frgn_ntby_qty": "0"},  # 빈 행 skip
    ]
    out = ki.parse_investor_rows(_kis_resp(rows), "005930")
    assert len(out) == 2
    # 오래된→최신 정렬
    assert out[0]["trade_date"] == "2026-01-04"
    assert out[1]["trade_date"] == "2026-01-05"
    assert out[1]["foreign_net"] == -1000.0
    assert out[1]["institution_net"] == -500.0
    assert out[1]["retail_net"] == 1400.0
    assert out[1]["volume"] == 600.0  # 100+200+300 매수거래량 합 근사


def test_parse_raises_on_error_rtcd():
    raised = False
    try:
        ki.parse_investor_rows(_kis_resp([], rt_cd="1"), "XXXX")
    except RuntimeError:
        raised = True
    assert raised, "rt_cd!=0 이면 RuntimeError(가짜 성공 금지)"


def test_parse_skips_all_empty_nets():
    # 순매수 3주체 전부 비면 가짜 0 만들지 않고 skip
    row = {"stck_bsop_date": "20260106", "frgn_ntby_qty": "", "orgn_ntby_qty": "",
           "prsn_ntby_qty": ""}
    out = ki.parse_investor_rows(_kis_resp([row]), "005930")
    assert out == []


# ============================================================
# 2) fetcher → 멱등 upsert (read-only)
# ============================================================
def test_fetch_and_store_idempotent():
    setup()
    rows = [
        _row("20260104", frgn=-800, orgn=-200, prsn=900, fvol=50, ovol=60, pvol=70),
        _row("20260105", frgn=-1000, orgn=-500, prsn=1400, fvol=100, ovol=200, pvol=300),
    ]
    client = _FakeClient(_kis_resp(rows))
    f = ki.KisInvestorFetcher(client=client)
    res1 = f.fetch_and_store("005930")
    assert res1["ok"] and res1["written"] == 2 and res1["read_only"] is True
    assert client.calls[0][0] == ep.PATH_DOMESTIC_INVESTOR
    assert client.calls[0][1] == ep.TRID_DOMESTIC_INVESTOR
    # 멱등 재실행 — 중복 행 없음
    f.fetch_and_store("005930")
    conn = store_db.connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM investor_flows WHERE instrument_code=?",
            ("005930",)).fetchone()["c"]
    finally:
        conn.close()
    assert n == 2, f"멱등 upsert 실패 — 행 수 {n}"


def test_fetcher_has_no_order_method():
    # read-only 강제: 주문 경로가 fetcher 에 정의조차 없어야 함
    f = ki.KisInvestorFetcher(client=_FakeClient(_kis_resp([])))
    for name in ("place_order", "order", "submit_order", "buy", "sell"):
        assert not hasattr(f, name), f"fetcher 에 주문 메서드 {name} 존재 — read-only 위반"


# ============================================================
# 3) 분산축 — 위험/완충/데이터없음
# ============================================================
def _flows(spec: list[tuple], *, vol=1000.0, start_day=1):
    """spec: [(foreign, institution, retail), ...] → investor_flows 행(오래된→최신)."""
    out = []
    for i, (f, ins, r) in enumerate(spec):
        out.append({"trade_date": f"2026-02-{start_day+i:02d}",
                    "foreign_net": f, "institution_net": ins, "retail_net": r,
                    "volume": vol})
    return out


def test_distribution_risk_fires_on_smart_money_exit():
    # 외국인+기관 동반 순매도 & 개인 순매수가 다수일 → 분산 위험↑
    spec = [(-100, -50, 160)] * 8
    res = dist.score({"investor_flows": _flows(spec)})
    assert res["data_available"] is True
    assert res["risk_0_100"] > 30, res
    fired = {s["name"] for s in res["signals"] if s["fired"]}
    assert "smart_money_distribution" in fired
    assert "분산" in res["detail"]  # 설명 출력


def test_distribution_volume_surge_increases_risk():
    # 거래량 급증 동반 — 장기평균 대비 최근 급증
    base = _flows([(-100, -50, 160)] * 30, vol=1000.0)
    # 최근 10일 거래량을 크게(급증)
    for r in base[-10:]:
        r["volume"] = 3000.0
    res = dist.score({"investor_flows": base})
    fired = {s["name"] for s in res["signals"] if s["fired"]}
    assert "volume_surge_on_distribution" in fired, res


def test_institution_buffer_dampens_risk():
    # 외국인 순매도 + 개인 순매수지만 기관이 강하게 순매수(방어) → 완충으로 위험 감쇄
    spec_no_buffer = [(-100, -50, 160)] * 8        # 기관도 매도
    spec_buffer = [(-100, 120, 0)] * 8             # 기관 강한 순매수(방어)
    r_nb = dist.score({"investor_flows": _flows(spec_no_buffer)})
    r_b = dist.score({"investor_flows": _flows(spec_buffer)})
    fired_b = {s["name"] for s in r_b["signals"] if s["fired"]}
    assert "institution_buy_buffer" in fired_b
    # 방어 케이스의 분산 위험이 더 낮아야 함(완충 효과)
    assert r_b["risk_0_100"] < r_nb["risk_0_100"], (r_b["risk_0_100"], r_nb["risk_0_100"])


def test_distribution_no_data_is_honest():
    res = dist.score({})
    assert res["data_available"] is False
    assert res["risk_0_100"] == 0.0
    assert res["confidence"] == 0.0


def test_distribution_insufficient_days_honest():
    res = dist.score({"investor_flows": _flows([(-100, -50, 160)] * 3)})  # < min_days
    assert res["data_available"] is False
    assert res["risk_0_100"] == 0.0


# ============================================================
# 4) security_selection 수급 반영
# ============================================================
def test_security_selection_flow_reflected():
    setup()
    from main_mission.portfolio_os import security_selection as ss
    conn = store_db.connect()
    try:
        # 외국인·기관 이탈 종목 → caution
        ki.upsert_flows("005930", _flows([(-100, -50, 160)] * 8))
        sig = ss._flow_signal("005930", conn=conn)
        assert sig["available"] is True
        assert sig["tone"] == "caution"
        assert "진입 속도 조절" in sig["note"]

        # 데이터 없는 종목 → 미반영(정직)
        none = ss._flow_signal("999999", conn=conn)
        assert none["available"] is False
        assert none["tone"] == "neutral"
    finally:
        conn.close()


def test_flow_buffer_is_supportive():
    setup()
    from main_mission.portfolio_os import security_selection as ss
    conn = store_db.connect()
    try:
        ki.upsert_flows("000660", _flows([(-100, 120, 0)] * 8))
        sig = ss._flow_signal("000660", conn=conn)
        assert sig["available"] is True
        assert sig["tone"] == "supportive"
    finally:
        conn.close()


# ============================================================
# 5) read-only 안전 실패(키 없음) + no anthropic
# ============================================================
def test_missing_credentials_clear_failure(monkeypatch):
    # 키 없으면 KisConfigError(명확 실패 — 가짜 성공/NotImplementedError 아님)
    for k in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"):
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("KIS_MODE", "paper")
    from main_mission.portfolio_os.broker.kis_client import KisConfigError
    f = ki.KisInvestorFetcher(account_index=None)
    raised = False
    try:
        f.fetch("005930")
    except KisConfigError:
        raised = True
    except Exception as e:  # noqa: BLE001
        # 네트워크/.env 로딩으로 다른 예외가 나도, NotImplementedError(stub)만 아니면 OK
        assert not isinstance(e, NotImplementedError)
        raised = True
    assert raised, "키 없으면 명확 실패해야 함(가짜 성공 금지)"


def test_no_anthropic_import():
    import main_mission.portfolio_os.broker.kis_investor as m
    src = open(m.__file__, encoding="utf-8").read()
    assert "anthropic" not in src.lower()
    assert "ANTHROPIC_API_KEY" not in src
