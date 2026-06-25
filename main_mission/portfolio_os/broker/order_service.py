"""주문 제출 안전 서비스 — 주문 전 검증 + idempotency + in_doubt + 감사.

핵심 흐름 강화 (CEO 정의):
  - 주문 전 검증: idempotency(원장) → 모드 일치 → broker health → 리스크 통과 → 매수여력
  - 주문 후 추적: orders 원장 상태머신 (submitting → submitted | in_doubt | rejected)
  - idempotency: client_order_id UNIQUE + payload_hash (같은 id+다른 payload = 거부, A4)
  - in_doubt: 전송 응답 불확실 시 재전송 금지, 원장에 in_doubt 기록 (§5)
  - 모든 결정은 audit_logs 에 기록

벤치마크 차용(정직 변형): Alpaca pre-order validation chain + rejection 분류,
IBKR/OMS in-doubt 상태머신, KIS api_adapter.md §5/§7.4.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from ..audit import logger as audit
from ..store import db as store_db
from ..growth import middleware as growth_mw
from .port import Account, OrderRequest


def payload_hash(req: OrderRequest) -> str:
    body = {
        "ticker": req.instrument.ticker,
        "market": req.instrument.market,
        "side": req.side,
        "qty": str(req.qty),
        "order_type": req.order_type,
        "limit_price": str(req.limit_price) if req.limit_price is not None else "",
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _result(ok: bool, status: str, reason: str | None = None, broker_order_id: str | None = None, **extra):
    return {"ok": ok, "status": status, "reason": reason, "broker_order_id": broker_order_id, **extra}


def submit_order(
    broker,
    account: Account,
    req: OrderRequest,
    *,
    available_cash_krw: float | None = None,
    risk_passed: bool | None = None,
    urgent_sell: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """승인된 주문 후보 1건을 안전하게 제출. 호출 전 리스크 게이트 결과를 risk_passed 로 전달.
    urgent_sell=True 는 '긴급 매도'에 한해 시장가 매도를 허용하는 명시적 예외 (§16).

    Growth Middleware(run_task) 강제 통과: prehook(order_submit) 은 account_id 귀속만 게이트한다
    (모드/health/idempotency/live-lock/시장가 정책/매수여력은 본문 _submit_order_impl 이 SSOT 로 유지).
    prehook block(account_id 누락 등) 이면 본문 미실행 + 기존 aborted shape 반환."""
    account_index = getattr(account, "id", None)

    # 0) live 하드락 — Growth Middleware 진입 *전* 에 재검증해 예외를 그대로 전파(무승인 실주문 차단).
    #    run_task 가 본문 예외를 흡수하므로, live lock 은 미들웨어 밖에서 hard-fail 로 유지(§15·Top위험#1).
    #    mock/paper 경로는 무영향. 본문(_submit_order_impl)도 동일 검증을 중복 보존(방어 심층).
    if getattr(broker, "mode", None) == "live":
        from .factory import _require_live_confirm
        _require_live_confirm()

    def _impl(_inp, _ctx):
        return _submit_order_impl(
            broker, account, req, available_cash_krw=available_cash_krw,
            risk_passed=risk_passed, urgent_sell=urgent_sell, conn=conn,
        )

    out = growth_mw.run_task(
        "order_submit", "broker-chief", _impl, account_index=account_index,
        input={"ticker": req.instrument.ticker, "side": req.side, "qty": str(req.qty),
               "order_type": req.order_type, "mode": getattr(broker, "mode", None)},
        record_failure=True,
    )
    if out["blocked"]:
        return _result(False, "aborted", "; ".join(out["reasons"]) or "prehook gate=block")
    if not out["ok"]:
        # 본문 실행 예외 — 주문 제출 경로는 실패를 in_doubt 가 아닌 명시 오류로 보고(재전송 금지).
        return _result(False, "error", "; ".join(out.get("reasons") or ["내부 오류"]))
    return out["result"]


def _submit_order_impl(
    broker,
    account: Account,
    req: OrderRequest,
    *,
    available_cash_krw: float | None = None,
    risk_passed: bool | None = None,
    urgent_sell: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict:
    own = conn is None
    conn = conn or store_db.connect()
    try:
        # 0) live 하드락 재확인 (Top위험#1): factory 생성 시점뿐 아니라 *제출 시점*에도
        #    KIS_LIVE_CONFIRM 을 재검증. 환경/가드가 사후 변경돼도 무승인 실주문 차단.
        #    mock/paper 경로는 무영향.
        if getattr(broker, "mode", None) == "live":
            from .factory import _require_live_confirm
            _require_live_confirm()

        h = payload_hash(req)

        # 1) idempotency (원장 SSOT)
        row = conn.execute(
            "SELECT id, payload_hash, status, broker_order_id FROM orders WHERE client_order_id=?",
            (req.client_order_id,),
        ).fetchone()
        if row is not None:
            if row["payload_hash"] != h:
                audit.record("order_block_dup_payload", actor="broker-chief", entity_type="orders",
                             entity_id=row["id"], mode=broker.mode, level="WARNING",
                             payload={"client_order_id": req.client_order_id}, conn=conn)
                conn.commit()
                return _result(False, "rejected", "duplicate client_order_id with different payload (A4)")
            if row["status"] in ("submitted", "partial", "filled"):
                # 이미 전송됨 → 재전송 금지 (idempotent)
                return _result(True, row["status"], "idempotent: already submitted",
                               row["broker_order_id"], duplicate=True)

        # 2) 시장가 정책 (§16): 매수 시장가 영구 금지. 매도 시장가는 '긴급 매도'(urgent_sell)에만 예외.
        if req.order_type == "market":
            if req.side == "buy" or not urgent_sell:
                reason = ("시장가 매수 금지 — 진입은 항상 지정가 (§16 · 예측 진입)"
                          if req.side == "buy"
                          else "시장가 매도 금지 — 긴급 매도(urgent_sell) 명시일 때만 예외 (§16)")
                audit.record("order_block_market", actor="risk-chief", entity_type="orders", mode=broker.mode,
                             level="WARNING", payload={"ticker": req.instrument.ticker, "side": req.side,
                                                       "qty": str(req.qty)}, conn=conn)
                conn.commit()
                return _result(False, "aborted", reason)
            # 긴급 매도 시장가 — 명시적 예외. 강조 감사 기록.
            audit.record("order_urgent_market_sell", actor="risk-chief", entity_type="orders", mode=broker.mode,
                         level="CRITICAL", payload={"ticker": req.instrument.ticker, "qty": str(req.qty)}, conn=conn)
            conn.commit()

        # 3) 모드 일치 (paper 의도인데 live adapter 등 — 사고 방지)
        if account.mode != broker.mode:
            audit.record("order_block_mode_mismatch", actor="risk-chief", entity_type="orders",
                         mode=broker.mode, level="WARNING",
                         payload={"account_mode": account.mode, "broker_mode": broker.mode,
                                  "ticker": req.instrument.ticker}, conn=conn)
            conn.commit()
            return _result(False, "aborted", f"mode mismatch: account={account.mode} broker={broker.mode}")

        # 3) broker health (API 장애 시 주문 중단, A3)
        if not broker.is_healthy:
            return _result(False, "aborted", "broker unhealthy (A3)")

        # 4) 리스크 게이트 (호출측이 gate.py 로 계산해 전달)
        if risk_passed is False:
            audit.record("risk_block", actor="risk-chief", entity_type="orders", mode=broker.mode,
                         level="WARNING", payload={"ticker": req.instrument.ticker, "side": req.side,
                                                   "qty": str(req.qty)}, conn=conn)
            conn.commit()
            return _result(False, "aborted", "risk gate failed")

        # 5) 매수여력 (Alpaca buying_power 차용)
        if available_cash_krw is not None and req.side == "buy" and req.limit_price:
            notional = float(req.qty) * float(req.limit_price)
            if notional > float(available_cash_krw):
                audit.record("order_block_buying_power", actor="risk-chief", entity_type="orders",
                             mode=broker.mode, level="WARNING",
                             payload={"notional": notional, "cash": float(available_cash_krw)}, conn=conn)
                conn.commit()
                return _result(False, "aborted",
                               f"insufficient_buying_power: notional={notional} cash={available_cash_krw}")

        # 원장에 submitting 기록 (없으면 생성)
        conn.execute(
            "INSERT OR IGNORE INTO orders(client_order_id, payload_hash, account_id, mode, ticker, side, qty, "
            "order_type, limit_price, status) VALUES (?,?,?,?,?,?,?,?,?, 'submitting')",
            (req.client_order_id, h, account.id, broker.mode, req.instrument.ticker, req.side,
             float(req.qty), req.order_type, float(req.limit_price) if req.limit_price is not None else None),
        )
        conn.commit()

        # 6) 전송 (예외/네트워크 불확실 → in_doubt, 재전송 금지)
        try:
            ack = broker.place_order(account, req)
        except Exception as exc:  # noqa: BLE001
            conn.execute("UPDATE orders SET status='in_doubt', reason=?, updated_at=datetime('now') "
                         "WHERE client_order_id=?", (f"in_doubt: {exc}", req.client_order_id))
            conn.commit()
            audit.record("order_in_doubt", actor="broker-chief", entity_type="orders", mode=broker.mode,
                         level="WARNING", payload={"client_order_id": req.client_order_id,
                                                   "error": str(exc)}, conn=conn)
            conn.commit()
            return _result(False, "in_doubt", f"in_doubt (재조회 필요): {exc}")

        if ack.accepted:
            conn.execute("UPDATE orders SET status='submitted', broker_order_id=?, reason=?, "
                         "updated_at=datetime('now') WHERE client_order_id=?",
                         (ack.broker_order_id, ack.reason, req.client_order_id))
            conn.commit()
            audit.record("order_submit", actor="broker-chief", entity_type="orders", mode=broker.mode,
                         payload={"client_order_id": req.client_order_id, "ticker": req.instrument.ticker,
                                  "side": req.side, "qty": str(req.qty),
                                  "broker_order_id": ack.broker_order_id}, conn=conn)
            conn.commit()
            return _result(True, "submitted", ack.reason, ack.broker_order_id)

        # 응답 불확실 표현 감지 → in_doubt, 그 외 rejected
        reason = ack.reason or ""
        in_doubt = any(k in reason for k in ("in_doubt", "전송 오류", "재조회"))
        status = "in_doubt" if in_doubt else "rejected"
        conn.execute("UPDATE orders SET status=?, reason=?, updated_at=datetime('now') "
                     "WHERE client_order_id=?", (status, reason, req.client_order_id))
        conn.commit()
        audit.record(f"order_{status}", actor="broker-chief", entity_type="orders", mode=broker.mode,
                     level="WARNING", payload={"client_order_id": req.client_order_id, "reason": reason}, conn=conn)
        conn.commit()
        return _result(False, status, reason)
    finally:
        if own:
            conn.close()


def list_orders(status: str | None = None, conn: sqlite3.Connection | None = None) -> list[dict]:
    own = conn is None
    conn = conn or store_db.connect()
    try:
        if status:
            rows = conn.execute("SELECT * FROM orders WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()
