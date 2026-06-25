"""백엔드 동기화 작업 — KIS 에서 데이터를 가져와 **SQLite(운영 truth)에 저장**.

  python -m main_mission.portfolio_os.broker.sync_job --account 1   # 계좌1 메타+잔고 동기화
  python -m main_mission.portfolio_os.broker.sync_job --all          # .env 의 모든 계좌

웹은 이 job 을 trigger 만 하고, 화면은 DB 에 저장된 결과를 조회한다.
자격증명/토큰은 DB 에 저장하지 않는다 (.env 전용). 주문 없음(읽기 전용 수집).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from ..store import backend as store_backend
from ..store import db as store_db
from ..growth import middleware as growth_mw
from .account_status import fetch
from .kis_client import _load_env, mask


def _account_broker(n: int) -> str:
    """계좌 n 의 broker 결정: .env KIS_ACCOUNT_{n}_BROKER 우선, 없으면
    KIWOOM 자격증명 존재 시 kiwoom, 그 외 kis (멀티 브로커 분기)."""
    b = os.getenv(f"KIS_ACCOUNT_{n}_BROKER", "").strip().lower()
    if b:
        return b
    if os.getenv(f"KIWOOM_ACCOUNT_{n}_APP_KEY", "").strip():
        return "kiwoom"
    return "kis"


def fetch_account(n: int) -> dict:
    """broker 에 맞는 read-only 잔고 fetch 디스패치 (KIS 와 동일한 결과 dict 구조)."""
    if _account_broker(n) == "kiwoom":
        return _fetch_kiwoom(n)
    return fetch(n)  # 기존 KIS 경로 (무변경)


def _fetch_kiwoom(n: int) -> dict:
    """키움 read-only 잔고 fetch — account_status.fetch(KIS) 와 동일한 표준 결과 구조.

    토큰 → 예수금 → 보유종목. 비밀값(키/토큰) 절대 출력하지 않음. 주문 없음(읽기 전용)."""
    from .kiwoom_adapter import KiwoomRestAdapter, KiwoomNotConfigured

    pre = f"KIWOOM_ACCOUNT_{n}_"
    mode = (os.getenv(pre + "MODE") or os.getenv("KIS_MODE", "paper")).strip().lower()
    if mode not in ("paper", "live"):
        return {"ok": False, "error": f"키움 계좌 모드가 '{mode}' 입니다. paper|live 로 연결하세요."}

    adapter = KiwoomRestAdapter(account_index=n, mode=mode)
    try:
        adapter._need()  # 키 미설정이면 명확히 차단(비밀 미노출)
    except KiwoomNotConfigured as e:
        return {"ok": False, "stage": "credentials", "error": str(e)}

    try:
        adapter.ensure_token()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stage": "token", "error": str(e)}

    try:
        lines = adapter.get_balance()
        cash = adapter.get_cash_krw()
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pg_write_balance_ok(n: int, result: dict, started: str) -> None:
    """전환기 dual-write: SQLite 외에 **추가로** PG(운영-truth)에 동기화 성공 결과 기록.

    DB_BACKEND=postgres 일 때만 호출. 실패는 sync 를 중단시키지 않는다.
    자격증명/DATABASE_URL 은 절대 로그에 남기지 않는다.
    """
    from ..store import pg  # 지연 import (psycopg2 미설치 sqlite 환경 보호)

    with pg.connect() as conn:
        pre = f"KIS_ACCOUNT_{n}_"
        alias = os.getenv(pre + "ALIAS") or f"계좌 {n}"
        mode = (os.getenv(pre + "MODE") or os.getenv("KIS_MODE", "paper")).strip().lower()
        acct_no = os.getenv(pre + "ACCOUNT_NO", "").strip()
        masked = (acct_no[:2] + "******") if acct_no else None
        has_cred = bool(
            os.getenv(pre + "APP_KEY", "").strip() and os.getenv(pre + "APP_SECRET", "").strip()
        )
        account_id = pg.upsert_account(
            conn,
            account_index=n,
            alias=alias,
            mode=mode,
            account_no_masked=masked,
            has_credentials=has_cred,
            token_status="ok",
            sync_status="ok",
            last_error=None,
            last_synced_at=_now(),
        )
        snap_id = pg.insert_account_snapshot(
            conn,
            account_id=account_id,
            cash_krw=result["cashKrw"],
            total_value_krw=result["totalValueKrw"],
            holdings_count=len(result["holdings"]),
            source=f"kis_{result['mode']}",
            is_stale=False,
            captured_at=_now(),
        )
        pg.insert_position_snapshots(
            conn,
            account_id=account_id,
            account_snapshot_id=snap_id,
            holdings=result["holdings"],
            captured_at=_now(),
        )
        pg.insert_sync_event(
            conn,
            account_id=account_id,
            kind="balance",
            status="ok",
            stage="balance",
            started_at=started,
            finished_at=_now(),
        )


def _pg_write_balance_error(n: int, stage, err, started: str) -> None:
    """전환기 dual-write: 동기화 오류 상태를 PG 에도 반영 (실패해도 sync 미중단)."""
    from ..store import pg

    with pg.connect() as conn:
        account_id = pg.account_id_for(conn, n)
        if account_id is not None:
            pg.update_account_status(
                conn,
                account_index=n,
                sync_status="error",
                token_status="ok" if stage != "token" else "error",
                last_error=err,
            )
        pg.insert_sync_event(
            conn,
            account_id=account_id,
            kind="balance",
            status="error",
            stage=stage,
            error=err,
            started_at=started,
            finished_at=_now(),
        )


def discover_indices() -> list[int]:
    """KIS 또는 키움 자격증명이 있는 계좌 index 수집 (멀티 브로커)."""
    return [
        n for n in range(1, 51)
        if os.getenv(f"KIS_ACCOUNT_{n}_APP_KEY", "").strip()
        or os.getenv(f"KIWOOM_ACCOUNT_{n}_APP_KEY", "").strip()
    ]


def upsert_account_meta(conn, n: int) -> None:
    # broker 별 .env prefix 선택 (KIS 코드 무변경 — kiwoom 은 KIWOOM_* 사용)
    pre = f"KIWOOM_ACCOUNT_{n}_" if _account_broker(n) == "kiwoom" else f"KIS_ACCOUNT_{n}_"
    alias = os.getenv(pre + "ALIAS") or os.getenv(f"KIS_ACCOUNT_{n}_ALIAS") or f"계좌 {n}"
    mode = (os.getenv(pre + "MODE") or os.getenv("KIS_MODE", "paper")).strip().lower()
    acct_no = os.getenv(pre + "ACCOUNT_NO", "").strip()
    masked = (acct_no[:2] + "******") if acct_no else None
    has_cred = 1 if (os.getenv(pre + "APP_KEY", "").strip() and os.getenv(pre + "APP_SECRET", "").strip()) else 0
    conn.execute(
        "INSERT INTO accounts(account_index, alias, mode, account_no_masked, has_credentials, sync_status, updated_at) "
        "VALUES(?,?,?,?,?, COALESCE((SELECT sync_status FROM accounts WHERE account_index=?), 'never'), ?) "
        "ON CONFLICT(account_index) DO UPDATE SET alias=excluded.alias, mode=excluded.mode, "
        "account_no_masked=excluded.account_no_masked, has_credentials=excluded.has_credentials, updated_at=excluded.updated_at",
        (n, alias, mode, masked, has_cred, n, _now()),
    )
    conn.commit()


def sync_balance(n: int, conn) -> dict:
    """계좌 n 동기화. Growth Middleware(run_task) 강제 통과.

    prehook(broker_sync) 는 account_id(n) 귀속만 게이트한다(읽기 전용 수집 — 신선 스냅샷을
    *만드는* 작업이므로 fresh_snapshot 요구 금지). 동기화 자체의 성공/실패는 본문이 SSOT.
    fetch 실패(stage/error)는 본문이 정직하게 ok:False 로 반환하며, run_task validations 로도 기록."""
    def _impl(_inp, _ctx):
        res = _sync_balance_impl(n, conn)
        if not res.get("ok"):
            # 동기화 실패도 자산: run_task 가 task_failure_patterns 로 기록하게 validation 실패로 표기.
            return {"result": res, "success": False,
                    "validations": [{"name": "balance_sync", "ok": False,
                                     "detail": f"stage={res.get('stage')} error={res.get('error')}"}]}
        return {"result": res}

    out = growth_mw.run_task("broker_sync", "broker-chief", _impl, account_index=n,
                             input={"account_index": n}, record_failure=True)
    if out["blocked"]:
        return {"account_index": n, "ok": False, "stage": "prehook",
                "error": "; ".join(out["reasons"]) or "prehook gate=block"}
    if out["result"] is None:
        return {"account_index": n, "ok": False, "stage": "internal",
                "error": "; ".join(out.get("reasons") or ["내부 오류"])}
    return out["result"]


def _sync_balance_impl(n: int, conn) -> dict:
    started = _now()
    upsert_account_meta(conn, n)
    result = fetch_account(n)  # 읽기 전용: 토큰 + 잔고 (broker 별 디스패치)

    if not result.get("ok"):
        stage = result.get("stage")
        err = result.get("error")
        token_ok = "ok" if result.get("tokenOk") else "error"
        conn.execute(
            "UPDATE accounts SET sync_status='error', token_status=?, last_error=?, updated_at=? WHERE account_index=?",
            (token_ok if stage != "token" else "error", err, _now(), n),
        )
        conn.execute(
            "INSERT INTO sync_events(account_index, kind, status, stage, error, started_at, finished_at) "
            "VALUES(?, 'balance', 'error', ?, ?, ?, ?)",
            (n, stage, err, started, _now()),
        )
        conn.commit()
        # 전환기 dual-write (PG 추가 기록). 기본 sqlite 면 no-op. 실패해도 sync 미중단.
        if store_backend.is_postgres():
            try:
                _pg_write_balance_error(n, stage, err, started)
            except Exception as exc:  # noqa: BLE001 — 자격증명 미노출 로깅
                sys.stderr.write(f"[pg-dualwrite] account={n} balance-error 기록 실패: {type(exc).__name__}\n")
        return {"account_index": n, "ok": False, "stage": stage, "error": err}

    # 성공 → 스냅샷 + 보유종목 저장
    cur = conn.execute(
        "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, source, is_stale, captured_at) "
        "VALUES(?,?,?,?,?,0,?)",
        (n, result["cashKrw"], result["totalValueKrw"], len(result["holdings"]),
         f"{_account_broker(n)}_{result['mode']}", _now()),
    )
    snap_id = cur.lastrowid
    for h in result["holdings"]:
        conn.execute(
            "INSERT INTO holdings(snapshot_id, account_index, ticker, qty, avg_price, market_value, currency, captured_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (snap_id, n, h["ticker"], h["qty"], h["avgPrice"], h["marketValue"], "KRW", _now()),
        )
    conn.execute(
        "UPDATE accounts SET sync_status='ok', token_status='ok', last_error=NULL, last_synced_at=?, updated_at=? "
        "WHERE account_index=?",
        (_now(), _now(), n),
    )
    conn.execute(
        "INSERT INTO sync_events(account_index, kind, status, stage, started_at, finished_at) "
        "VALUES(?, 'balance', 'ok', 'balance', ?, ?)",
        (n, started, _now()),
    )
    conn.commit()
    # 전환기 dual-write (PG 추가 기록). 기본 sqlite 면 no-op. 실패해도 sync 미중단(데이터 손실 0).
    if store_backend.is_postgres():
        try:
            _pg_write_balance_ok(n, result, started)
        except Exception as exc:  # noqa: BLE001 — 자격증명/URL 미노출 로깅
            sys.stderr.write(f"[pg-dualwrite] account={n} balance-ok 기록 실패: {type(exc).__name__}\n")
    return {"account_index": n, "ok": True, "snapshot_id": snap_id, "cashKrw": result["cashKrw"],
            "holdings": len(result["holdings"])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    _load_env()
    conn = store_db.connect()
    try:
        if args.all:
            indices = discover_indices()
            results = [sync_balance(n, conn) for n in indices]
            out = {"ok": all(r["ok"] for r in results), "synced": results}
        elif args.account:
            out = sync_balance(args.account, conn)
        else:
            # 메타만 업서트
            for n in discover_indices():
                upsert_account_meta(conn, n)
            out = {"ok": True, "accounts": discover_indices()}
    finally:
        conn.close()
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
