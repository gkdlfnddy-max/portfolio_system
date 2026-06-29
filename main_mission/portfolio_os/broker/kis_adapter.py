"""KisPaperAdapter / KisLiveAdapter — BrokerPort 의 KIS 구현.

paper(모의) 우선. live 는 KIS_MODE=live + CEO 승인 후에만 주입(factory).
국내주식 중심 (연결·관리 1차). 미국주식/FX/체결 push 는 TODO (api_adapter.md §3, §7).

읽기(잔고/시세)는 안전. place_order 는 *승인된 후보만* 호출됨(관리 수준=제안+승인).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from . import kis_endpoints as ep
from .kis_client import KisHttpClient
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


def _dec(value: str | None) -> Decimal:
    try:
        return Decimal(str(value).strip() or "0")
    except Exception:
        return Decimal(0)


def domestic_instrument(ticker: str, name: str = "") -> Instrument:
    return Instrument(ticker=ticker, market="KRX", currency="KRW", asset_class="stock")


class _KisAdapterBase:
    """국내주식 BrokerPort 구현 공통. mode 는 서브클래스가 고정."""

    _mode: ep.Mode

    def __init__(self, client: KisHttpClient | None = None) -> None:
        self.client = client or KisHttpClient(self._mode)
        self._seen_client_ids: set[str] = set()

    @property
    def mode(self) -> ep.Mode:
        return self._mode

    @property
    def is_healthy(self) -> bool:
        return self.client.is_healthy

    def ensure_token(self) -> None:
        self.client.ensure_token()

    # --- 조회 (read) ----------------------------------------------------
    def get_quote(self, instrument: Instrument) -> Quote:
        resp = self.client.get(
            ep.PATH_DOMESTIC_PRICE,
            ep.TRID_DOMESTIC_PRICE,
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": instrument.ticker},
        )
        out = resp.get("output", {}) or {}
        price = _dec(out.get("stck_prpr"))
        return Quote(
            instrument=instrument,
            price=price,
            currency="KRW",
            captured_at=datetime.now(timezone.utc),
            is_stale=(price == 0),
        )

    def get_daily_bars(self, ticker: str, *, start: str, end: str,
                       adjusted: bool = True) -> list[dict]:
        """국내주식 기간별시세(일봉) 조회 — read-only(주문 아님).

        start/end: 'YYYYMMDD'. KIS 1회 최대 100건 → 호출측(price_history)에서
        날짜 윈도우 페이징. 반환: [{trade_date='YYYY-MM-DD', open, high, low, close, volume}]
        (오래된→최신). rt_cd != 0 또는 응답 이상 시 RuntimeError(가짜 성공 금지).
        """
        resp = self.client.get(
            ep.PATH_DOMESTIC_DAILY_CHART,
            ep.TRID_DOMESTIC_DAILY_CHART,
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
                "FID_INPUT_DATE_1": start,
                "FID_INPUT_DATE_2": end,
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0" if adjusted else "1",
            },
        )
        rt = resp.get("rt_cd")
        if rt not in (None, "0"):
            raise RuntimeError(
                f"일봉조회 실패 rt_cd={rt} msg={resp.get('msg1')} (ticker={ticker})"
            )
        rows = resp.get("output2") or []
        bars: list[dict] = []
        for r in rows:
            d = (r.get("stck_bsop_date") or "").strip()
            clpr = r.get("stck_clpr")
            if len(d) != 8 or not clpr or _dec(clpr) == 0:
                continue  # 비거래일/빈 행 skip (가짜 데이터 금지)
            bars.append({
                "trade_date": f"{d[0:4]}-{d[4:6]}-{d[6:8]}",
                "open": float(_dec(r.get("stck_oprc"))),
                "high": float(_dec(r.get("stck_hgpr"))),
                "low": float(_dec(r.get("stck_lwpr"))),
                "close": float(_dec(clpr)),
                "volume": float(_dec(r.get("acml_vol"))),
            })
        bars.sort(key=lambda b: b["trade_date"])  # 오래된→최신
        return bars

    def get_balance(self, account: Account) -> list[BalanceLine]:
        resp = self.client.get(
            ep.PATH_DOMESTIC_BALANCE,
            ep.domestic_balance_trid(self._mode),
            {
                "CANO": self.client.account_no,
                "ACNT_PRDT_CD": self.client.account_prod,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        if resp.get("rt_cd") not in (None, "0"):
            raise RuntimeError(f"잔고조회 실패 rt_cd={resp.get('rt_cd')} msg={resp.get('msg1')}")
        lines: list[BalanceLine] = []
        for h in resp.get("output1", []) or []:
            qty = _dec(h.get("hldg_qty"))
            if qty == 0:
                continue
            inst = domestic_instrument(h.get("pdno", ""), h.get("prdt_name", ""))
            mv = _dec(h.get("evlu_amt"))
            lines.append(BalanceLine(
                instrument=inst,
                qty=qty,
                avg_price=_dec(h.get("pchs_avg_pric")),
                market_value=mv,
                currency="KRW",
                value_krw=mv,
            ))
        return lines

    def get_cash_krw(self, account: Account) -> Decimal:
        """예수금(주문가능현금 근사). 잔고 output2 의 dnca_tot_amt."""
        resp = self.client.get(
            ep.PATH_DOMESTIC_BALANCE,
            ep.domestic_balance_trid(self._mode),
            {
                "CANO": self.client.account_no,
                "ACNT_PRDT_CD": self.client.account_prod,
                "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
                "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            },
        )
        out2 = resp.get("output2", []) or []
        if out2:
            return _dec(out2[0].get("dnca_tot_amt"))
        return Decimal(0)

    def get_fx_rate(self, pair: str = "USDKRW") -> Decimal:
        # KIS 단독 FX 엔드포인트 미확인 (api_adapter.md §7.5) — 외부 소스 필요.
        raise NotImplementedError("FX 미구현 — 외부 환율 소스 연결 필요 (api_adapter.md §7.5)")

    def get_open_orders(self, account: Account) -> list[Order]:
        # TODO: inquire-psbl-rvsecncl / 일별주문체결 (api_adapter.md §3). 1차 범위 외.
        return []

    def get_fills(self, account: Account, since: datetime) -> list[Fill]:
        # TODO: 체결 조회 / WebSocket 체결통보 (api_adapter.md §7.3). 1차 범위 외.
        return []

    @staticmethod
    def _is_overseas(inst) -> bool:
        """미국(해외) 주문 여부 — 통화 USD 또는 market 이 KRX 류가 아니면 해외."""
        return (inst.currency or "").upper() == "USD" or (inst.market or "").upper() not in ("KRX", "KOSPI", "KOSDAQ")

    # --- 주문 (write) — 승인된 후보만 ----------------------------------
    def place_order(self, account: Account, req: OrderRequest) -> OrderAck:
        if not self.client.is_healthy:
            return OrderAck(req.client_order_id, None, False, "broker unhealthy (A3)")
        if req.client_order_id in self._seen_client_ids:
            return OrderAck(req.client_order_id, None, False, "duplicate client_order_id (A4)")
        # 시장가 매수 영구 금지(§16) — 국내/해외 공통.
        if req.order_type == "market" and req.side == "buy":
            return OrderAck(req.client_order_id, None, False, "시장가 매수 금지(§16) — 지정가만")

        if self._is_overseas(req.instrument):
            return self._place_overseas(req)
        return self._place_domestic(req)

    def _place_domestic(self, req: OrderRequest) -> OrderAck:
        ord_dvsn = "01" if req.order_type == "market" else "00"  # 01=시장가 00=지정가
        unpr = "0" if req.order_type == "market" else str(req.limit_price or 0)
        body = {
            "CANO": self.client.account_no,
            "ACNT_PRDT_CD": self.client.account_prod,
            "PDNO": req.instrument.ticker,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(int(req.qty)),
            "ORD_UNPR": unpr,
        }
        tr_id = ep.domestic_order_trid(self._mode, req.side)
        try:
            h = self.client.hashkey(body)
            resp = self.client.post(ep.PATH_DOMESTIC_ORDER, tr_id, body, hashkey=h)
        except Exception as exc:
            # 전송 불확실 → 재전송 금지. 상태는 get_open_orders 로 확인 (§5).
            return OrderAck(req.client_order_id, None, False, f"전송 오류(상태 재조회 필요): {exc}")
        ok = resp.get("rt_cd") == "0"
        if ok:
            self._seen_client_ids.add(req.client_order_id)
        broker_id = (resp.get("output", {}) or {}).get("ODNO")
        return OrderAck(req.client_order_id, broker_id, ok, resp.get("msg1"))

    def _place_overseas(self, req: OrderRequest) -> OrderAck:
        """미국(해외) **지정가** 주문. ⚠️ tr_id 미검증 — 실주문 전 KIS 공식 코드 확인 필수."""
        excg = (req.instrument.exchange or "").strip().upper() or ep.kis_overseas_exchange(req.instrument.market)
        if not excg:
            return OrderAck(req.client_order_id, None, False,
                            f"미국 거래소코드 미상('{req.instrument.market}') — NASD/NYSE/AMEX 확인 필요(주문 보류)")
        if req.order_type == "market":  # 해외는 지정가만
            return OrderAck(req.client_order_id, None, False, "미국 주문은 지정가만 — 시장가 불가(§16)")
        body = {
            "CANO": self.client.account_no,
            "ACNT_PRDT_CD": self.client.account_prod,
            "OVRS_EXCG_CD": excg,                        # NASD|NYSE|AMEX
            "PDNO": req.instrument.ticker,
            "ORD_QTY": str(int(req.qty)),
            "OVRS_ORD_UNPR": str(req.limit_price or 0),   # 지정가(USD, 소수 호가 허용)
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",                            # 00=지정가
        }
        tr_id = ep.overseas_order_trid(self._mode, req.side)
        try:
            h = self.client.hashkey(body)
            resp = self.client.post(ep.PATH_OVERSEAS_ORDER, tr_id, body, hashkey=h)
        except Exception as exc:
            return OrderAck(req.client_order_id, None, False, f"전송 오류(상태 재조회 필요): {exc}")
        ok = resp.get("rt_cd") == "0"
        if ok:
            self._seen_client_ids.add(req.client_order_id)
        broker_id = (resp.get("output", {}) or {}).get("ODNO")
        return OrderAck(req.client_order_id, broker_id, ok, resp.get("msg1"))

    def cancel_order(self, account: Account, broker_order_id: str) -> CancelAck:
        # TODO: order-rvsecncl (정정취소) — 원주문 정보 필요. 1차 범위 외.
        raise NotImplementedError("취소 미구현 — order-rvsecncl 연결 필요 (api_adapter.md §3)")


class KisPaperAdapter(_KisAdapterBase):
    _mode = "paper"


class KisLiveAdapter(_KisAdapterBase):
    """실전. factory 가 KIS_MODE=live + 승인 확인 후에만 생성."""
    _mode = "live"
