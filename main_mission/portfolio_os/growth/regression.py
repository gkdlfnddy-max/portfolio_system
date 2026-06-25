"""실패 패턴 → regression test 자동 승격 (N#18).

원칙: 같은 실패가 반복되면(같은 task_type + detail) 그 실패를 다시는 통과시키지
않도록 `task_regression_tests` 에 active regression 으로 못박는다(append-only, UNIQUE).

흐름:
  middleware._record_failure 가 task_failure_patterns 에 실패를 적재한다(행 단위).
  promote_failures() 가 (task_type, detail) 별로 행 수(+occurrences)를 세어
  min_occurrences 이상이면 task_regression_tests 에 INSERT(UNIQUE 충돌 무시)하고
  원 failure 행들을 promoted_to_regression=1 로 표시한다.

  python -m main_mission.portfolio_os.growth --promote-regression
"""
from __future__ import annotations

from .. import lessons as _lessons  # noqa: F401 (CLI 재사용 경로 일관성)
from ..store import db as store_db


def _title_for(task_type: str, detail: str) -> str:
    """regression title — UNIQUE(task_type, title) 키. detail 을 안정적으로 요약."""
    d = (detail or "").strip().replace("\n", " ")
    if len(d) > 120:
        d = d[:117] + "..."
    return f"regression: {d}" if d else "regression: (no detail)"


def promote_failures(min_occurrences: int = 2, *, conn=None) -> dict:
    """반복 실패(같은 task_type+detail 이 min_occurrences 회 이상)를 regression 으로 승격.

    - 미승격(promoted_to_regression=0) failure 만 집계.
    - task_regression_tests 에 INSERT (UNIQUE(task_type,title) 충돌 시 무시 = 이미 승격됨).
    - 집계에 들어간 원 failure 행을 promoted_to_regression=1 로 표시.
    반환: {ok, promoted_count, promoted:[{task_type, title, given_input, expect, occurrences}]}
    """
    own = conn is None
    conn = conn or store_db.connect()
    promoted: list[dict] = []
    try:
        # (task_type, detail) 그룹: 행 수와 occurrences 합 모두 고려(둘 중 큰 값으로 반복성 판단).
        groups = conn.execute(
            "SELECT task_type, IFNULL(detail,'') AS detail, "
            "COUNT(*) AS row_count, SUM(IFNULL(occurrences,1)) AS occ_sum, "
            "MIN(id) AS first_id "
            "FROM task_failure_patterns "
            "WHERE IFNULL(promoted_to_regression,0)=0 "
            "GROUP BY task_type, IFNULL(detail,'')"
        ).fetchall()
        for g in groups:
            occurrences = max(int(g["row_count"]), int(g["occ_sum"] or 0))
            if occurrences < min_occurrences:
                continue
            task_type = g["task_type"]
            detail = g["detail"]
            title = _title_for(task_type, detail)
            given_input = detail            # 재현 입력 = 실패를 부른 사유/맥락
            expect = f"must_not_repeat: {detail}" if detail else "must_not_repeat"
            cur = conn.execute(
                "INSERT OR IGNORE INTO task_regression_tests "
                "(task_type, title, given_input, expect, source_failure_id, status, created_at) "
                "VALUES(?,?,?,?,?, 'active', datetime('now'))",
                (task_type, title, given_input, expect, g["first_id"]),
            )
            # 충돌로 INSERT 안 됐어도(rowcount 0) 원 실패는 처리완료로 표시(중복 누적 방지).
            conn.execute(
                "UPDATE task_failure_patterns SET promoted_to_regression=1 "
                "WHERE task_type=? AND IFNULL(detail,'')=? AND IFNULL(promoted_to_regression,0)=0",
                (task_type, detail),
            )
            if cur.rowcount and cur.rowcount > 0:
                promoted.append({
                    "task_type": task_type, "title": title,
                    "given_input": given_input, "expect": expect,
                    "occurrences": occurrences,
                })
        conn.commit()
        return {"ok": True, "promoted_count": len(promoted), "promoted": promoted,
                "min_occurrences": min_occurrences}
    finally:
        if own:
            conn.close()


def list_regressions(task_type: str | None = None, *, status: str | None = "active",
                     conn=None) -> list[dict]:
    """등록된 regression test 조회(기본 active 만)."""
    own = conn is None
    conn = conn or store_db.connect()
    try:
        sql = ("SELECT id, task_type, title, given_input, expect, source_failure_id, status, "
               "created_at FROM task_regression_tests WHERE 1=1")
        args: list = []
        if task_type:
            sql += " AND task_type=?"; args.append(task_type)
        if status:
            sql += " AND status=?"; args.append(status)
        sql += " ORDER BY id"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        if own:
            conn.close()
