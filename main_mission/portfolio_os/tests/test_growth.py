"""성장 스캐폴딩 테스트 — registry / memory(decay·feedback) / tasks / prehook(게이트) / posthook.

키 없이 임시 SQLite로 전 경로 검증. (Anthropic API 미사용 — 순수 메모리/안전/추적 토대)
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_growth.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import lessons as lessons_mod
from main_mission.portfolio_os.growth import registry, memory, tasks, prehooks, posthooks


def setup():
    store_db.init()


def _insert_lesson(scope, title, body, confidence, created_at, ref=None, status="active"):
    conn = store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO lessons(account_index, scope, ref, title, body, confidence, source, status, created_at) "
            "VALUES(NULL,?,?,?,?,?,'test',?,?)",
            (scope, ref, title, body, confidence, status, created_at),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ---- registry ----
def test_registry_seed_idempotent_and_scopes():
    r1 = registry.seed()
    r2 = registry.seed()  # 멱등 — 중복 행 없이 재실행
    assert r1["ok"] and r2["ok"], (r1, r2)
    sc = registry.scopes_for("theme-sector-advisor")
    assert sc[0] == "sector", sc  # priority 최상
    assert "instrument" in sc and "market" in sc, sc
    vc = registry.scopes_for("view-coach")
    assert vc[0] == "premise", vc
    # 미등록 agent → 전역 fallback(빈 리스트가 아니라 코드 기본) 또는 []
    assert isinstance(registry.scopes_for("unknown-agent"), list)


# ---- lessons decay / archive / search ----
def test_decay_archives_stale_lowconf_and_search_excludes():
    fresh = _insert_lesson("sector", "AI 과열 주의", "...", 0.9, "2026-06-19 00:00:00", ref="AI")
    stale = _insert_lesson("sector", "오래된 낡은 견해", "...", 0.2, "2020-01-01 00:00:00", ref="AI")
    out = lessons_mod.decay()
    assert stale in out["archived"], out
    assert fresh not in out["archived"], out
    titles = [l["title"] for l in lessons_mod.search(scope="sector")]
    assert "AI 과열 주의" in titles and "오래된 낡은 견해" not in titles, titles
    # eff_confidence가 base confidence 이하(감쇠)
    item = next(l for l in lessons_mod.search(scope="sector") if l["id"] == fresh)
    assert item["eff_confidence"] <= item["confidence"], item


def test_touch_refreshes_freshness():
    lid = _insert_lesson("market", "시장 메모", "...", 0.7, "2025-01-01 00:00:00")
    before = lessons_mod.search(scope="market")
    eff_before = next(l["eff_confidence"] for l in before if l["id"] == lid)
    lessons_mod.touch([lid])
    after = lessons_mod.search(scope="market")
    eff_after = next(l["eff_confidence"] for l in after if l["id"] == lid)
    assert eff_after >= eff_before, (eff_before, eff_after)  # 참조하면 감쇠 시계 리셋


# ---- memory recall + feedback ----
def test_recall_scoped_and_feedback_roundtrip():
    _insert_lesson("sector", "반도체 사이클", "...", 0.8, "2026-06-19 00:00:00", ref="반도체")
    items = memory.recall("theme-sector-advisor", account_index=1, refs=["반도체"])
    assert any(i["title"] == "반도체 사이클" for i in items), items
    assert all("eff_confidence" in i for i in items), items
    fb = memory.record_feedback("rejected_advice", "사용자가 양자 비중 확대 거절", account_index=1,
                                agent="theme-sector-advisor", scope="sector", ref="양자", source_ref="advice:7")
    assert fb["ok"], fb
    got = memory.recall_feedback(account_index=1, agent="theme-sector-advisor")
    assert any("양자" in (f["ref"] or "") for f in got), got


# ---- tasks ----
def test_task_lifecycle_and_provenance():
    tid = tasks.open_task("risk-chief", "risk_check", account_index=1, policy_version=3,
                          prehook={"gate": "pass"})
    tasks.link_memory(tid, [{"memory_kind": "lesson", "memory_id": 1, "scope": "risk", "relevance": 0.7}])
    tasks.update_task(tid, status="done", outcome={"violations": 0}, next_action="없음")
    t = tasks.get_task(tid)
    assert t["status"] == "done" and t["policy_version"] == 3, t
    assert t["outcome"] == {"violations": 0}, t
    assert any(m["memory_kind"] == "lesson" for m in t["memory_links"]), t


# ---- prehook gates ----
def test_prehook_decision_blocks_without_selected_allocation():
    # selected allocation/스냅샷 없음 → decision 은 hard-block
    pre = prehooks.prepare("broker-chief", "decision", account_index=99)
    assert pre["gate"] == "block", pre
    assert any("selected allocation" in r for r in pre["reasons"]), pre["reasons"]
    # 차단돼도 task는 provenance와 함께 남는다(blocked)
    t = tasks.get_task(pre["task_id"])
    assert t["status"] == "blocked", t


def test_prehook_consult_passes_and_loads_memory():
    _insert_lesson("premise", "방어적 성향 유지", "...", 0.75, "2026-06-19 00:00:00")
    pre = prehooks.prepare("view-coach", "consult", account_index=1)
    assert pre["gate"] == "pass", pre
    assert isinstance(pre["memory"], list), pre
    t = tasks.get_task(pre["task_id"])
    assert t["status"] == "running" and t["prehook"]["gate"] == "pass", t


# ---- posthook ----
def test_posthook_writes_candidate_not_lesson_and_feedback():
    pre = prehooks.prepare("theme-sector-advisor", "theme_advice", account_index=1)
    lessons_before = len(lessons_mod.search(limit=999))
    res = posthooks.finalize(
        pre["task_id"], status="done",
        outcome={"themes": ["반도체"]},
        lesson_candidates=[{"scope": "sector", "title": "반도체 분산 필요", "body": "단일테마 과집중 경고",
                            "ref": "반도체", "account_index": 1, "confidence": 0.5, "agent": "theme-sector-advisor"}],
        feedback=[{"kind": "user_edit", "detail": "tilt cap 더 낮게", "account_index": 1,
                   "agent": "theme-sector-advisor", "scope": "sector", "ref": "반도체"}],
        next_action="allocation 재계산 제안", unresolved_risk="반도체 집중도",
    )
    assert res["ok"] and res["lesson_candidates"], res
    # candidate로만 — lessons 즉시 승격 금지
    lessons_after = len(lessons_mod.search(limit=999))
    assert lessons_after == lessons_before, (lessons_before, lessons_after)
    # candidate 테이블에는 존재
    ov = lessons_mod.overview()
    assert ov["candidates"].get("candidate", 0) >= 1, ov
    t = tasks.get_task(pre["task_id"])
    assert t["next_action"] == "allocation 재계산 제안" and t["unresolved_risk"] == "반도체 집중도", t


def test_posthook_promotion_requires_repetition():
    # 후보를 2회 관찰 + confidence/evidence 충족해야 승격
    for _ in range(2):
        lessons_mod.add_candidate("decision", "분할 진입이 슬리피지 줄임", "관측", ref="rebalance",
                                  confidence=0.7, evidence_ref="ev:1", agent="broker-chief")
    out = lessons_mod.promote()
    assert out["promoted_count"] >= 1, out
    assert any("분할 진입" in l["title"] for l in lessons_mod.search(scope="decision")), out


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} GROWTH TESTS PASSED")
