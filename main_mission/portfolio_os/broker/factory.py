"""Broker factory — KIS_MODE / 계좌별 mode 로 어떤 adapter 를 주입할지 결정.

  get_broker()                  → 전역 KIS_MODE (mock|paper|live), primary 자격증명
  get_broker(account_index=n)   → .env 의 KIS_ACCOUNT_{n}_* (계좌별 mode·자격증명, 멀티계좌)

live 는 KIS_LIVE_CONFIRM=I_UNDERSTAND 가 있어야만 생성 (안전 §6).
"""
from __future__ import annotations

import os

from .mock_adapter import MockAdapter
from .kis_adapter import KisPaperAdapter, KisLiveAdapter
from .kis_client import KisHttpClient, _load_env
from .kiwoom_adapter import KiwoomRestAdapter
from .kiwoom_client import KiwoomHttpClient


def _resolve_broker(account_index: int | None, broker: str | None) -> str:
    """계좌의 broker 결정: 명시값 > .env(KIS_ACCOUNT_{n}_BROKER) > 기본 'kis'. (멀티 브로커 분기)"""
    if broker:
        return broker.strip().lower()
    if account_index is not None:
        env_b = os.getenv(f"KIS_ACCOUNT_{account_index}_BROKER", "").strip().lower()
        if env_b:
            return env_b
    return "kis"


def _require_live_confirm() -> None:
    from .. import guards  # 지연 import — live_hard_lock predicate 의 단일 정의(guards.live_locked)
    if guards.live_locked():
        raise RuntimeError(
            "실전(live) broker 차단 — KIS_LIVE_CONFIRM=I_UNDERSTAND 가 없습니다. "
            "모의투자(paper)에서 충분히 검증 후 CEO 승인 체크리스트로만 전환하세요 (안전 §6)."
        )


def _dispatch(mode: str, client: KisHttpClient | None):
    if mode == "mock":
        return MockAdapter()
    if mode == "paper":
        return KisPaperAdapter(client)
    if mode == "live":
        _require_live_confirm()
        return KisLiveAdapter(client)
    raise ValueError(f"알 수 없는 mode: {mode!r} (mock|paper|live)")


def get_broker(mode: str | None = None, account_index: int | None = None, broker: str | None = None):
    _load_env()
    # 멀티 브로커 분기 — 키움 등은 독립 adapter (KIS 코드에 끼워넣지 않음).
    resolved_broker = _resolve_broker(account_index, broker)
    if resolved_broker == "kiwoom":
        acct_mode = (
            mode
            or (os.getenv(f"KIWOOM_ACCOUNT_{account_index}_MODE") if account_index else None)
            or (os.getenv(f"KIS_ACCOUNT_{account_index}_MODE") if account_index else None)
            or "paper"
        ).strip().lower()
        if acct_mode == "live":
            _require_live_confirm()  # 키움 live 도 동일 하드락 (안전 §6, 15)
        # 키 있을 때만 HTTP client 주입(독립 client). 키 없으면 adapter 가 lazy 차단.
        client = None
        pre = f"KIWOOM_ACCOUNT_{account_index}_" if account_index else "KIWOOM_"
        if os.getenv(pre + "APP_KEY", "").strip() and os.getenv(pre + "APP_SECRET", "").strip():
            client = KiwoomHttpClient(mode=acct_mode, account_index=account_index)
        return KiwoomRestAdapter(account_index=account_index, mode=acct_mode, client=client)
    if resolved_broker not in ("kis", "paper", "mock", ""):
        raise ValueError(f"미지원 broker: {resolved_broker!r} (kis|kiwoom)")

    # --- 기존 KIS 경로 (무변경) ---
    if account_index is not None:
        if not (1 <= account_index <= 50):
            raise ValueError(f"account_index 는 1~50: {account_index}")
        pre = f"KIS_ACCOUNT_{account_index}_"
        resolved = (mode or os.getenv(pre + "MODE") or os.getenv("KIS_MODE", "mock")).strip().lower()
        client = None if resolved == "mock" else KisHttpClient(mode=resolved, account_index=account_index)
        return _dispatch(resolved, client)

    resolved = (mode or os.getenv("KIS_MODE", "mock")).strip().lower()
    return _dispatch(resolved, None)
