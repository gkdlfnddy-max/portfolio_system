"""키움증권 REST API adapter — BrokerPort 구현.

멀티 브로커 원칙: KIS 코드에 키움 예외처리 추가 금지. 키움은 **독립 adapter**.
1차 목표(키 발급 후): 토큰검증 → 잔고 → 보유종목 → 현재가 → DB snapshot(KIS 와 동일 표준 구조).
주문(place_order/cancel_order)은 **2차** — NotImplemented 유지 (잔고/가격 검증·risk gate·
승인·account PIN·live 하드락 후 단계). 모의투자(paper) 우선.

자격증명 미설정 시 **명확히 실패(KiwoomNotConfigured)** — 비밀은 절대 노출하지 않는다.
자격증명: .env 의 KIWOOM_ACCOUNT_{n}_APP_KEY / _APP_SECRET / _ACCOUNT_NO (평문 로그 금지).

응답 필드 주의: kt00018/kt00001 의 일부 응답 필드명은 공식 문서 재확인 필요(docs 참고).
누락/이름변경에 견디도록 _pick() 로 후보 키를 순회한다(임의 추측이 아니라 폴백).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from . import kiwoom_client as kc
from .kiwoom_client import KiwoomHttpClient, KiwoomConfigError
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


class KiwoomNotConfigured(KiwoomConfigError):
    """키움 REST 키 미설정 — 조회/주문 차단(비밀 미노출). (하위호환 alias)"""


def _dec(value: Any) -> Decimal:
    try:
        s = str(value).strip().replace(",", "")
        return Decimal(s or "0")
    except Exception:
        return Decimal(0)


def _pick(d: dict, *keys: str) -> Any:
    """응답 dict 에서 후보 키들 중 처음 발견되는 값 반환 (필드명 불확실 대응 폴백)."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def domestic_instrument(ticker: str, name: str = "") -> Instrument:
    return Instrument(ticker=ticker, market="KRX", currency="KRW", asset_class="stock")


