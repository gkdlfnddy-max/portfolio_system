"""Track C — SQLite 운영행 → PG(운영-truth) 일회성 멱등 마이그레이션.

목적:
  웹이 읽는 운영 데이터(계좌·잔고 스냅샷·보유종목·선택 자산배분·결정/리스크 핵심)를
  기존 SQLite 에서 PG(schema=portfolio)로 한 번 복사한다.
  이후 신규 데이터는 sync_job 의 dual-write 가 유지한다.

원칙:
  - **멱등**: 자연키 기준 존재 시 skip/upsert. 두 번 실행해도 행이 늘지 않는다.
      · accounts          → account_index UNIQUE (upsert)
      · account_snapshots → (account_id, captured_at) 존재 검사 후 insert
      · position_snapshots→ 해당 snapshot 에 행이 이미 있으면 skip (스냅샷 단위 멱등)
      · selected_allocations → (account_id, selected_at, status) 존재 검사 후 insert
      · portfolio_decisions  → (account_id, created_at) 존재 검사 후 insert
      · risk_checks          → decision_id 연결 시 (account_id, created_at) 존재 검사 후 insert
  - **단일 백엔드 읽기**: 원본은 SQLite, 대상은 PG. 한 read 가 두 백엔드를 섞지 않는다.
  - **append-only 운영 truth**: portfolio_app 은 DELETE 권한이 없으므로 삭제-재삽입 안 함.
  - **자격증명/DATABASE_URL 미노출**: 카운트만 출력. URL/비밀은 절대 print 하지 않는다.

CLI:
  python -m main_mission.portfolio_os.store.migrate_to_pg
"""
from __future__ import annotations

import json
import sys
from typing import Any

from . import db as store_db
from . import pg


# --- SQLite 읽기 헬퍼 (원본: 단일 백엔드) ----------------------------------

def _sqlite_accounts(sconn) -> list[dict]:
    rows = sconn.execute(
        "SELECT account_index, alias, mode, account_no_masked, has_credentials, "
        "token_status, sync_status, last_error, last_synced_at FROM accounts "
        "ORDER BY account_index"
    ).fetchall()
    return [dict(r) for r in rows]


def _sqlite_latest_snapshot(sconn, account_index: int) -> dict | None:
    r = sconn.execute(
        "SELECT id, cash_krw, total_value_krw, holdings_count, fx_rate, source, "
        "is_stale, captured_at FROM account_snapshots "
        "WHERE account_index=? ORDER BY id DESC LIMIT 1",
        (account_index,),
    ).fetchone()
    return dict(r) if r else None


