"""conn_test (stage별 연결 테스트) 검증.

정직 원칙(가짜 성공 금지):
  - 키 미설정 → credential stage 에서 정직 실패(reason=credential), 이후 stage 미진행.
  - 키 없을 때 KIS 경로로 새지 않고 broker 별 정직 차단. KIS 무영향.
  - 비밀값(키/시크릿/토큰)은 어떤 stage 결과에도 들어가지 않음.
  - 성공 경로는 실 네트워크 없이 HTTP 계층만 monkeypatch (가짜 성공 아님 — 어댑터 실제 호출).
  - 주문 stage 없음(place_order 호출 안 함).
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_conn_test.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

# 테스트 격리: 11/12번 index 의 키/모드/브로커가 환경에 새지 않도록 비움.
for _n in (11, 12):
    for _suf in ("APP_KEY", "APP_SECRET", "ACCOUNT_NO", "MODE", "BROKER"):
        os.environ.pop(f"KIWOOM_ACCOUNT_{_n}_{_suf}", None)
        os.environ.pop(f"KIS_ACCOUNT_{_n}_{_suf}", None)

from main_mission.portfolio_os.broker import conn_test
from main_mission.portfolio_os.broker.kiwoom_client import KiwoomHttpClient
from main_mission.portfolio_os.broker import kiwoom_client as kc
from main_mission.portfolio_os.store import db as store_db


def setup():
    # 다른 테스트 모듈과 동일 패턴 — 현재(공유) SQLITE_PATH 에 스키마 보장(idempotent).
    store_db.init()


def _stage(res, key):
    for s in res["stages"]:
        if s["stage"] == key:
            return s
    return None


# --- 키 미설정: credential 에서 정직 실패 (KIS 무영향) -------------------------
def test_kis_no_keys_fails_at_credential():
    res = conn_test.test_connection(11, save=False)
    assert res["broker"] == "kis"
    assert res["ok"] is False
    cred = _stage(res, "credential")
    assert cred is not None and cred["ok"] is False
    assert cred["reason"] == "credential"
    # 이후 stage 는 진행되지 않음(가짜 성공 금지)
    assert [s["stage"] for s in res["stages"]] == ["credential"]
    assert res["snapshot_saved"] is False


def test_kiwoom_no_keys_fails_at_credential(monkeypatch):
    monkeypatch.setenv("KIS_ACCOUNT_11_BROKER", "kiwoom")
    res = conn_test.test_connection(11, save=False)
    assert res["broker"] == "kiwoom"
    assert res["ok"] is False
    cred = _stage(res, "credential")
    assert cred["ok"] is False and cred["reason"] == "credential"
    assert len(res["stages"]) == 1  # 안전 차단


def test_no_secret_in_result():
    # 키가 애초에 없으므로 비밀이 셀 수 없지만, 메시지에 Bearer/secret 토큰류 없음 확인.
    res = conn_test.test_connection(11, save=False)
    blob = str(res)
    assert "Bearer" not in blob
    assert "APP_SECRET" in blob  # 라벨은 안내용으로 허용(값 아님)


# --- stage 순서 상수 (웹 체크리스트 매핑 안정성) --------------------------------
def test_stage_order_constant():
    assert conn_test.STAGE_ORDER == ["credential", "token", "account", "cash", "balance", "quote"]


# --- 원인 분리 분류기 ----------------------------------------------------------
def test_classify_reasons():
    assert conn_test._classify(RuntimeError("HTTP 429 rate limit")) == "rate_limit"
    assert conn_test._classify(RuntimeError("네트워크 오류 (unhealthy)")) == "network"
    assert conn_test._classify(RuntimeError("토큰 발급 실패")) == "token"

    class KiwoomNotConfigured(Exception):
        pass

    assert conn_test._classify(KiwoomNotConfigured("x")) == "credential"


# --- 성공 경로: 실 네트워크 없이 HTTP 계층만 mock (키움) ------------------------
def test_kiwoom_full_success_mocked(monkeypatch):
    monkeypatch.setenv("KIS_ACCOUNT_12_BROKER", "kiwoom")
    monkeypatch.setenv("KIWOOM_ACCOUNT_12_APP_KEY", "TESTKEY")
    monkeypatch.setenv("KIWOOM_ACCOUNT_12_APP_SECRET", "TESTSECRET")
    monkeypatch.setenv("KIWOOM_ACCOUNT_12_ACCOUNT_NO", "1234567890")
    monkeypatch.setenv("KIWOOM_ACCOUNT_12_MODE", "paper")

    monkeypatch.setattr(KiwoomHttpClient, "ensure_token", lambda self: "TKN")

    def fake_request(self, path, api_id, body=None, cont_yn="N", next_key="", timeout=10):
        if api_id == kc.API_DEPOSIT:
            return {"entr": "1000000"}
        if api_id == kc.API_BALANCE:
            return {"acnt_evlt_remn_indv_tot": [
                {"stk_cd": "A005930", "stk_nm": "삼성전자", "rmnd_qty": "10",
                 "pur_pric": "70000", "evlt_amt": "750000"},
            ]}
        if api_id == kc.API_STOCK_INFO:
            return {"cur_prc": "-71500"}
        return {}

    monkeypatch.setattr(KiwoomHttpClient, "request", fake_request)

    res = conn_test.test_connection(12, save=False)
    assert res["broker"] == "kiwoom"
    assert res["mode"] == "paper"
    assert res["ok"] is True, res
    assert [s["stage"] for s in res["stages"]] == conn_test.STAGE_ORDER
    assert all(s["ok"] for s in res["stages"])
    assert "TESTSECRET" not in str(res)


def test_kiwoom_token_failure_stops(monkeypatch):
    monkeypatch.setenv("KIS_ACCOUNT_12_BROKER", "kiwoom")
    monkeypatch.setenv("KIWOOM_ACCOUNT_12_APP_KEY", "TESTKEY")
    monkeypatch.setenv("KIWOOM_ACCOUNT_12_APP_SECRET", "TESTSECRET")
    monkeypatch.setenv("KIWOOM_ACCOUNT_12_ACCOUNT_NO", "1234567890")
    monkeypatch.setenv("KIWOOM_ACCOUNT_12_MODE", "paper")

    def boom(self):
        raise RuntimeError("키움 토큰 발급 실패 (broker unhealthy, A3): 네트워크 오류")

    monkeypatch.setattr(KiwoomHttpClient, "ensure_token", boom)
    res = conn_test.test_connection(12, save=False)
    assert res["ok"] is False
    tok = _stage(res, "token")
    assert tok is not None and tok["ok"] is False
    # token 까지만 진행(credential ok, token fail) — 이후 stage 없음
    assert [s["stage"] for s in res["stages"]] == ["credential", "token"]


def test_kiwoom_missing_account_no_fails(monkeypatch):
    monkeypatch.setenv("KIS_ACCOUNT_12_BROKER", "kiwoom")
    monkeypatch.setenv("KIWOOM_ACCOUNT_12_APP_KEY", "TESTKEY")
    monkeypatch.setenv("KIWOOM_ACCOUNT_12_APP_SECRET", "TESTSECRET")
    monkeypatch.delenv("KIWOOM_ACCOUNT_12_ACCOUNT_NO", raising=False)
    monkeypatch.setenv("KIWOOM_ACCOUNT_12_MODE", "paper")
    monkeypatch.setattr(KiwoomHttpClient, "ensure_token", lambda self: "TKN")

    res = conn_test.test_connection(12, save=False)
    assert res["ok"] is False
    acc = _stage(res, "account")
    assert acc is not None and acc["ok"] is False and acc["reason"] == "account"
    assert [s["stage"] for s in res["stages"]] == ["credential", "token", "account"]


if __name__ == "__main__":
    import sys
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for f in fns:
        if f.__code__.co_argcount:
            print(f"  SKIP {f.__name__} (needs fixture; run via pytest)")
            continue
        try:
            f(); print(f"  PASS {f.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"  FAIL {f.__name__}: {e}")
    sys.exit(1 if failed else 0)