class KiwoomRestAdapter:
    """BrokerPort 호환 키움 REST 어댑터. 키 발급 전까지 조회/주문은 안전하게 차단."""

    def __init__(self, account_index: int | None = None, mode: str = "paper",
                 client: KiwoomHttpClient | None = None) -> None:
        self.account_index = account_index
        self._mode = (mode or "paper").strip().lower()  # 키움도 모의투자(paper) 우선
        self._client = client
        self._seen_client_ids: set[str] = set()

    @property
    def mode(self) -> str:
        return self._mode

    def _prefix(self) -> str:
        return f"KIWOOM_ACCOUNT_{self.account_index}_" if self.account_index else "KIWOOM_"

    def _configured(self) -> bool:
        pre = self._prefix()
        return bool(os.getenv(pre + "APP_KEY", "").strip() and os.getenv(pre + "APP_SECRET", "").strip())

    @property
    def client(self) -> KiwoomHttpClient:
        """lazy client — 키 없으면 명확히 차단(비밀 미노출)."""
        self._need()
        if self._client is None:
            self._client = KiwoomHttpClient(mode=self._mode, account_index=self.account_index)
        return self._client

    @property
    def is_healthy(self) -> bool:
        # 키 미설정이면 unhealthy → sync_job/루프가 주문 시도 안 함(안전 A3).
        if not self._configured():
            return False
        if self._client is not None:
            return self._client.is_healthy
        return True

    def _need(self) -> None:
        if not self._configured():
            raise KiwoomNotConfigured(
                "키움 REST 자격증명 미설정 — .env 의 "
                f"{self._prefix()}APP_KEY / {self._prefix()}APP_SECRET 가 필요합니다. "
                "(키움 REST 앱키 발급·모의투자 신청 후. docs/portfolio/kiwoom_onboarding.md 참고. "
                "평문 키는 로그/DB 저장 금지.)"
            )

    # --- 인증 ---------------------------------------------------------------
    def ensure_token(self) -> None:
        self.client.ensure_token()

    # --- 조회 (read) --------------------------------------------------------
    def get_cash_krw(self, account: Account | None = None) -> Decimal:
        """예수금(주문가능현금 근사) — kt00001 예수금상세현황."""
        resp = self.client.request(kc.PATH_ACCOUNT, kc.API_DEPOSIT, body={})
        # 후보: entr(예수금) / 주문가능금액. 둘 다 없으면 0.
        cash = _pick(resp, "entr", "100stk_ord_alow_amt", "ord_alow_amt")
        return _dec(cash)

    def get_balance(self, account: Account | None = None) -> list[BalanceLine]:
        """보유종목 → BalanceLine. kt00018 계좌평가잔고내역.

        잔고 list 키 후보: acnt_evlt_remn_indv_tot / stk_acnt_evlt_prst / output.
        """
        resp = self.client.request(
            kc.PATH_ACCOUNT, kc.API_BALANCE,
            body={
                "qry_tp": "1",          # 1=합산, 2=개별 (기본 합산)
                "dmst_stex_tp": "KRX",  # 국내거래소
            },
        )
        rows = (
            _pick(resp, "acnt_evlt_remn_indv_tot", "stk_acnt_evlt_prst", "output", "output1")
            or []
        )
        if isinstance(rows, dict):
            rows = [rows]
        lines: list[BalanceLine] = []
        for h in rows:
            qty = _dec(_pick(h, "rmnd_qty", "stk_qty", "hldg_qty"))
            if qty == 0:
                continue
            ticker = str(_pick(h, "stk_cd", "pdno", "ticker") or "").strip().lstrip("A")
            name = str(_pick(h, "stk_nm", "prdt_name") or "")
            mv = _dec(_pick(h, "evlt_amt", "evltv_amt", "tot_evlt_amt"))
            avg = _dec(_pick(h, "pur_pric", "buy_uv", "pchs_avg_pric"))
            lines.append(BalanceLine(
                instrument=domestic_instrument(ticker, name),
                qty=qty,
                avg_price=avg,
                market_value=mv,
                currency="KRW",
                value_krw=mv,
            ))
        return lines

    def get_quote(self, instrument: Instrument) -> Quote:
        """현재가 — ka10001 주식기본정보."""
        ticker = instrument.ticker.strip()
        resp = self.client.request(kc.PATH_STOCK_INFO, kc.API_STOCK_INFO, body={"stk_cd": ticker})
        price = abs(_dec(_pick(resp, "cur_prc", "stck_prpr", "prpr")))
        return Quote(
            instrument=instrument,
            price=price,
            currency="KRW",
            captured_at=datetime.now(timezone.utc),
            is_stale=(price == 0),
        )

    def get_fx_rate(self, pair: str = "USDKRW") -> Decimal:
        # 키움 단독 FX 엔드포인트 미확인 — 외부 환율 소스 필요 (KIS 와 동일 상태).
        raise NotImplementedError("키움 FX 미구현 — 외부 환율 소스 연결 필요")

    def get_open_orders(self, account: Account | None = None) -> list[Order]:
        # 미체결 조회 endpoint 확인 필요. 1차 범위 외(주문 2차) → 빈 목록.
        self._need()
        return []

    def get_fills(self, account: Account | None = None, since: datetime | None = None) -> list[Fill]:
        # 체결 조회 endpoint 확인 필요. 1차 범위 외(주문 2차) → 빈 목록.
        self._need()
        return []

    # --- 주문 (write) — 2차 단계 (조회 검증·risk gate·승인·PIN·live 하드락 후) -------
    def place_order(self, account, req) -> OrderAck:
        raise NotImplementedError(
            "키움 주문 — 2차 단계. 잔고/가격 검증·risk gate·사용자 승인·account PIN·"
            "live 하드락 해제 후 구현 (시장가 매수 영구 금지, 진입은 지정가)."
        )

    def cancel_order(self, account, broker_order_id) -> CancelAck:
        raise NotImplementedError("키움 주문 취소 — 주문(2차) 단계에서 구현")

    # --- 진단 ---------------------------------------------------------------
    def health_check(self) -> dict:
        """연결 준비 상태(비밀 미노출). UI 계좌 카드가 표시."""
        return {
            "broker": "kiwoom",
            "mode": self._mode,
            "configured": self._configured(),
            "note": ("키움 REST 키 확인됨 — 잔고/보유종목 동기화 가능"
                     if self._configured() else
                     "키움 REST 앱키 미발급 — .env 에 KIWOOM 자격증명 추가 후 동기화 가능"),
        }
