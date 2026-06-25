"""Broker layer — KIS adapter 격리.

호출측(strategy/portfolio/risk)은 BrokerPort 만 의존한다.
구체 adapter(mock/paper/live)는 get_broker() 가 KIS_MODE 로 주입한다.
"""
from .port import (
    Account,
    BalanceLine,
    BrokerPort,
    CancelAck,
    Fill,
    Instrument,
    Order,
    OrderAck,
    OrderRequest,
    Quote,
)
from .mock_adapter import MockAdapter
from .kis_adapter import KisPaperAdapter, KisLiveAdapter
from .factory import get_broker  # KIS_MODE → mock|paper|live 주입 (live 는 KIS_LIVE_CONFIRM 가드)

__all__ = [
    "Account",
    "BalanceLine",
    "BrokerPort",
    "CancelAck",
    "Fill",
    "Instrument",
    "Order",
    "OrderAck",
    "OrderRequest",
    "Quote",
    "MockAdapter",
    "KisPaperAdapter",
    "KisLiveAdapter",
    "get_broker",
]
