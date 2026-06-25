"""pytest 공통 설정 — 테스트 격리 (per-test 고유 DB, 근본 수정).

문제(기존 flakiness):
  - 30+ 테스트 모듈이 module-level 로 `os.environ["SQLITE_PATH"]=_TMP` 를 고정하고,
    일부는 `setup_function`/`setup` 에서 다시 재핀한다. 한 프로세스에서 수집·실행되면
    전역 SQLITE_PATH 가 모듈 간 오염되고( '다른 모듈 DB 가로채기' / no such table ),
    같은 /tmp 파일을 공유·재생성하며 churn → disk I/O error / readonly database.

근본 수정(회피 없음 — skip/xfail/sleep/순서고정 안 씀):
  - 각 테스트마다 **고유 sqlite 파일**(tmp_path_factory) 을 쓰도록 `store/db.db_path()` 자체를
    monkeypatch 한다. 모듈이 os.environ["SQLITE_PATH"] 를 어떻게 바꾸든, db_path() 가 항상
    그 테스트의 고유 경로를 돌려주므로 **모듈 재핀이 무력화**된다(순서·공유 오염 원천 제거).
  - 스키마는 db.connect() 가 경로 변경을 감지해 lazy 부트스트랩(_bootstrapped_path) 한다.
  - 테스트 종료 후 DB + WAL/SHM/journal 사이드카 정리.

PG 전용 테스트(test_pg_*)는 자체적으로 RUN_DB_TESTS + DATABASE_URL 를 확인해 동작/skip.
"""
import os

import pytest

# 수집 시점 — 실 운영 PostgreSQL mirror 차단.
os.environ["DB_BACKEND"] = "sqlite"


@pytest.fixture(autouse=True)
def _isolated_sqlite(tmp_path_factory, monkeypatch):
    db_path = tmp_path_factory.mktemp("posdb") / "test.sqlite3"

    from main_mission.portfolio_os.store import db as _db
    # db_path() 를 고유 경로로 고정 — 모듈의 os.environ["SQLITE_PATH"] 재핀을 무력화.
    monkeypatch.setattr(_db, "db_path", lambda: db_path)
    # 경로별 부트스트랩 캐시 리셋 → 첫 connect() 가 이 테스트 DB 에 스키마 보장.
    monkeypatch.setattr(_db, "_bootstrapped_path", None, raising=False)

    yield

    for _ext in ("", "-wal", "-shm", "-journal"):
        try:
            os.unlink(str(db_path) + _ext)
        except OSError:
            pass
