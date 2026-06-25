"""regression 자동 승격 테스트 (N#18).

동일 (task_type, detail) 실패가 min_occurrences 회 이상 적재되면 promote_failures 가
task_regression_tests 에 1행을 만들고 원 failure 행을 promoted_to_regression=1 로 표시한다.
(임시 SQLite, Anthropic API 미사용)
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_regression.sqlite3")

# env(SQLITE_PATH)는 import 시점이 아니라 setup()에서 핀(pin)한다 — import 순서로 다른
# 테스트 모듈의 DB 경로를 가로채지 않도록(모듈 간 누수 방지).
from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os.growth import regression


def setup():
    os.environ["SQLITE_PATH"] = _TMP
    if os.path.exists(_TMP):
        os.remove(_TMP)
    store_db.init()


def _add_failure(task_type, detail, agent="theme-sector-advisor", account_index=None):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO task_failure_patterns(task_type, agent_name, account_index, detail, created_at) "
            "VALUES(?,?,?,?, datetime('now'))",
            (task_type, agent, account_index, detail),
        )
        conn.commit()
    finally:
        conn.close()


def _count(sql, args=()):
    conn = store_db.connect()
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def test_repeated_failure_promotes_to_regression():
    setup()
    detail = "theme=반도체 direction 미지정 → validation 실패"
    _add_failure("theme_direction", detail)
    _add_failure("theme_direction", detail)  # 2회 (>= min_occurrences)

    out = regression.promote_failures(min_occurrences=2)
    assert out["ok"]
    assert out["promoted_count"] == 1

    # task_regression_tests 에 정확히 1행.
    rows = regression.list_regressions(task_type="theme_direction")
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "active"
    assert detail in r["given_input"]
    assert detail in r["expect"]
    assert r["source_failure_id"] is not None

    # 원 failure 2행 모두 promoted_to_regression=1.
    assert _count("SELECT COUNT(*) FROM task_failure_patterns WHERE promoted_to_regression=1") == 2
    assert _count("SELECT COUNT(*) FROM task_failure_patterns WHERE promoted_to_regression=0") == 0


def test_single_failure_not_promoted():
    setup()
    _add_failure("daily_review", "1회성 사유")  # 1회만 → 승격 안 됨
    out = regression.promote_failures(min_occurrences=2)
    assert out["promoted_count"] == 0
    assert regression.list_regressions(task_type="daily_review") == []
    assert _count("SELECT COUNT(*) FROM task_failure_patterns WHERE promoted_to_regression=0") == 1


def test_idempotent_no_duplicate_on_rerun():
    setup()
    detail = "현금 하한 위반 반복"
    _add_failure("rebalance", detail)
    _add_failure("rebalance", detail)
    first = regression.promote_failures(min_occurrences=2)
    assert first["promoted_count"] == 1

    # 같은 detail 새 실패가 또 들어와도 UNIQUE(task_type,title) 로 중복 행 안 생김.
    _add_failure("rebalance", detail)
    _add_failure("rebalance", detail)
    regression.promote_failures(min_occurrences=2)
    assert _count("SELECT COUNT(*) FROM task_regression_tests WHERE task_type='rebalance'") == 1


def test_occurrences_column_counts_toward_threshold():
    setup()
    # 1행이지만 occurrences=3 이면 반복성 충족.
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO task_failure_patterns(task_type, detail, occurrences, created_at) "
            "VALUES('agg','누적 실패',3, datetime('now'))",
        )
        conn.commit()
    finally:
        conn.close()
    out = regression.promote_failures(min_occurrences=2)
    assert out["promoted_count"] == 1
    assert len(regression.list_regressions(task_type="agg")) == 1
