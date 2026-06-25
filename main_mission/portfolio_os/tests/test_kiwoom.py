"""키움 REST adapter/client 테스트.

검증 범위 (정직: 실 네트워크 호출은 키 없으면 하지 않음 — 가짜 성공 보고 금지):
  - 키 미설정 시 안전 차단(KiwoomNotConfigured / 명확 에러, 비밀 미노출)
  - factory 키움 분기 (KIS 경로 무변경)
  - is_healthy False(키 없음)
  - place_order/cancel_order NotImplemented (주문 2차)
  - KIS ↔ 키움 adapter 완전 격리
  - client 토큰 발급은 monkeypatch 로 mock (실 네트워크 X)
  - sync_job broker 디스패치 (키움 키 없으면 credentials 단계로 안전 실패)
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_kiwoom.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

# 테스트 격리: 키움/KIS 키가 환경에 새지 않도록 9/10번 index 는 항상 비움.
for _n in (9, 10):
    for _suf in ("APP_KEY", "APP_SECRET", "ACCOUNT_NO", "MODE"):
        os.environ.pop(f"KIWOOM_ACCOUNT_{_n}_{_suf}", None)
        os.environ.pop(f"KIS_ACCOUNT_{_n}_{_suf}", None)
    os.environ.pop(f"KIS_ACCOUNT_{_n}_BROKER", None)

from main_mission.portfolio_os.broker import factory
from main_mission.portfolio_os.broker import kiwoom_client as kc
from main_mission.portfolio_os.broker.kiwoom_adapter import (
    KiwoomRestAdapter,
    KiwoomNotConfigured,
)
from main_mission.portfolio_os.broker.kiwoom_client import KiwoomHttpClient, KiwoomConfigError
from main_mission.portfolio_os.broker.mock_adapter import MockAdapter
from main_mission.portfolio_os.broker.port import BrokerPort, Instrument


# --- 안전: 키 미설정 차단 -----------------------------------------------------
def test_unhealthy_without_keys():
    b = KiwoomRestAdapter(account_index=9)
    assert b.is_healthy is False
    hc = b.health_check()
    assert hc["broker"] == "kiwoom" and hc["configured"] is False, hc


def test_read_blocked_without_keys():
    b = KiwoomRestAdapter(account_index=9)
    for call in (b.get_balance, b.get_cash_krw, b.ensure_token):
        try:
            call()
            assert False, f"키 없이 {call.__name__} 통과됨"
        except KiwoomNotConfigured as e:
            assert "APP_KEY" in str(e) and "APP_SECRET" in str(e)
            # 비밀값이 메시지에 노출되지 않음(키가 애초에 없음 — 라벨만)
            assert "Bearer" not in str(e)


def test_quote_blocked_without_keys():
    b = KiwoomRestAdapter(account_index=9)
    inst = Instrument(ticker="005930", market="KRX", currency="KRW")
    try:
        b.get_quote(inst)
        assert False, "키 없이 현재가 통과됨"
    except KiwoomNotConfigured:
        pass


def test_client_requires_credentials():
    c = KiwoomHttpClient(mode="paper", account_index=9)
    assert c.configured() is False
    try:
        c.require_credentials()
        assert False
    except KiwoomConfigError as e:
        assert "APP_KEY" in str(e)


# --- 주문 2차 (미개방) --------------------------------------------------------
def test_order_not_implemented():
    b = KiwoomRestAdapter(account_index=9)
    for call in (lambda: b.place_order(None, None), lambda: b.cancel_order(None, "x")):
        try:
            call()
            assert False, "키움 주문이 막히지 않음"
        except NotImplementedError as e:
            assert "주문" in str(e)


# --- factory 분기 + KIS 격리 --------------------------------------------------
def test_factory_dispatches_kiwoom():
    b = factory.get_broker(account_index=9, broker="kiwoom")
    assert isinstance(b, KiwoomRestAdapter)
    assert b.account_index == 9 and b.mode == "paper"


def test_factory_kis_unchanged():
    b = factory.get_broker(account_index=9, broker="kis", mode="mock")
    assert isinstance(b, MockAdapter)
    assert not isinstance(b, KiwoomRestAdapter)


def test_kis_kiwoom_isolated():
    kis = factory.get_broker(account_index=9, broker="kis", mode="mock")
    kiwoom = factory.get_broker(account_index=9, broker="kiwoom")
    assert type(kis) is not type(kiwoom)
    assert isinstance(kiwoom, KiwoomRestAdapter) and not isinstance(kis, KiwoomRestAdapter)


def test_adapter_satisfies_brokerport():
    b = KiwoomRestAdapter(account_index=9)
    assert isinstance(b, BrokerPort)


# --- 토큰/잔고: 실 네트워크 없이 mock (가짜 성공 아님 — HTTP 계층만 대체) -------
def test_token_and_balance_with_mocked_http(monkeypatch):
    # 키 주입(테스트 한정, 가짜) — 키 게이트 통과 후 HTTP 계층만 대체.
    monkeypatch.setenv("KIWOOM_ACCOUNT_9_APP_KEY", "TESTKEY")
    monkeypatch.setenv("KIWOOM_ACCOUNT_9_APP_SECRET", "TESTSECRET")

    captured = {}

    def fake_raw_post(self, path, body, headers, timeout=10):
        captured["token_path"] = path
        captured["body"] = body
        # 키움 토큰 응답 형태
        return {"return_code": 0, "token": "TKN-xyz", "token_type": "bearer",
                "expires_dt": "20991231235959"}

    def fake_request(self, path, api_id, body=None, cont_yn="N", next_key="", timeout=10):
        if api_id == kc.API_DEPOSIT:
            return {"entr": "1000000"}
        if api_id == kc.API_BALANCE:
            return {"acnt_evlt_remn_indv_tot": [
                {"stk_cd": "A005930", "stk_nm": "삼성전자", "rmnd_qty": "10",
                 "pur_pric": "70000", "evlt_amt": "750000"},
                {"stk_cd": "000660", "stk_nm": "SK", "rmnd_qty": "0",
                 "pur_pric": "0", "evlt_amt": "0"},  # qty 0 → 제외
            ]}
        return {}

    monkeypatch.setattr(KiwoomHttpClient, "_raw_post", fake_raw_post)
    monkeypatch.setattr(KiwoomHttpClient, "request", fake_request)

    b = KiwoomRestAdapter(account_index=9, mode="paper")
    assert b.is_healthy is True
    # 이전 실행의 토큰 파일 캐시가 있으면 _raw_post 가 호출되지 않으므로 제거.
    cache = b.client._token_cache_path()
    if cache.exists():
        cache.unlink()
    b.ensure_token()
    assert captured["token_path"] == kc.PATH_TOKEN
    assert captured["body"]["grant_type"] == "client_credentials"
    assert captured["body"]["secretkey"] == "TESTSECRET"  # 키움은 secretkey

    cash = b.get_cash_krw()
    assert int(cash) == 1000000

    lines = b.get_balance()
    assert len(lines) == 1  # qty 0 제외
    ln = lines[0]
    assert ln.instrument.ticker == "005930"  # A prefix 제거
    assert int(ln.qty) == 10 and int(ln.market_value) == 750000


def test_quote_with_mocked_http(monkeypatch):
    monkeypatch.setenv("KIWOOM_ACCOUNT_9_APP_KEY", "TESTKEY")
    monkeypatch.setenv("KIWOOM_ACCOUNT_9_APP_SECRET", "TESTSECRET")
    monkeypatch.setattr(KiwoomHttpClient, "ensure_token", lambda self: "TKN")
    monkeypatch.setattr(KiwoomHttpClient, "request",
                        lambda self, path, api_id, body=None, **k: {"cur_prc": "-71500"})
    b = KiwoomRestAdapter(account_index=9)
    q = b.get_quote(Instrument(ticker="005930", market="KRX", currency="KRW"))
    assert int(q.price) == 71500 and q.is_stale is False  # 부호(-) 절대값 처리


# --- sync_job broker 디스패치 (키움 키 없음 → credentials 안전 실패) ----------
def test_sync_dispatch_kiwoom_blocks_without_keys(monkeypatch):
    monkeypatch.setenv("KIS_ACCOUNT_9_BROKER", "kiwoom")
    from main_mission.portfolio_os.broker import sync_job
    assert sync_job._account_broker(9) == "kiwoom"
    res = sync_job.fetch_account(9)  # 키 없음 → KIS 경로로 새지 않고 키움 credentials 실패
    assert res["ok"] is False and res["stage"] == "credentials"
    assert "APP_KEY" in res["error"]


def test_secret_never_logged(capsys):
    # 진단 출력/요약에 비밀 원문이 들어가지 않음.
    c = KiwoomHttpClient(mode="paper", account_index=9)
    summ = c.credential_summary()
    assert "TESTSECRET" not in str(summ)
    assert summ["broker"] == "kiwoom"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for f in fns:
        argc = f.__code__.co_argcount
        if argc:  # monkeypatch/capsys fixtures — pytest 로만 실행
            print(f"  SKIP {f.__name__} (needs fixture; run via pytest)")
            continue
        try:
            f(); print(f"  PASS {f.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"  FAIL {f.__name__}: {e}")
    sys.exit(1 if failed else 0)