def _sqlite_holdings(sconn, snapshot_id: int) -> list[dict]:
    rows = sconn.execute(
        "SELECT ticker, name, qty, avg_price, market_value, currency "
        "FROM holdings WHERE snapshot_id=? ORDER BY id",
        (snapshot_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _sqlite_active_selection(sconn, account_index: int) -> dict | None:
    r = sconn.execute(
        "SELECT id, proposal_id, variant, allocation, policy_version, account_snapshot_id, "
        "expected_drift_pct, expected_rebalance_total_krw, expected_rebalance_rounds, "
        "precheck_status, precheck_reasons, selected_by, user_override, diff, status, selected_at "
        "FROM allocation_selections WHERE account_index=? AND status='active' "
        "ORDER BY id DESC LIMIT 1",
        (account_index,),
    ).fetchone()
    return dict(r) if r else None


def _sqlite_latest_decision(sconn, account_index: int) -> dict | None:
    r = sconn.execute(
        "SELECT id, payload, created_at FROM decisions "
        "WHERE account_index=? ORDER BY id DESC LIMIT 1",
        (account_index,),
    ).fetchone()
    return dict(r) if r else None


# --- PG 멱등 존재 검사 + 삽입 (대상: 단일 백엔드) ----------------------------

def _pg_snapshot_exists(conn, account_id: int, captured_at: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM account_snapshots "
            "WHERE account_id=%s AND captured_at=%s::timestamptz "
            "ORDER BY id DESC LIMIT 1",
            (account_id, captured_at),
        )
        row = cur.fetchone()
        return int(row[0]) if row else None


def _pg_positions_count(conn, account_snapshot_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM position_snapshots WHERE account_snapshot_id=%s",
            (account_snapshot_id,),
        )
        return int(cur.fetchone()[0])


def _pg_selection_exists(conn, account_id: int, selected_at: str, status: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM selected_allocations "
            "WHERE account_id=%s AND selected_at=%s::timestamptz AND status=%s LIMIT 1",
            (account_id, selected_at, status),
        )
        return cur.fetchone() is not None


def _pg_decision_exists(conn, account_id: int, created_at: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM portfolio_decisions "
            "WHERE account_id=%s AND created_at=%s::timestamptz ORDER BY id DESC LIMIT 1",
            (account_id, created_at),
        )
        row = cur.fetchone()
        return int(row[0]) if row else None


def _pg_riskcheck_exists(conn, account_id: int, decision_id: int | None) -> bool:
    with conn.cursor() as cur:
        if decision_id is not None:
            cur.execute(
                "SELECT 1 FROM risk_checks WHERE account_id=%s AND decision_id=%s LIMIT 1",
                (account_id, decision_id),
            )
        else:
            return True  # decision 없으면 risk_check 도 안 만든다
        return cur.fetchone() is not None


def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s",
            (pg.SCHEMA, table),
        )
        return cur.fetchone() is not None


def _as_jsonb(raw: Any):
    """SQLite TEXT(JSON) → PG jsonb 입력값. None/빈값은 그대로, 파싱 실패는 빈 obj."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (dict, list)):
        return json.dumps(raw, ensure_ascii=False)
    # 이미 JSON 문자열이면 검증 후 그대로 전달
    try:
        json.loads(raw)
        return raw
    except (ValueError, TypeError):
        return json.dumps({"raw": str(raw)}, ensure_ascii=False)


# --- 마이그레이션 본체 -----------------------------------------------------

def migrate() -> dict[str, int]:
    """SQLite → PG 멱등 복사. 반환: 단계별 카운트 (자격증명/URL 미포함)."""
    counts = {
        "accounts": 0,
        "account_snapshots": 0,
        "position_snapshots": 0,
        "selected_allocations": 0,
        "portfolio_decisions": 0,
        "risk_checks": 0,
        "skipped_snapshots": 0,
        "skipped_selections": 0,
        "skipped_decisions": 0,
    }

    sconn = store_db.connect()
    try:
        accounts = _sqlite_accounts(sconn)
        with pg.connect() as conn:
            has_decisions = _table_exists(conn, "portfolio_decisions")
            has_risk = _table_exists(conn, "risk_checks")
            has_selections = _table_exists(conn, "selected_allocations")

            for acc in accounts:
                aidx = int(acc["account_index"])
                # 1) accounts — upsert (account_index 자연키, 멱등)
                account_id = pg.upsert_account(
                    conn,
                    account_index=aidx,
                    alias=acc.get("alias"),
                    mode=(acc.get("mode") or "paper"),
                    account_no_masked=acc.get("account_no_masked"),
                    has_credentials=bool(acc.get("has_credentials")),
                    token_status=acc.get("token_status"),
                    sync_status=acc.get("sync_status"),
                    last_error=acc.get("last_error"),
                    last_synced_at=acc.get("last_synced_at"),
                )
                counts["accounts"] += 1

                # 2) 최신 account_snapshot (+holdings → position_snapshots)
                snap = _sqlite_latest_snapshot(sconn, aidx)
                if snap is not None:
                    captured_at = snap["captured_at"]
                    existing = _pg_snapshot_exists(conn, account_id, captured_at)
                    if existing is None:
                        snap_id = pg.insert_account_snapshot(
                            conn,
                            account_id=account_id,
                            cash_krw=snap.get("cash_krw"),
                            total_value_krw=snap.get("total_value_krw"),
                            holdings_count=snap.get("holdings_count") or 0,
                            source=snap.get("source") or "sqlite_migrate",
                            fx_rate=snap.get("fx_rate"),
                            is_stale=bool(snap.get("is_stale")),
                            captured_at=captured_at,
                        )
                        counts["account_snapshots"] += 1
                    else:
                        snap_id = existing
                        counts["skipped_snapshots"] += 1

                    # holdings → position_snapshots (스냅샷 단위 멱등)
                    holdings = _sqlite_holdings(sconn, snap["id"])
                    if holdings and _pg_positions_count(conn, snap_id) == 0:
                        n = pg.insert_position_snapshots(
                            conn,
                            account_id=account_id,
                            account_snapshot_id=snap_id,
                            holdings=holdings,
                            captured_at=captured_at,
                        )
                        counts["position_snapshots"] += n

                # 3) 활성 selected_allocation (있으면)
                if has_selections:
                    sel = _sqlite_active_selection(sconn, aidx)
                    if sel is not None:
                        selected_at = sel["selected_at"]
                        status = sel.get("status") or "active"
                        if not _pg_selection_exists(conn, account_id, selected_at, status):
                            _insert_selection(conn, account_id, sel)
                            counts["selected_allocations"] += 1
                        else:
                            counts["skipped_selections"] += 1

                # 4) 최신 portfolio_decision + risk_check 핵심 (있으면)
                if has_decisions:
                    dec = _sqlite_latest_decision(sconn, aidx)
                    if dec is not None:
                        created_at = dec["created_at"]
                        existing_dec = _pg_decision_exists(conn, account_id, created_at)
                        if existing_dec is None:
                            decision_id, passed, drift, reasons = _insert_decision(
                                conn, account_id, dec
                            )
                            counts["portfolio_decisions"] += 1
                            if has_risk and not _pg_riskcheck_exists(conn, account_id, decision_id):
                                _insert_risk_check(
                                    conn, account_id, decision_id, passed, reasons
                                )
                                counts["risk_checks"] += 1
                        else:
                            counts["skipped_decisions"] += 1
    finally:
        sconn.close()

    return counts


def _insert_selection(conn, account_id: int, sel: dict) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO selected_allocations(account_id, proposal_id, variant, allocation_json,
                account_snapshot_id, expected_drift_pct, expected_rebalance_total_krw,
                expected_rebalance_rounds, precheck_status, precheck_reasons_json,
                selected_by, user_override, diff_json, status, selected_at)
            VALUES(%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s,
                   COALESCE(%s::timestamptz, now()))
            RETURNING id
            """,
            (
                account_id,
                sel.get("proposal_id"),
                sel.get("variant"),
                _as_jsonb(sel.get("allocation")),
                None,  # account_snapshot_id: SQLite id ≠ PG id, 안전하게 미연결
                sel.get("expected_drift_pct"),
                sel.get("expected_rebalance_total_krw"),
                sel.get("expected_rebalance_rounds"),
                sel.get("precheck_status"),
                _as_jsonb(sel.get("precheck_reasons")),
                sel.get("selected_by"),
                bool(sel.get("user_override")),
                _as_jsonb(sel.get("diff")),
                sel.get("status") or "active",
                sel.get("selected_at"),
            ),
        )
        return int(cur.fetchone()[0])


