"""Growth Middleware 거버넌스 — 모든 작업이 강제로 prehook→실행→posthook 루프를 타는지 증명.

증명: task_type/agent 강제 · prehook gate=block 시 fn 미실행 · 통과 시 posthook 저장 ·
      실패도 기록 · account-bound hard-block.
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_gmw.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os.growth import middleware as mw


def setup():
    store_db.init()


def _conn():
    return store_db.connect()


def test_task_type_required():
    try:
        mw.run_task("", "theme-agent", lambda i, c: {"result": 1})
        assert False, "task_type 없는데 실행됨"
    except ValueError as e:
        assert "task_type" in str(e)


def test_agent_required():
    try:
        mw.run_task("theme_advice", "", lambda i, c: {"result": 1})
        assert False, "agent 없는데 실행됨"
    except ValueError as e:
        assert "agent" in str(e)


def test_prehook_block_skips_fn():
    # decision 은 account-bound + selected allocation 필요 → 없으면 block, fn 실행 안 됨.
    ran = {"called": False}
    def fn(i, c):
        ran["called"] = True
        return {"result": "should-not-run"}
    out = mw.run_task("decision", "broker-chief", fn, account_index=999)
    assert out["blocked"] is True and out["gate"] == "block", out
    assert ran["called"] is False, "block인데 fn이 실행됨"
    assert out["result"] is None


def test_pass_runs_fn_and_posthook_saves_candidate():
    # theme_advice 는 요구사항 없음 → pass. fn 이 ctx(memory) 받고 lesson candidate 반환 → posthook 저장.
    seen_ctx = {}
    def fn(inp, ctx):
        seen_ctx.update(ctx)
        return {
            "result": {"direction": "mixed_swing"},
            "lesson_candidates": [{"scope": "sector", "title": "반도체 혼재", "agent": "theme-agent",
                                   "body": "장기 성장 + 단기 과열 공존 → mixed_swing, exposure plan 으로만 저장",
                                   "ref": "반도체", "confidence": 0.6}],
            "validations": [{"name": "direction_set", "ok": True}],
        }
    out = mw.run_task("theme_advice", "theme-agent", fn, account_index=1, refs=["반도체"], input={"theme": "반도체"})
    assert out["ok"] and out["success"] and not out["blocked"], out
    assert "task_id" in seen_ctx and "memory" in seen_ctx, "ctx에 prehook 컨텍스트 없음"
    assert out["posthook"]["lesson_candidates"], "lesson candidate가 저장 안 됨"
    # DB 확인: task done + candidate 행 존재
    c = _conn()
    t = c.execute("SELECT status FROM tasks WHERE id=?", (out["task_id"],)).fetchone()
    assert t and t["status"] == "done", dict(t) if t else None
    cand = c.execute("SELECT count(*) n FROM lesson_candidates WHERE title=?", ("반도체 혼재",)).fetchone()
    assert cand["n"] >= 1, "lesson_candidates 미저장"
    c.close()


def test_validation_failure_records_pattern_and_needs_review():
    def fn(i, c):
        return {"result": {}, "validations": [{"name": "sum_100", "ok": False, "detail": "합계 110%"}]}
    out = mw.run_task("allocation_check", "allocation-agent", fn, account_index=1)
    assert out["ok"] and out["success"] is False, out  # 실행은 됐지만 validation 실패 → failed
    c = _conn()
    t = c.execute("SELECT status FROM tasks WHERE id=?", (out["task_id"],)).fetchone()
    assert t["status"] == "failed", dict(t)
    fp = c.execute("SELECT count(*) n FROM task_failure_patterns WHERE task_type='allocation_check'").fetchone()
    assert fp["n"] >= 1, "실패 패턴 미기록"
    c.close()


def test_fn_exception_recorded_as_failure():
    def fn(i, c):
        raise RuntimeError("boom")
    out = mw.run_task("theme_advice", "theme-agent", fn, account_index=1)
    assert out["ok"] is False and out["success"] is False, out
    c = _conn()
    fp = c.execute("SELECT count(*) n FROM task_failure_patterns WHERE detail LIKE '%boom%'").fetchone()
    assert fp["n"] >= 1, "예외 실패 미기록"
    c.close()


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} GROWTH-MIDDLEWARE TESTS PASSED")
