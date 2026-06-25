"""Track C — PG 어댑터 테스트.

설계:
  - 기본(SQLite) CI 에서는 **항상 green** 이어야 한다.
    → live PG 가 필요한 테스트는 DB_BACKEND!=postgres 이거나 psycopg2 미설치면 skip.
  - backend 선택자 단위테스트는 PG 없이도 돈다 (env monkeypatch).
  - live round-trip 은 DB_BACKEND=postgres + DATABASE_URL 있을 때만 (portfolio_app 권한).

자격증명/DATABASE_URL 은 어떤 출력에도 노출하지 않는다.
"""
from __future__ import annotations

import os

import pytest

from main_mission.portfolio_os.store import backend


# ----------------------------------------------------------------------
# 1) backend 선택자 — PG 불요 (항상 실행, SQLite 기본 보호 검증)
# ----------------------------------------------------------------------

def test_default_backend_is_sqlite(monkeypatch):
    # 코드-레벨 기본값 보호: DB_BACKEND 가 *전혀* 없으면(.env override 도 무시) sqlite.
    # Track C: 운영 .env 가 postgres 로 전환되어도, 환경변수 미주입 시 코드 기본은 sqlite 여야 한다.
    monkeypatch.delenv("DB_BACKEND", raising=False)
    monkeypatch.setattr(backend, "_load_env", lambda: None)  # .env 비의존 — 순수 코드 기본 검증
    assert backend.current_backend() == "sqlite"
    assert backend.is_sqlite() is True
    assert backend.is_postgres() is False


def test_backend_postgres_opt_in(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "postgres")
    assert backend.current_backend() == "postgres"
    assert backend.is_postgres() is True
    monkeypatch.setenv("DB_BACKEND", "postgresql")
    assert backend.is_postgres() is True


def test_unknown_backend_falls_back_to_sqlite(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "oracle")
    # 알 수 없는 값은 안전하게 sqlite (silent PG 활성화 금지).
    assert backend.current_backend() == "sqlite"


def test_require_database_url_raises_without_secret(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    with pytest.raises(RuntimeError) as exc:
        backend.require_database_url()
    msg = str(exc.value)
    assert "DATABASE_URL 미설정" in msg
    # 비밀(URL)이 에러 메시지에 새지 않아야 함.
    assert "://" not in msg
    assert "@" not in msg


def test_dual_truth_guard():
    assert backend.assert_single_backend({"sqlite"}) == "sqlite"
    assert backend.assert_single_backend({"postgres"}) == "postgres"
    with pytest.raises(backend.DualTruthError):
        backend.assert_single_backend({"sqlite", "postgres"})
    with pytest.raises(backend.DualTruthError):
        backend.assert_single_backend(set())


# ----------------------------------------------------------------------
# 2) Live PG round-trip — env 가드 (PG 없으면 skip)
# ----------------------------------------------------------------------

def _live_pg_available() -> bool:
    # backend.is_postgres() 가 .env 를 로드하므로 DATABASE_URL 도 그 후 채워진다.
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
def test_pg_account_snapshot_roundtrip():
    """portfolio_app 으로 account_snapshot write→read 왕복 + account_id 격리.

    운영 truth 는 append-only(portfolio_app 에 DELETE 권한 없음)이므로,
    테스트는 **수동 트랜잭션 + rollback** 으로 실데이터를 남기지 않는다.
    pg.connect() 헬퍼 자체는 commit 정책이라 여기선 raw 연결을 쓴다.
    """
    import psycopg2

    from main_mission.portfolio_os.store import pg
    from main_mission.portfolio_os.store.backend import require_database_url

    conn = psycopg2.connect(require_database_url(), options="-c search_path=portfolio")
    try:
        # search_path 강제 + portfolio_app 권한 확인
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, current_schema")
            user, schema = cur.fetchone()
        assert schema == "portfolio"
        assert user == "portfolio_app"

        # 격리 검증용 두 계좌 (테스트 전용 높은 인덱스로 운영 데이터 회피)
        idx_a, idx_b = 99001, 99002
        aid_a = pg.upsert_account(
            conn, account_index=idx_a, alias="pytest-A", mode="paper",
            account_no_masked="99******", has_credentials=False,
        )
        aid_b = pg.upsert_account(
            conn, account_index=idx_b, alias="pytest-B", mode="paper",
            account_no_masked="99******", has_credentials=False,
        )
        assert aid_a != aid_b

        snap_a = pg.insert_account_snapshot(
            conn, account_id=aid_a, cash_krw="1000000.00", total_value_krw="1500000.00",
            holdings_count=1, source="pytest",
        )
        n_pos = pg.insert_position_snapshots(
            conn, account_id=aid_a, account_snapshot_id=snap_a,
            holdings=[{"ticker": "005930", "qty": 10, "avgPrice": "70000.00",
                       "marketValue": "750000.00", "name": "삼성전자"}],
        )
        assert n_pos == 1
        # B 계좌에도 스냅샷 — 격리 확인
        pg.insert_account_snapshot(
            conn, account_id=aid_b, cash_krw="500000.00", total_value_krw="500000.00",
            holdings_count=0, source="pytest",
        )

        # read back: A 의 최신 스냅샷이 A 의 값
        got = pg.fetch_account_snapshot_latest(conn, aid_a)
        assert got is not None
        assert got["account_id"] == aid_a
        assert str(got["cash_krw"]) == "1000000.00"

        # account_id 격리: B 의 최신은 B 값 (A 와 안 섞임)
        got_b = pg.fetch_account_snapshot_latest(conn, aid_b)
        assert got_b["account_id"] == aid_b
        assert str(got_b["cash_krw"]) == "500000.00"
    finally:
        # 실데이터 미오염 — 전부 롤백 (append-only 운영 truth 보호).
        conn.rollback()
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
