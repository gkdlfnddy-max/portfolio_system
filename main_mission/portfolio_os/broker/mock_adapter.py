"""MockAdapter — 오프라인·결정론 테스트용 broker.

KIS 없이 루프 전체(조회→제안→리스크→승인→주문→체결)를 돌려보기 위한 가짜 broker.
가격/잔고 고정. place_order 는 즉시 체결로 시뮬레이션하되 idempotency 는 지킨다.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .port import (
    Account,
    BalanceLine,
    CancelAck,
    Fill,
    Instrument,
    Order,
    OrderAck,
    OrderRequest,
    Quote,
)

_SAMSUNG = Instrument("005930", "KRX", "KRW", "stock")
_AAPL = Instrument("AAPL", "NASDAQ", "USD", "stock")
_SOXL = Instrument("SOXL", "NYSE", "USD", "etf", is_leveraged=True)


class MockAdapter:
    """결정론적 가짜 broker. BrokerPort 를 구조적으로 만족."""

    def __init__(self) -> None:
        self._healthy = True
        self._seen_client_ids: set[str] = set()   # 중복 주문 방지 검증용
        self._fx = Decimal("1380")
        self._prices = {
            ("005930", "KRX"): Decimal("78000"),
            ("AAPL", "NASDAQ"): Decimal("230"),
            ("SOXL", "NYSE"): Decimal("32"),
        }

    @property
    def mode(self) -> str:
        return "paper"

    @property
    def is_healthy(self) -> bool:
        return self._healthy

    def set_healthy(self, value: bool) -> None:
        """테스트에서 API 장애 시뮬레이션."""
        self._healthy = value

    def ensure_token(self) -> None:
        if not self._healthy:
            raise RuntimeError("broker unhealthy — 토큰 발급 불가 (안전 A3)")

    def get_balance(self, account: Account) -> list[BalanceLine]:
        return [
            BalanceLine(_SAMSUNG, Decimal(100), Decimal("70000"),
                        Decimal("7800000"), "KRW", Decimal("7800000")),
            BalanceLine(_AAPL, Decimal(20), Decimal("200"),
                        Decimal("4600"), "USD", Decimal("4600") * self._fx),
        ]

    def get_quote(self, instrument: Instrument) -> Quote:
        price = self._prices.get((instrument.ticker, instrument.market), Decimal(0))
        return Quote(instrument, price, instrument.currency, datetime(2026, 6, 19, 10, 0, 0))

    def get_fx_rate(self, pair: str = "USDKRW") -> Decimal:
        return self._fx

    def get_open_orders(self, account: Account) -> list[Order]:
        return []

    def get_fills(self, account: Account, since: datetime) -> list[Fill]:
        return []

    def place_order(self, account: Account, req: OrderRequest) -> OrderAck:
        if not self._healthy:
            return OrderAck(req.client_order_id, None, False, "broker unhealthy (A3)")
        if req.client_order_id in self._seen_client_ids:
            # 안전 A4 — 같은 주문 중복 실행 방지
            return OrderAck(req.client_order_id, None, False, "duplicate client_order_id (A4)")
        self._seen_client_ids.add(req.client_order_id)
        return OrderAck(req.client_order_id, f"MOCK-{len(self._seen_client_ids)}", True)

    def cancel_order(self, account: Account, broker_order_id: str) -> CancelAck:
        return CancelAck(broker_order_id, True)
