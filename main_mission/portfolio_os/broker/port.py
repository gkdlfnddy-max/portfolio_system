"""BrokerPort — 모든 broker adapter 가 구현하는 인터페이스.

KIS 의 국내/미국 · paper/live 차이를 이 뒤로 숨긴다.
호출측은 instrument.market 만 넘기고, tr_id 분기는 adapter 내부 책임.
세부: docs/portfolio/api_adapter.md
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

Mode = Literal["paper", "live"]
Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class Account:
    id: int
    mode: Mode
    base_currency: str = "KRW"


@dataclass(frozen=True)
class Instrument:
    ticker: str
    market: str          # KRX | US | NASDAQ | NYSE | AMEX
    currency: str        # KRW | USD
    asset_class: str = "stock"
    is_leveraged: bool = False
    is_inverse: bool = False
    exchange: str = ""   # KIS 해외 거래소 코드(NASD/NYSE/AMEX) — 미국 주문 OVRS_EXCG_CD 용


@dataclass(frozen=True)
class BalanceLine:
    instrument: Instrument
    qty: Decimal
    avg_price: Decimal
    market_value: Decimal       # 원통화
    currency: str
    value_krw: Decimal | None = None
    is_stale: bool = False


@dataclass(frozen=True)
class Quote:
    instrument: Instrument
    price: Decimal
    currency: str
    captured_at: datetime
    is_stale: bool = False


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str        # idempotency key (필수, 안전 A4)
    instrument: Instrument
    side: Side
    qty: Decimal
    order_type: Literal["market", "limit"] = "limit"
    limit_price: Decimal | None = None


@dataclass(frozen=True)
class OrderAck:
    client_order_id: str
    broker_order_id: str | None
    accepted: bool
    reason: str | None = None


@dataclass(frozen=True)
class CancelAck:
    broker_order_id: str
    canceled: bool
    reason: str | None = None


@dataclass(frozen=True)
class Order:
    client_order_id: str
    broker_order_id: str | None
    status: str
    filled_qty: Decimal = Decimal(0)


@dataclass(frozen=True)
class Fill:
    broker_order_id: str
    qty: Decimal
    price: Decimal
    currency: str
    filled_at: datetime


@runtime_checkable
class BrokerPort(Protocol):
    """모든 adapter 공통 계약."""

    @property
    def mode(self) -> Mode: ...

    @property
    def is_healthy(self) -> bool:
        """False 면 의사결정 루프가 ABORT (안전 A3 — API 장애 시 주문 중단)."""
        ...

    # --- 인증 ---
    def ensure_token(self) -> None: ...

    # --- 조회 (read) ---
    def get_balance(self, account: Account) -> list[BalanceLine]: ...
    def get_quote(self, instrument: Instrument) -> Quote: ...
    def get_fx_rate(self, pair: str = "USDKRW") -> Decimal: ...
    def get_open_orders(self, account: Account) -> list[Order]: ...
    def get_fills(self, account: Account, since: datetime) -> list[Fill]: ...

    # --- 주문 (write) — CEO 승인된 후보만 ---
    def place_order(self, account: Account, req: OrderRequest) -> OrderAck: ...
    def cancel_order(self, account: Account, broker_order_id: str) -> CancelAck: ...