def _insert_decision(conn, account_id: int, dec: dict):
    """decisions.payload(JSON) → portfolio_decisions 핵심 필드.

    payload 안에 risk/drift 가 있으면 추출, 없으면 None.
    반환: (decision_id, passed, drift_pct, risk_reasons_json).
    """
    payload_raw = dec.get("payload")
    drift = None
    passed = None
    reasons = None
    try:
        payload = json.loads(payload_raw) if payload_raw else {}
        risk = payload.get("risk") if isinstance(payload, dict) else None
        if isinstance(risk, dict):
            passed = risk.get("passed")
            reasons = risk.get("reasons")
        if isinstance(payload, dict):
            drift = payload.get("drift_pct") or payload.get("drift")
    except (ValueError, TypeError):
        payload = {"raw": str(payload_raw)}

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO portfolio_decisions(account_id, selected_allocation_id,
                account_snapshot_id, payload_json, drift_pct, risk_reasons_json, passed, created_at)
            VALUES(%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s, COALESCE(%s::timestamptz, now()))
            RETURNING id
            """,
            (
                account_id,
                None,
                None,
                _as_jsonb(payload),
                drift,
                _as_jsonb(reasons),
                passed,
                dec.get("created_at"),
            ),
        )
        decision_id = int(cur.fetchone()[0])
    return decision_id, passed, drift, reasons


def _insert_risk_check(conn, account_id: int, decision_id: int, passed, reasons) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO risk_checks(account_id, decision_id, selected_allocation_id,
                account_snapshot_id, passed, risk_reasons_json)
            VALUES(%s,%s,%s,%s,%s,%s::jsonb)
            """,
            (
                account_id,
                decision_id,
                None,
                None,
                bool(passed) if passed is not None else False,
                _as_jsonb(reasons),
            ),
        )


def main() -> int:
    from . import backend as store_backend

    if not store_backend.is_postgres():
        sys.stderr.write(
            "DB_BACKEND 가 postgres 가 아닙니다 — 마이그레이션은 PG 대상 전용입니다. "
            "(DB_BACKEND=postgres 로 실행하세요)\n"
        )
        return 2
    if not pg.psycopg_available():
        sys.stderr.write("psycopg2 미설치 — PG 마이그레이션 불가.\n")
        return 2

    counts = migrate()
    # 카운트만 출력 (자격증명/URL 절대 미포함)
    sys.stdout.write(json.dumps({"ok": True, "migrated": counts}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
