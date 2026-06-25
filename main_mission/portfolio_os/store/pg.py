"""PostgreSQL 운영-truth 쓰기 어댑터 (opt-in, schema=portfolio).

Track C — SQLite 기본을 깨지 않고 PG 쓰기 경로를 추가한다.
  - search_path=portfolio 강제 (public 운영 테이블 0 정책).
  - 금액은 numeric (PG 컬럼이 numeric). account_id 격리: 모든 쓰기가 account_id 동반.
  - 자격증명/DATABASE_URL 은 어떤 로그/에러에도 노출하지 않는다.

PG 스키마 매핑 (SQLite → PG):
  accounts(account_index PK)        → portfolio.accounts(id PK, account_index UNIQUE)
  account_snapshots(account_index)  → portfolio.account_snapshots(account_id → accounts.id)
  holdings(snapshot_id,account_index) → portfolio.position_snapshots(account_id, account_snapshot_id)
  quotes(ticker)                    → portfolio.price_snapshots(ticker)
  sync_events                       → (PG 미존재) — 있으면 기록, 없으면 skip
"""
from __future__ import annotations

import contextlib
from typing import Any, Iterable, Iterator

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    _PSYCOPG_OK = True
except ImportError:  # pragma: no cover - 드라이버 미설치 환경
    psycopg2 = None  # type: ignore
    RealDictCursor = None  # type: ignore
    _PSYCOPG_OK = False

from .backend import require_database_url

SCHEMA = "portfolio"


def psycopg_available() -> bool:
    return _PSYCOPG_OK


@contextlib.contextmanager
def connect() -> Iterator["psycopg2.extensions.connection"]:
    """search_path=portfolio 가 강제된 PG 연결 (context-managed).

    정상 종료 시 commit, 예외 시 rollback. URL 은 노출하지 않는다.
    """
    if not _PSYCOPG_OK:
        raise RuntimeError("psycopg2 미설치 — PG 경로 사용 불가 (pip install psycopg2-binary)")
    url = require_database_url()  # 없으면 명확히 raise (URL 미노출)
    conn = psycopg2.connect(url, options=f"-c search_path={SCHEMA}")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- 내부 헬퍼 -----------------------------------------------------------

def _sync_events_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name='sync_events'",
            (SCHEMA,),
        )
        return cur.fetchone() is not None


# --- UPSERT / INSERT 함수 (멱등: 자연키 존재 시) ----------------------------

def upsert_account(
    conn,
    *,
    account_index: int,
    alias: str | None,
    mode: str,
    account_no_masked: str | None,
    has_credentials: bool,
    token_status: str | None = None,
    sync_status: str | None = None,
    last_error: str | None = None,
    last_synced_at: str | None = None,
) -> int:
    """accounts UPSERT (자연키 account_index). 반환: accounts.id (account_id).

    sync_status/token_status/last_error/last_synced_at 은 None 이면 기존값 보존(COALESCE).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts(account_index, alias, mode, account_no_masked,
                                 has_credentials, token_status, sync_status, last_error,
                                 last_synced_at, updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,COALESCE(%s,'never'),%s,%s, now())
            ON CONFLICT(account_index) DO UPDATE SET
                alias=EXCLUDED.alias,
                mode=EXCLUDED.mode,
                account_no_masked=EXCLUDED.account_no_masked,
                has_credentials=EXCLUDED.has_credentials,
                token_status=COALESCE(EXCLUDED.token_status, accounts.token_status),
                sync_status=COALESCE(EXCLUDED.sync_status, accounts.sync_status),
                last_error=EXCLUDED.last_error,
                last_synced_at=COALESCE(EXCLUDED.last_synced_at, accounts.last_synced_at),
                updated_at=now()
            RETURNING id
            """,
            (account_index, alias, mode, account_no_masked, has_credentials,
             token_status, sync_status, last_error, last_synced_at),
        )
        return int(cur.fetchone()[0])


