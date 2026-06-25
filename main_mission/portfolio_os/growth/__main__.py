"""growth 운영 CLI.

  python -m main_mission.portfolio_os.growth --seed                 # agent_memory_scope 시드(멱등)
  python -m main_mission.portfolio_os.growth --status               # 스캐폴딩 현황
  python -m main_mission.portfolio_os.growth --decay                # outdated lesson decay/archive
  python -m main_mission.portfolio_os.growth --promote              # 반복 검증 candidate→lesson 승격
  python -m main_mission.portfolio_os.growth --promote-regression   # 반복 실패→regression 승격
  python -m main_mission.portfolio_os.growth --report               # agent/task별 성장 리포트
"""
from __future__ import annotations

import argparse
import json
import sys

from .. import lessons as lessons_mod
from ..store import db as store_db
from . import registry, regression


def status() -> dict:
    conn = store_db.connect()
    try:
        def count(t):
            return conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        scopes = conn.execute(
            "SELECT agent, COUNT(*) c FROM agent_memory_scope GROUP BY agent ORDER BY agent"
        ).fetchall()
        return {
            "ok": True,
            "tasks": count("tasks"),
            "agent_memory_scope": {r["agent"]: r["c"] for r in scopes},
            "task_memory_links": count("task_memory_links"),
            "feedback_memory": count("feedback_memory"),
            "lessons": count("lessons"),
            "lesson_candidates": count("lesson_candidates"),
        }
    finally:
        conn.close()


def report() -> dict:
    """agent/task 별 성장 리포트: 신규 candidate·promoted·archived·rejection 수.
    growth_reports 에 INSERT 후 dict 반환. (lessons/regression 등 기존 집계 재사용)"""
    conn = store_db.connect()
    try:
        now_rows = []
        # agent scope 집계 — lesson_candidates.agent 기준.
        agent_rows = conn.execute(
            "SELECT IFNULL(agent,'(none)') AS scope_name, status, COUNT(*) c "
            "FROM lesson_candidates GROUP BY IFNULL(agent,'(none)'), status"
        ).fetchall()
        # task scope 집계 — task_failure_patterns/task_regression_tests 기준.
        fail_rows = conn.execute(
            "SELECT task_type AS scope_name, "
            "SUM(CASE WHEN IFNULL(promoted_to_regression,0)=0 THEN 1 ELSE 0 END) AS new_candidates, "
            "SUM(CASE WHEN IFNULL(promoted_to_regression,0)=1 THEN 1 ELSE 0 END) AS promoted_count "
            "FROM task_failure_patterns GROUP BY task_type"
        ).fetchall()
        archived = conn.execute(
            "SELECT COUNT(*) c FROM lessons WHERE IFNULL(status,'active')='archived'"
        ).fetchone()["c"]

        def agg(name: str) -> dict:
            d = {"new_candidates": 0, "promoted_count": 0, "rejected_count": 0}
            for r in agent_rows:
                if r["scope_name"] != name:
                    continue
                if r["status"] == "candidate":
                    d["new_candidates"] += r["c"]
                elif r["status"] == "promoted":
                    d["promoted_count"] += r["c"]
                elif r["status"] == "rejected":
                    d["rejected_count"] += r["c"]
            return d

        reports: list[dict] = []
        agent_names = sorted({r["scope_name"] for r in agent_rows})
        for name in agent_names:
            d = agg(name)
            rec = {"scope_type": "agent", "scope_name": name,
                   "new_candidates": d["new_candidates"], "promoted_count": d["promoted_count"],
                   "archived_count": archived, "rejected_count": d["rejected_count"]}
            reports.append(rec)
            now_rows.append(rec)
        for r in fail_rows:
            rec = {"scope_type": "task", "scope_name": r["scope_name"],
                   "new_candidates": int(r["new_candidates"] or 0),
                   "promoted_count": int(r["promoted_count"] or 0),
                   "archived_count": 0, "rejected_count": 0}
            reports.append(rec)
            now_rows.append(rec)

        for rec in now_rows:
            conn.execute(
                "INSERT INTO growth_reports(scope_type, scope_name, new_candidates, promoted_count, "
                "archived_count, rejected_count, summary_json, created_at) "
                "VALUES(?,?,?,?,?,?,?, datetime('now'))",
                (rec["scope_type"], rec["scope_name"], rec["new_candidates"], rec["promoted_count"],
                 rec["archived_count"], rec["rejected_count"],
                 json.dumps(rec, ensure_ascii=False)),
            )
        conn.commit()
        return {"ok": True, "report_count": len(reports), "reports": reports}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--decay", action="store_true")
    ap.add_argument("--promote", action="store_true")
    ap.add_argument("--promote-regression", dest="promote_regression", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.seed:
        out = registry.seed()
    elif args.decay:
        out = lessons_mod.decay()
    elif args.promote:
        out = lessons_mod.promote()
    elif args.promote_regression:
        out = regression.promote_failures()
    elif args.report:
        out = report()
    else:
        out = status()
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
