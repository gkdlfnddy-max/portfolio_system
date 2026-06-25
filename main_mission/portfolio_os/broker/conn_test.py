"""Broker 연결 테스트 (stage별) — KIS / 키움 공통.

  python -m main_mission.portfolio_os.broker.conn_test --account 1
  python -m main_mission.portfolio_os.broker.conn_test --account 1 --no-save   # 스냅샷 저장 생략

웹(계좌 상세 "연결 테스트")이 이 CLI 를 스폰하고, stage별 체크리스트를 표시한다.

정직 원칙(불변):
  - 키 입력 전에는 "준비 완료"라고만 하고 **실연동(가짜 성공)은 절대 만들지 않는다.**
  - 키 없으면 stage=credential 에서 정직하게 실패(ok:false, reason=NotConfigured) → 안전 차단.
    KIS 계좌에는 어떤 영향도 주지 않는다(broker 별 독립 어댑터).
  - 주문(place_order)은 호출하지 않는다(주문 2차 보류).
  - 비밀값(키/시크릿/토큰/평문 계좌번호)은 어떤 stage 결과에도 넣지 않는다.

stage 순서: credential → token → account(계좌번호 유효) → cash(예수금) → balance(잔고/보유종목) → quote(현재가)
각 stage 실패 시 원인을 분리: credential | token | account | tr | network | rate_limit.

성공 시(--no-save 아니면) sync_job 의 저장 경로를 재사용해 account_snapshots 에 기록 →
snapshot_saved:true (대시보드/계좌 화면이 동일 truth 를 반영).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import factory
from .kis_client import _load_env
from .port import Account, Instrument

# 테스트용 현재가 종목 (국내 대표) — 조회만, 주문 없음.
_PROBE_TICKER = "005930"
_PROBE_NAME = "삼성전자"

# stage 키(고정 — 웹 체크리스트가 라벨 매핑). 실패 원인(reason) 은 별도 필드.
STAGE_ORDER = ["credential", "token", "account", "cash", "balance", "quote"]

_STAGE_LABEL = {
    "credential": "자격증명",
    "token": "토큰 발급",
    "account": "계좌번호 유효",
    "cash": "예수금 조회",
    "balance": "잔고/보유종목",
    "quote": "현재가 조회",
}


def _classify(exc: Exception) -> str:
    """예외 → 실패 원인 분리(credential/token/account/tr/network/rate_limit)."""
    name = type(exc).__name__
    msg = str(exc).lower()
    if "notconfigured" in name.lower() or "configerror" in name.lower():
        return "credential"
    if "429" in msg or "rate" in msg or "초당" in msg or "한도" in msg:
        return "rate_limit"
    if "네트워크" in msg or "timeout" in msg or "timed out" in msg or "urlerror" in msg or "unhealthy" in msg:
        return "network"
    if "토큰" in msg or "token" in msg or "oauth" in msg or "401" in msg:
        return "token"
    if "계좌" in msg or "cano" in msg or "account_no" in msg:
        return "account"
    return "tr"


def _ok(stage: str) -> dict:
    return {"stage": stage, "label": _STAGE_LABEL[stage], "ok": True, "error": None, "reason": None}


def _fail(stage: str, error: str, reason: str) -> dict:
    # error 는 사람이 읽는 메시지(비밀 미포함), reason 은 분류 코드.
    return {"stage": stage, "label": _STAGE_LABEL[stage], "ok": False, "error": error, "reason": reason}


def _account_broker(account_index: int) -> str:
    """sync_job 과 동일한 broker 판별(멀티 브로커). KIS 코드 무변경."""
    b = os.getenv(f"KIS_ACCOUNT_{account_index}_BROKER", "").strip().lower()
    if b:
        return b
    if os.getenv(f"KIWOOM_ACCOUNT_{account_index}_APP_KEY", "").strip():
        return "kiwoom"
    return "kis"


def _test_mode(broker: str, account_index: int) -> str:
    """연결 테스트 모드 — .env MODE 가 live 면 live, 그 외(paper/mock/미설정)는 paper.
    mock 은 가짜이므로 연결 테스트에 쓰지 않는다(정직)."""
    pre = f"KIWOOM_ACCOUNT_{account_index}_" if broker == "kiwoom" else f"KIS_ACCOUNT_{account_index}_"
    m = (os.getenv(pre + "MODE") or os.getenv("KIS_MODE", "paper")).strip().lower()
    return "live" if m == "live" else "paper"


def _has_account_no(broker: str, account_index: int) -> bool:
    pre = f"KIWOOM_ACCOUNT_{account_index}_" if broker == "kiwoom" else f"KIS_ACCOUNT_{account_index}_"
    return bool(os.getenv(pre + "ACCOUNT_NO", "").strip())


def test_connection(account_index: int, save: bool = True) -> dict:
    """계좌 1건의 broker 연결을 stage별로 점검. 항상 dict 반환(예외 누출 없음)."""
    _load_env()
    broker = _account_broker(account_index)
    result: dict = {
        "account_index": account_index,
        "broker": broker,
        "mode": None,
        "stages": [],
        "ok": False,
        "snapshot_saved": False,
    }
    stages = result["stages"]

    # --- stage 1: credential (키 미설정이면 *adapter 만들기 전* 정직 실패 → KIS 무영향) ---
    # 키가 없으면 어댑터/토큰을 만들지 않는다(가짜 성공·live 하드락 메시지 혼동 방지).
    if not _credentials_present(broker, account_index):
        stages.append(_fail(
            "credential",
            f"{broker.upper()} 자격증명(.env APP_KEY/APP_SECRET) 미설정 — 키 입력 전 '준비 완료' 상태(실연동 아님).",
            "credential",
        ))
        return result  # 안전 차단 — 이후 stage 진행 안 함(가짜 성공 금지)

    # 연결 테스트는 *실연동*만 의미가 있다 → paper|live 로 강제(mock 은 가짜 성공이라 금지).
    # 계좌 .env MODE 가 mock/미설정이면 paper 로 점검.
    test_mode = _test_mode(broker, account_index)

    # --- adapter 확보 (factory — broker 별 분기) ---
    try:
        adapter = factory.get_broker(mode=test_mode, account_index=account_index, broker=broker)
    except Exception as exc:  # noqa: BLE001 — live 하드락 등도 정직하게 표면화
        stages.append(_fail("credential", str(exc), _classify(exc)))
        return result
    result["mode"] = getattr(adapter, "mode", None)
    stages.append(_ok("credential"))

    acct = Account(id=account_index, mode=result["mode"] or "paper")  # type: ignore[arg-type]

    # --- stage 2: token ---
    try:
        adapter.ensure_token()
        stages.append(_ok("token"))
    except Exception as exc:  # noqa: BLE001
        stages.append(_fail("token", str(exc), _classify(exc)))
        return result

    # --- stage 3: account (계좌번호 유효 — .env 에 ACCOUNT_NO 존재 확인) ---
    if _has_account_no(broker, account_index):
        stages.append(_ok("account"))
    else:
        stages.append(_fail(
            "account",
            f"{broker.upper()} 계좌번호(.env ACCOUNT_NO) 미설정 — 잔고/예수금 조회 불가.",
            "account",
        ))
        return result

    # --- stage 4: cash (예수금) ---
    try:
        adapter.get_cash_krw(acct)
        stages.append(_ok("cash"))
    except Exception as exc:  # noqa: BLE001
        stages.append(_fail("cash", str(exc), _classify(exc)))
        return result

    # --- stage 5: balance (잔고/보유종목) ---
    try:
        adapter.get_balance(acct)
        stages.append(_ok("balance"))
    except Exception as exc:  # noqa: BLE001
        stages.append(_fail("balance", str(exc), _classify(exc)))
        return result

    # --- stage 6: quote (현재가) ---
    try:
        adapter.get_quote(Instrument(ticker=_PROBE_TICKER, market="KRX", currency="KRW"))
        stages.append(_ok("quote"))
    except Exception as exc:  # noqa: BLE001
        stages.append(_fail("quote", str(exc), _classify(exc)))
        return result

    result["ok"] = all(s["ok"] for s in stages)

    # --- 성공 시 스냅샷 저장 (sync_job 저장 경로 재사용 — 동일 truth) ---
    if result["ok"] and save:
        try:
            result["snapshot_saved"] = _save_snapshot(account_index)
        except Exception as exc:  # noqa: BLE001 — 저장 실패는 연결 성공을 가리지 않음
            result["save_error"] = type(exc).__name__

    return result


def _credentials_present(broker: str, account_index: int) -> bool:
    pre = f"KIWOOM_ACCOUNT_{account_index}_" if broker == "kiwoom" else f"KIS_ACCOUNT_{account_index}_"
    return bool(os.getenv(pre + "APP_KEY", "").strip() and os.getenv(pre + "APP_SECRET", "").strip())


def _save_snapshot(account_index: int) -> bool:
    """sync_job 의 read-only 저장 경로를 재사용해 account_snapshots 에 스냅샷 기록.

    가짜 성공 금지: 실제 sync_balance 가 ok 일 때만 True. (저장도 read-only 수집)"""
    from . import sync_job
    from ..store import db as store_db

    conn = store_db.connect()
    try:
        out = sync_job.sync_balance(account_index, conn)
    finally:
        conn.close()
    return bool(out.get("ok"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Broker 연결 테스트 (stage별, KIS/키움 공통)")
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--no-save", action="store_true", help="성공해도 스냅샷 저장 생략")
    args = ap.parse_args()
    try:
        out = test_connection(args.account, save=not args.no_save)
    except Exception as exc:  # noqa: BLE001 — 항상 JSON 1줄 보장
        out = {"account_index": args.account, "ok": False, "stages": [],
               "error": f"내부 오류: {type(exc).__name__}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
