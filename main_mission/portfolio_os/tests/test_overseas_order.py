"""미국(해외) 주문 배선 — KisPaperAdapter/_place_overseas 의 요청 구성 검증.

실 KIS 미호출(stub client). body/tr_id/거래소코드(OVRS_EXCG_CD)·지정가·시장가 금지를 고정.
⚠️ tr_id 자체는 KIS 공식 미검증(코드 상수). 여기선 '구성이 의도대로인지'만 검증한다.
"""
from __future__ import annotations

from decimal import Decimal

from main_mission.portfolio_os.broker.kis_adapter import KisPaperAdapter, KisLiveAdapter
from main_mission.portfolio_os.broker import kis_endpoints as ep
from main_mission.portfolio_os.broker.port import Instrument, OrderRequest


class _StubClient:
    def __init__(self):
        self.account_no = "50000000"
        self.account_prod = "01"
        self.posted = []           # (path, tr_id, body)

    @property
    def is_healthy(self):
        return True

    def hashkey(self, body):
        return "HASH"

    def post(self, path, tr_id, body, hashkey=None, timeout=10):
        self.posted.append((path, tr_id, dict(body)))
        return {"rt_cd": "0", "output": {"ODNO": "0001"}, "msg1": "정상"}


def _req(ticker, market, ccy, *, exchange="", side="buy", qty=2, limit="100.50", otype="limit"):
    inst = Instrument(ticker, market, ccy, "etf", exchange=exchange)
    return OrderRequest(client_order_id=f"t-{ticker}-{side}", instrument=inst, side=side,
                        qty=Decimal(qty), order_type=otype, limit_price=Decimal(limit))


def test_exchange_mapping():
    assert ep.kis_overseas_exchange("NASDAQ") == "NASD"
    assert ep.kis_overseas_exchange("NYSE") == "NYSE"
    assert ep.kis_overseas_exchange("ARCA") == "AMEX"
    assert ep.kis_overseas_exchange("US") == ""        # 미상


def test_us_order_routes_overseas_with_correct_body():
    c = _StubClient()
    ad = KisPaperAdapter(c)
    ack = ad.place_order(None, _req("QQQ", "US", "USD", exchange="NASD", limit="350.25"))
    assert ack.accepted and ack.broker_order_id == "0001"
    path, tr_id, body = c.posted[-1]
    assert path == ep.PATH_OVERSEAS_ORDER
    assert tr_id == ep.TRID_OVERSEAS_ORDER[("paper", "buy")]
    assert body["OVRS_EXCG_CD"] == "NASD"
    assert body["PDNO"] == "QQQ" and body["ORD_QTY"] == "2"
    assert body["OVRS_ORD_UNPR"] == "350.25"           # 소수 호가 보존(센트)
    assert body["ORD_DVSN"] == "00"                    # 지정가


def test_exchange_from_market_when_not_explicit():
    c = _StubClient()
    ack = KisLiveAdapter(c).place_order(None, _req("SPY", "AMEX", "USD", limit="700"))
    assert ack.accepted
    _, tr_id, body = c.posted[-1]
    assert body["OVRS_EXCG_CD"] == "AMEX"
    assert tr_id == ep.TRID_OVERSEAS_ORDER[("live", "buy")]   # live 코드


def test_unknown_exchange_refused():
    c = _StubClient()
    ack = KisPaperAdapter(c).place_order(None, _req("FOO", "US", "USD"))  # 거래소 미상
    assert ack.accepted is False and "거래소코드 미상" in (ack.reason or "")
    assert not c.posted                                # 전송 안 함


def test_market_buy_blocked():
    c = _StubClient()
    ack = KisPaperAdapter(c).place_order(None, _req("QQQ", "US", "USD", exchange="NASD", otype="market"))
    assert ack.accepted is False and "시장가" in (ack.reason or "")
    assert not c.posted


def test_domestic_still_routes_domestic():
    c = _StubClient()
    ack = KisPaperAdapter(c).place_order(None, _req("005930", "KRX", "KRW", limit="70000"))
    assert ack.accepted
    path, tr_id, body = c.posted[-1]
    assert path == ep.PATH_DOMESTIC_ORDER
    assert "ORD_UNPR" in body and "OVRS_EXCG_CD" not in body
