"""Track C — PG cutover (마이그레이션 + 단일백엔드 가드) 테스트.

설계 (기본 SQLite CI 보호):
  - psycopg2 미설치 또는 DB_BACKEND!=postgres 또는 DATABASE_URL 없음 → **전부 graceful skip**.
  - 단일백엔드 가드(assert_single_backend)는 PG 불요 — 항상 실행.
  - live PG 가용 시: 마이그레이션 멱등 / round-trip / account_id 격리 / public=0 검증.

자격증명/DATABASE_URL 은 어떤 출력에도 노출하지 않는다.
"""
from __future__ import annotations

import os

import pytest

from main_mission.portfolio_os.store import backend


# ----------------------------------------------------------------------
# Dual-truth 가드 — PG 불요 (항상 실행)
# ----------------------------------------------------------------------

def test_assert_single_backend_rejects_mixing():
    assert backend.assert_single_backend({"sqlite"}) == "sqlite"
    assert backend.assert_single_backend({"postgres"}) == "postgres"
    # PG + SQLite 혼합 = 진실원천 위반 → hard-block
    with pytest.raises(backend.DualTruthError):
        backend.assert_single_backend({"sqlite", "postgres"})
    with pytest.raises(backend.DualTruthError):
        backend.assert_single_backend(set())


# ----------------------------------------------------------------------
# Live PG 가드
# ----------------------------------------------------------------------

def _live_pg_available() -> bool:
    if not backend.is_postgres():
        return False
    try:
        from main_mission.portfolio_os.store import pg
    except Exception:
        return False
    if not pg.psycopg_available():
        return False
    return bool((os.getenv("DATABASE_URL") or "").strip())


live = pytest.mark.skipif(
    not _live_pg_available(),
    reason="live PG 미가용 (DB_BACKEND!=postgres 또는 psycopg2 미설치 또는 DATABASE_URL 없음)",
)


@live
def test_migration_is_idempotent():
    """마이그레이션을 두 번 실행해도 PG 운영행 수가 늘지 않는다(멱등)."""
    from main_mission.portfolio_os.store import migrate_to_pg, pg

    def _counts():
        with pg.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM account_snapshots")
                snaps = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM position_snapshots")
                pos = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM selected_allocations")
                sels = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM portfolio_decisions")
                decs = cur.fetchone()[0]
        return (snaps, pos, sels, decs)

    migrate_to_pg.migrate()  # 1차 (이미 적용됐을 수 있음 — 멱등이므로 무해)
    after_first = _counts()
    migrate_to_pg.migrate()  # 2차
    after_second = _counts()
    # 2차 실행은 신규 행을 만들지 않아야 한다.
    assert after_first == after_second, (
        f"멱등 위반: 1차={after_first} 2차={after_second}"
    )


@live
def test_fetch_latest_returns_migrated_data():
    """마이그레이션 후 PG 에 스냅샷이 있는 계좌를 pg.fetch_account_snapshot_latest 가 돌려준다.

    주의: 일부 다른 테스트가 SQLITE_PATH 를 임시 DB 로 바꿔둘 수 있으므로(세션 전역 부작용),
    검증 대상 계좌는 **PG(대상)에서** 고른다 — 단일 백엔드 read 로 자기일관성 확보.
    """
    from main_mission.portfolio_os.store import migrate_to_pg, pg

    migrate_to_pg.migrate()  # 멱등 — 현재 활성 SQLite 의 운영행을 PG 로 복사

    with pg.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT account_id FROM account_snapshots ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        if row is None:
            pytest.skip("PG 에 account_snapshot 없음 (마이그레이션할 원본 스냅샷 없음)")
        account_id = int(row[0])

        # account_id 로 최신 스냅샷 round-trip
        latest = pg.fetch_account_snapshot_latest(conn, account_id)
        assert latest is not None
        assert latest["account_id"] == account_id
        # 해당 account_id 가 실제 accounts 행에 연결됨(자연키 매핑 유효)
        with conn.cursor() as cur:
            cur.execute("SELECT account_index FROM accounts WHERE id=%s", (account_id,))
            acc_row = cur.fetchone()
        assert acc_row is not None
        assert pg.account_id_for(conn, int(acc_row[0])) == account_id


@live
def test_account_id_isolation():
    """서로 다른 account 의 스냅샷은 섞이지 않는다 (account_id 격리).

    append-only 운영 truth 보호: 수동 트랜잭션 + rollback 으로 실데이터 미오염.
    """
    import psycopg2

    from main_mission.portfolio_os.store import pg
    from main_mission.portfolio_os.store.backend import require_database_url

    conn = psycopg2.connect(require_database_url(), options="-c search_path=portfolio")
    try:
        idx_a, idx_b = 98001, 98002
        aid_a = pg.upsert_account(
            conn, account_index=idx_a, alias="cutover-A", mode="paper",
            account_no_masked="98******", has_credentials=False,
        )
        aid_b = pg.upsert_account(
            conn, account_index=idx_b, alias="cutover-B", mode="paper",
            account_no_masked="98******", has_credentials=False,
        )
        assert aid_a != aid_b
        pg.insert_account_snapshot(
            conn, account_id=aid_a, cash_krw="111.00", total_value_krw="111.00",
            holdings_count=0, source="cutover-test",
        )
        pg.insert_account_snapshot(
            conn, account_id=aid_b, cash_krw="222.00", total_value_krw="222.00",
            holdings_count=0, source="cutover-test",
        )
        got_a = pg.fetch_account_snapshot_latest(conn, aid_a)
        got_b = pg.fetch_account_snapshot_latest(conn, aid_b)
        assert str(got_a["cash_krw"]) == "111.00"
        assert str(got_b["cash_krw"]) == "222.00"
        # 격리: A 의 최신이 B 값과 안 섞임
        assert got_a["account_id"] == aid_a
        assert got_b["account_id"] == aid_b
        assert got_a["cash_krw"] != got_b["cash_krw"]
    finally:
        conn.rollback()  # 실데이터 미오염
        conn.close()


@live
def test_public_schema_has_zero_operational_tables():
    """운영 테이블은 schema=portfolio 전용. public 에 0개여야 한다."""
    from main_mission.portfolio_os.store import pg

    with pg.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
            )
            assert cur.fetchone()[0] == 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
