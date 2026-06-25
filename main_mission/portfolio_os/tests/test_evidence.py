"""evidence 엔진 테스트 (O#10).

add_evidence → recall_evidence (freshness decay·stance 보존·계좌 격리·링크).
외부 자료를 바로 매수/매도 확정하지 않고 stance(입장)만 태깅. (임시 SQLite, Anthropic 미사용)
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone, timedelta

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_evidence.sqlite3")

# env(SQLITE_PATH)는 setup()에서 핀 — import 순서로 다른 모듈 DB를 가로채지 않게.
from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import evidence


def setup():
    os.environ["SQLITE_PATH"] = _TMP
    if os.path.exists(_TMP):
        os.remove(_TMP)
    store_db.init()


def _backdate_freshness(evidence_id: int, days_ago: float):
    """봉투의 freshness_at + 컬럼 freshness 를 days_ago 전으로 조작(decay 검증용)."""
    import json
    old = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn = store_db.connect()
    try:
        row = conn.execute("SELECT body FROM evidence_documents WHERE id=?", (evidence_id,)).fetchone()
        obj = json.loads(row["body"])
        obj["__evidence__"]["freshness_at"] = old
        conn.execute("UPDATE evidence_documents SET body=?, freshness=? WHERE id=?",
                     (json.dumps(obj, ensure_ascii=False), old, evidence_id))
        conn.commit()
    finally:
        conn.close()


def test_add_and_recall_basic_with_stance_preserved():
    setup()
    eid = evidence.add_evidence(
        "news", theme="반도체", topic="HBM 수요", summary="HBM 공급 부족 지속",
        stance="long_support", source_url="http://x", source_title="HBM 리포트",
        publisher="someco", confidence=0.8, key_claims=["수요>공급"], risk_points=["사이클 둔화"],
    )
    assert isinstance(eid, int) and eid > 0
    rows = evidence.recall_evidence(theme="반도체")
    assert len(rows) == 1
    r = rows[0]
    assert r["stance"] == "long_support"          # stance 보존
    assert r["summary"] == "HBM 공급 부족 지속"
    assert r["key_claims"] == ["수요>공급"]
    assert r["risk_points"] == ["사이클 둔화"]
    assert abs(r["eff_confidence"] - 0.8) < 0.01    # 막 적재 → decay 거의 없음


def test_invalid_stance_rejected():
    setup()
    try:
        evidence.add_evidence("news", stance="strong_buy")  # 허용 stance 아님
        assert False, "should raise"
    except ValueError:
        pass


def test_freshness_decay_lowers_old_confidence():
    setup()
    fresh = evidence.add_evidence("report", theme="2차전지", summary="신규", stance="watch_only", confidence=0.8)
    old = evidence.add_evidence("report", theme="2차전지", summary="오래됨", stance="watch_only", confidence=0.8)
    _backdate_freshness(old, days_ago=90)  # 반감기 1회 → 약 0.4

    rows = evidence.recall_evidence(theme="2차전지")
    by_id = {r["id"]: r for r in rows}
    assert by_id[fresh]["eff_confidence"] > by_id[old]["eff_confidence"]
    assert abs(by_id[old]["eff_confidence"] - 0.4) < 0.02   # 0.8 * 0.5^1
    # base 는 보존, eff 만 하향.
    assert by_id[old]["base_confidence"] == 0.8
    # 정렬: fresh(높은 eff) 가 먼저.
    assert rows[0]["id"] == fresh


def test_max_age_days_filters_old():
    setup()
    old = evidence.add_evidence("news", theme="원유", summary="구자료", stance="risk_warning", confidence=0.6)
    _backdate_freshness(old, days_ago=200)
    assert evidence.recall_evidence(theme="원유", max_age_days=30) == []
    assert len(evidence.recall_evidence(theme="원유", max_age_days=365)) == 1


def test_stance_filter():
    setup()
    evidence.add_evidence("news", theme="금리", summary="a", stance="long_support")
    evidence.add_evidence("news", theme="금리", summary="b", stance="risk_warning")
    longs = evidence.recall_evidence(theme="금리", stance="long_support")
    assert len(longs) == 1 and longs[0]["summary"] == "a"


def test_account_isolation():
    setup()
    e1 = evidence.add_evidence("news", theme="공통테마", summary="acct1", stance="watch_only", account_index=1)
    e2 = evidence.add_evidence("news", theme="공통테마", summary="acct2", stance="watch_only", account_index=2)
    common = evidence.add_evidence("news", theme="공통테마", summary="공통", stance="watch_only", account_index=None)

    a1 = {r["id"] for r in evidence.recall_evidence(theme="공통테마", account_index=1)}
    assert e1 in a1 and common in a1 and e2 not in a1   # 다른 계좌(e2) 격리
    a2 = {r["id"] for r in evidence.recall_evidence(theme="공통테마", account_index=2)}
    assert e2 in a2 and common in a2 and e1 not in a2


def test_link_evidence_all_kinds():
    setup()
    eid = evidence.add_evidence("disclosure", theme="조선", summary="수주", stance="long_support")
    for kind, ref_col, table in [
        ("theme_advice", "advice_id", "theme_advice_evidence_links"),
        ("decision", "decision_id", "decision_evidence_links"),
        ("daily_review", "review_id", "daily_review_evidence_links"),
    ]:
        out = evidence.link_evidence(eid, kind, 42, note="t")
        assert out["ok"] and out["table"] == table
        conn = store_db.connect()
        try:
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {ref_col}=42 AND evidence_id=?", (eid,)
            ).fetchone()[0]
            assert n == 1
        finally:
            conn.close()


def test_link_invalid_kind():
    setup()
    eid = evidence.add_evidence("news", summary="x", stance="watch_only")
    try:
        evidence.link_evidence(eid, "order", 1)
        assert False
    except ValueError:
        pass
