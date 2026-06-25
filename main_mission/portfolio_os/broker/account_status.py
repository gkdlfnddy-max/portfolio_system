"""계좌 상태(잔고) JSON 출력 — 웹이 호출하는 read-only 엔드포인트.

  python -m main_mission.portfolio_os.broker.account_status --account 1

토큰 발급 → 잔고/예수금 조회. 주문 없음(읽기 전용). stdout 에 JSON 1줄만 출력.
비밀값(키/시크릿/토큰)은 절대 출력하지 않음.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .kis_client import KisHttpClient, KisConfigError, _load_env
from .kis_adapter import KisPaperAdapter, KisLiveAdapter
from .port import Account


def fetch(account_index: int) -> dict:
    _load_env()
    pre = f"KIS_ACCOUNT_{account_index}_"
    mode = (os.getenv(pre + "MODE") or os.getenv("KIS_MODE", "paper")).strip().lower()
    if mode not in ("paper", "live"):
        return {"ok": False, "error": f"계좌 모드가 '{mode}' 입니다. 웹에서 paper|live 로 연결하세요."}

    try:
        client = KisHttpClient(mode, account_index=account_index)  # type: ignore
        client.require_credentials()
    except KisConfigError as e:
        return {"ok": False, "stage": "credentials", "error": str(e)}

    adapter = KisLiveAdapter(client) if mode == "live" else KisPaperAdapter(client)
    acct = Account(id=account_index, mode=mode)  # type: ignore

    try:
        client.ensure_token()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stage": "token", "error": str(e)}

    try:
        lines = adapter.get_balance(acct)
        cash = adapter.get_cash_krw(acct)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stage": "balance", "tokenOk": True, "error": str(e)}

    holdings = [
        {
            "ticker": ln.instrument.ticker,
            "qty": float(ln.qty),
            "avgPrice": float(ln.avg_price),
            "marketValue": float(ln.market_value),
        }
        for ln in lines
    ]
    total = float(cash) + sum(h["marketValue"] for h in holdings)
    return {
        "ok": True,
        "mode": mode,
        "tokenOk": True,
        "cashKrw": float(cash),
        "holdings": holdings,
        "totalValueKrw": total,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    args = ap.parse_args()
    try:
        out = fetch(args.account)
    except Exception as e:  # noqa: BLE001  — 항상 JSON 1줄 보장
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
