"""Track D — 정책/공시/뉴스 커넥터(policy_news) 테스트.

검증(불변 안전):
  - 정책 이벤트(policy_events) 저장 + 멱등(같은 날짜·제목 중복 0).
  - stance 검증(VALID_STANCE) · severity 0~1 클램프 · 잘못된 값 거부.
  - suggest_stance: 키워드 *후보*만(확정 아님), 상충/무근거면 neutral(가짜 단정 0).
  - 공시/뉴스 요약 → evidence_items 저장(사실/해석/불확실성 구조, 근거 약하면 자동 약화).
  - 자동 피드 미연동 정직(data_connected=False) · 가짜 뉴스 0 · Anthropic 미사용.
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_policynews.sqlite3")
# WAL/SHM 사이드카까지 정리 — 미삭제 시 새 연결이 stale WAL 로 readonly/IO 오류(테스트 격리).
for _sfx in ("", "-wal", "-shm", "-journal"):
    if os.path.exists(_TMP + _sfx):
        os.remove(_TMP + _sfx)
os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import policy_news as pn


def setup():
    os.environ["SQLITE_PATH"] = _TMP
    store_db.init()


def setup_function(_fn=None):
    os.environ["SQLITE_PATH"] = _TMP
    conn = store_db.connect()
    try:
        conn.execute("DELETE FROM policy_events")
        conn.execute("DELETE FROM evidence_items")
        conn.commit()
    finally:
        conn.close()


# ---- 1) 정책 이벤트 저장 + 멱등 ----
def test_add_policy_event_and_idempotent():
    r1 = pn.add_policy_event("2026-06-01", "정부 반도체 보조금 확대", sector="반도체",
                             stance="favorable", severity=0.6)
    assert r1["ok"] and r1["written"] is True
    r2 = pn.add_policy_event("2026-06-01", "정부 반도체 보조금 확대", sector="반도체",
                             stance="favorable", severity=0.6)
    assert r2["written"] is False   # 중복 적재 안 함(멱등)
    conn = store_db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM policy_events").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1


def test_add_policy_event_invalid_stance():
    r = pn.add_policy_event("2026-06-01", "x", stance="bullish")
    assert r["ok"] is False


def test_severity_clamped():
    pn.add_policy_event("2026-06-02", "규제 강화", stance="adverse", severity=5.0)
    conn = store_db.connect()
    try:
        sev = conn.execute("SELECT severity FROM policy_events WHERE title='규제 강화'").fetchone()["severity"]
    finally:
        conn.close()
    assert sev == 1.0   # 0~1 클램프


def test_add_policy_requires_date_and_title():
    assert pn.add_policy_event("", "title")["ok"] is False
    assert pn.add_policy_event("2026-06-01", "")["ok"] is False


# ---- 2) stance 후보 (키워드 — 확정 아님) ----
def test_suggest_stance_favorable():
    s = pn.suggest_stance("정부, 반도체 보조금 지원 확대")
    assert s["stance"] == "favorable"
    assert s["is_suggestion"] is True


def test_suggest_stance_adverse():
    assert pn.suggest_stance("공정위, 플랫폼 규제 강화 및 과징금")["stance"] == "adverse"


def test_suggest_stance_conflict_is_neutral():
    # 호재/악재 키워드 동시 → neutral(가짜 단정 금지, 사람 판단).
    assert pn.suggest_stance("규제 강화하되 일부 지원 확대")["stance"] == "neutral"


def test_suggest_stance_none_is_neutral():
    assert pn.suggest_stance("기업 정기 주주총회 개최")["stance"] == "neutral"


# ---- 3) 공시/뉴스 요약 → evidence_items ----
def test_add_news_summary_stores_evidence():
    out = pn.add_news_summary("삼성전자 분기 최대 실적", source_type="news",
                              source="manual", source_date="2026-06-01",
                              related_ticker="005930",
                              facts=["매출 사상 최대"], interpretation=["수요 강세"])
    assert out["ok"] is True
    conn = store_db.connect()
    try:
        row = conn.execute(
            "SELECT source_type, related_ticker, summary FROM evidence_items "
            "WHERE id=?", (out["evidence_id"],)).fetchone()
    finally:
        conn.close()
    assert row["source_type"] == "news"
    assert row["related_ticker"] == "005930"
    assert "최대 실적" in row["summary"] and "매출 사상 최대" in row["summary"]


def test_add_news_summary_filing_type():
    out = pn.add_news_summary("유상증자 결정", source_type="filing", source="dart",
                              source_date="2026-06-02", related_ticker="000660")
    assert out["ok"] is True and out["source_type"] == "filing"


def test_add_news_summary_invalid_type():
    assert pn.add_news_summary("x", source_type="rumor")["ok"] is False


def test_add_news_summary_requires_content():
    assert pn.add_news_summary("", summary="")["ok"] is False


# ---- 4) 연동 상태 정직 ----
def test_status_honest_not_connected():
    st = pn.status()
    assert st["data_connected"] is False       # 자동 피드 미연동(정직)
    assert st["manual_input"] == "available"
    assert "가짜 뉴스 0" in st["note"]


def test_no_anthropic_import():
    import pathlib
    src = pathlib.Path(pn.__file__).read_text(encoding="utf-8").lower()
    assert "import anthropic" not in src
    assert "anthropic_api_key" not in src
