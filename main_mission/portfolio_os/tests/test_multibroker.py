"""멀티 브로커 — factory broker 분기 + 키움 스텁 안전 + KIS/키움 어댑터 분리."""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_mbroker.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.broker import factory
from main_mission.portfolio_os.broker.kiwoom_adapter import KiwoomRestAdapter, KiwoomNotConfigured
from main_mission.portfolio_os.broker.mock_adapter import MockAdapter


def test_factory_dispatches_kiwoom():
    b = factory.get_broker(account_index=2, broker="kiwoom")
    assert isinstance(b, KiwoomRestAdapter), type(b)
    assert b.account_index == 2 and b.mode == "paper", (b.account_index, b.mode)


def test_factory_kis_path_unchanged():
    # broker=kis(기본) + mode mock → 기존 KIS 경로(MockAdapter). 키움 코드 안 탐.
    b = factory.get_broker(account_index=3, broker="kis", mode="mock")
    assert isinstance(b, MockAdapter), type(b)
    # broker 미지정 기본도 kis 경로
    b2 = factory.get_broker(mode="mock")
    assert isinstance(b2, MockAdapter), type(b2)


def test_kiwoom_stub_blocks_without_keys():
    b = KiwoomRestAdapter(account_index=9)  # KIWOOM_ACCOUNT_9_* 없음
    assert b.is_healthy is False                      # 키 없으면 unhealthy → 주문 시도 안 함
    hc = b.health_check()
    assert hc["broker"] == "kiwoom" and hc["configured"] is False, hc
    # 조회는 명확히 차단(비밀 미노출)
    try:
        b.get_balance({"id": 9}); assert False, "키 없이 잔고조회가 통과됨"
    except KiwoomNotConfigured as e:
        assert "APP_KEY" in str(e) and "APP_SECRET" in str(e)


def test_kiwoom_order_blocked():
    b = KiwoomRestAdapter(account_index=9)
    try:
        b.place_order({"id": 9}, {"x": 1}); assert False, "키움 주문이 막히지 않음"
    except NotImplementedError as e:
        # 주문은 잔고검증·risk gate·승인·PIN·live락 후 단계라는 메시지
        assert "주문" in str(e)


def test_kis_kiwoom_adapters_isolated():
    # 같은 account_index 라도 broker 가 다르면 완전히 다른 adapter 클래스(혼용 없음).
    kis = factory.get_broker(account_index=5, broker="kis", mode="mock")
    kiwoom = factory.get_broker(account_index=5, broker="kiwoom")
    assert type(kis) is not type(kiwoom)
    assert isinstance(kiwoom, KiwoomRestAdapter) and not isinstance(kis, KiwoomRestAdapter)


def test_unsupported_broker_rejected():
    try:
        factory.get_broker(account_index=1, broker="삼성")
        assert False, "미지원 broker 가 통과됨"
    except ValueError as e:
        assert "broker" in str(e)


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} MULTI-BROKER TESTS PASSED")