def account_id_for(conn, account_index: int) -> int | None:
    """account_index → accounts.id (account_id) 조회. 없으면 None."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM accounts WHERE account_index=%s", (account_index,))
        row = cur.fetchone()
        return int(row[0]) if row else None


def update_account_status(
    conn,
    *,
    account_index: int,
    sync_status: str,
    token_status: str | None = None,
    last_error: str | None = None,
    last_synced_at: str | None = None,
) -> None:
    """동기화 결과 상태만 갱신 (계좌가 이미 존재해야 함)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE accounts SET
                sync_status=%s,
                token_status=COALESCE(%s, token_status),
                last_error=%s,
                last_synced_at=COALESCE(%s, last_synced_at),
                updated_at=now()
            WHERE account_index=%s
            """,
            (sync_status, token_status, last_error, last_synced_at, account_index),
        )


def insert_account_snapshot(
    conn,
    *,
    account_id: int,
    cash_krw: Any,
    total_value_krw: Any,
    holdings_count: int,
    source: str,
    fx_rate: Any = None,
    is_stale: bool = False,
    captured_at: str | None = None,
) -> int:
    """account_snapshots INSERT (account_id 격리). 반환: snapshot id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO account_snapshots(account_id, cash_krw, total_value_krw,
                holdings_count, fx_rate, source, is_stale, captured_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s, COALESCE(%s::timestamptz, now()))
            RETURNING id
            """,
            (account_id, cash_krw, total_value_krw, holdings_count, fx_rate,
             source, is_stale, captured_at),
        )
        return int(cur.fetchone()[0])


def insert_position_snapshots(
    conn,
    *,
    account_id: int,
    account_snapshot_id: int,
    holdings: Iterable[dict],
    captured_at: str | None = None,
) -> int:
    """position_snapshots 다건 INSERT (holdings → positions). 반환: 행 수.

    각 holding dict: ticker, qty, avgPrice|avg_price, marketValue|market_value, name?, currency?.
    account_id 격리 + account_snapshot_id FK.
    """
    n = 0
    with conn.cursor() as cur:
        for h in holdings:
            cur.execute(
                """
                INSERT INTO position_snapshots(account_id, account_snapshot_id, ticker,
                    name, qty, avg_price, market_value, currency, captured_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s, COALESCE(%s::timestamptz, now()))
                """,
                (
                    account_id,
                    account_snapshot_id,
                    h["ticker"],
                    h.get("name"),
                    h.get("qty"),
                    h.get("avgPrice", h.get("avg_price")),
                    h.get("marketValue", h.get("market_value")),
                    h.get("currency", "KRW"),
                    captured_at,
                ),
            )
            n += 1
    return n


def insert_price_snapshots(
    conn,
    *,
    quotes: Iterable[dict],
    captured_at: str | None = None,
) -> int:
    """price_snapshots 다건 INSERT (quotes). 각 quote: ticker, market?, price, source?."""
    n = 0
    with conn.cursor() as cur:
        for q in quotes:
            cur.execute(
                """
                INSERT INTO price_snapshots(ticker, market, price, source, captured_at)
                VALUES(%s,%s,%s,%s, COALESCE(%s::timestamptz, now()))
                """,
                (q["ticker"], q.get("market"), q.get("price"), q.get("source"), captured_at),
            )
            n += 1
    return n


def insert_sync_event(
    conn,
    *,
    account_id: int | None,
    kind: str,
    status: str,
    stage: str | None = None,
    error: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> bool:
    """sync_events INSERT — PG 에 테이블이 있으면 기록, 없으면 skip(반환 False).

    현재 portfolio 스키마에는 sync_events 가 미존재 → 안전하게 무시(중앙 머지가 결정).
    """
    if not _sync_events_exists(conn):
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_events(account_id, kind, status, stage, error,
                started_at, finished_at)
            VALUES(%s,%s,%s,%s,%s,
                   COALESCE(%s::timestamptz, now()), COALESCE(%s::timestamptz, now()))
            """,
            (account_id, kind, status, stage, error, started_at, finished_at),
        )
    return True


# --- 읽기 헬퍼 (테스트 round-trip / 검증용) --------------------------------

def fetch_account_snapshot_latest(conn, account_id: int) -> dict | None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM account_snapshots WHERE account_id=%s "
            "ORDER BY id DESC LIMIT 1",
            (account_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
