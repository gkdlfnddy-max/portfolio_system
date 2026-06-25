"""Evidence 요약 엔진 테스트 (자료 정리).

규칙기반 summarize·적재(evidence_items)·보유/관심 연결(evidence_for_account)·
freshness/stale/상충·성장(피드백→신뢰 보정)·데이터 소스 정직 표기.
Anthropic 미사용, 임시 SQLite 핀.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone, timedelta

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_evidence_summary.sqlite3")

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import evidence_summary as es


def setup():
    os.environ["SQLITE_PATH"] = _TMP
    # WAL/SHM 사이드카까지 정리 — 미삭제 시 새 연결이 stale WAL 로 readonly 오류 발생(테스트 격리).
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = _TMP + suffix
        if os.path.exists(p):
            os.remove(p)
    store_db.init()


def _days_ago(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).date().isoformat()


# --------------------------------------------------------------------------- summarize
def test_summarize_positive_rule_based():
    out = es.summarize({"summary": "HBM 공급 부족 지속, 수요 증가로 사상 최대 실적",
                        "confidence": 0.8})
    assert out["긍정요인"]            # 키워드 매칭됨
    assert not out["부정요인"]
    assert out["stance"] == "long_support"
    assert out["portfolio_impact_hint"] == "positive"
    assert not out["conflicting"]


def test_summarize_negative_rule_based():
    out = es.summarize({"summary": "어닝쇼크에 목표가 하향, 수요 둔화 우려", "confidence": 0.7})
    assert out["부정요인"] and not out["긍정요인"]
    assert out["stance"] == "risk_warning"
    assert out["portfolio_impact_hint"] == "negative"


def test_summarize_conflicting_flagged():
    out = es.summarize({"summary": "수주 증가 호재이나 유상증자 및 소송 리스크 동시",
                        "confidence": 0.8})
    assert out["긍정요인"] and out["부정요인"]
    assert out["conflicting"] is True
    assert out["stance"] == "conflicting_evidence"


def test_summarize_no_signal_insufficient():
    out = es.summarize({"summary": "회사가 정기 주주총회를 개최했다", "confidence": 0.5})
    assert out["stance"] == "insufficient_evidence"
    assert any("원문 직접" in f for f in out["추가확인"])


# --------------------------------------------------------------------------- add + gate
def test_add_evidence_stores_and_gates_weak_confidence():
    setup()
    # 낮은 confidence → 강한 액션 금지(watch_only).
    eid = es.add_evidence("news", source="someco", source_date=_days_ago(1),
                          summary="수요 증가로 호실적", confidence=0.2,
                          related_ticker="005930", related_account=1)
    assert eid > 0
    conn = store_db.connect()
    try:
        r = conn.execute("SELECT * FROM evidence_items WHERE id=?", (eid,)).fetchone()
        assert r["suggested_action"] == "watch_only"   # confidence 게이트
        assert r["source"] == "someco"
        assert r["related_ticker"] == "005930"
    finally:
        conn.close()


def test_add_evidence_strong_action_when_confident():
    setup()
    eid = es.add_evidence("financials", source="DART", source_date=_days_ago(1),
                          summary="사상 최대 실적, 수주 증가", confidence=0.85,
                          related_ticker="005930")
    conn = store_db.connect()
    try:
        r = conn.execute("SELECT * FROM evidence_items WHERE id=?", (eid,)).fetchone()
        assert r["suggested_action"] == "consider_long_review"  # 강한(검토) 액션 허용
    finally:
        conn.close()


def test_add_evidence_invalid_source_type():
    setup()
    try:
        es.add_evidence("rumor", summary="x")
        assert False, "should raise"
    except ValueError:
        pass


def test_old_source_date_marks_stale():
    setup()
    eid = es.add_evidence("news", source="old", source_date=_days_ago(400),
                          summary="수요 증가 호재", confidence=0.8, related_ticker="000660")
    conn = store_db.connect()
    try:
        r = conn.execute("SELECT stale, freshness FROM evidence_items WHERE id=?", (eid,)).fetchone()
        assert r["stale"] == 1            # 오래됨 → stale
        assert r["freshness"] < es.STALE_EFF_THRESHOLD
    finally:
        conn.close()


# --------------------------------------------------------------------------- account linking
def _make_account_with_holdings_and_universe(conn, account_index=1):
    cur = conn.execute(
        "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, source) "
        "VALUES(?,?,?,?,?)", (account_index, 1000.0, 5000.0, 1, "manual_sync"))
    snap_id = cur.lastrowid
    conn.execute("INSERT INTO holdings(snapshot_id, account_index, ticker, name, qty, market_value) "
                 "VALUES(?,?,?,?,?,?)", (snap_id, account_index, "005930", "삼성전자", 10, 4000.0))
    conn.execute(
        "INSERT INTO universe_instruments(account_index, ticker, market, name, is_active) "
        "VALUES(?,?,?,?,1)", (account_index, "069500", "KRX", "KODEX200"))
    conn.commit()


def test_evidence_for_account_links_holdings_and_universe():
    setup()
    conn = store_db.connect()
    try:
        _make_account_with_holdings_and_universe(conn, 1)
    finally:
        conn.close()
    # 보유 종목 관련
    es.add_evidence("news", source="a", source_date=_days_ago(1),
                    summary="삼성전자 수요 증가", confidence=0.8, related_ticker="005930")
    # 관심(유니버스 ETF) 관련 — related_etf 로 연결
    es.add_evidence("etf", source="b", source_date=_days_ago(1),
                    summary="KODEX200 자금 유입", confidence=0.7, related_etf="069500")
    # 내 종목과 무관 — 제외돼야 함
    es.add_evidence("news", source="c", source_date=_days_ago(1),
                    summary="무관 종목 호재", confidence=0.9, related_ticker="999999")

    res = es.evidence_for_account(1)
    tickers = {it["related_ticker"] or it["related_etf"] for it in res["items"]}
    assert "005930" in tickers
    assert "069500" in tickers
    assert "999999" not in tickers
    assert "005930" in res["holdings_tickers"]
    assert "069500" in res["universe_tickers"]


def test_evidence_for_account_isolation():
    setup()
    conn = store_db.connect()
    try:
        _make_account_with_holdings_and_universe(conn, 1)
        _make_account_with_holdings_and_universe(conn, 2)
    finally:
        conn.close()
    # 계좌1 전용 evidence
    es.add_evidence("news", source="a", source_date=_days_ago(1), summary="수요 증가",
                    confidence=0.8, related_ticker="005930", related_account=1)
    # 계좌2 전용 (같은 종목이지만 다른 계좌)
    es.add_evidence("news", source="b", source_date=_days_ago(1), summary="수요 증가",
                    confidence=0.8, related_ticker="005930", related_account=2)
    # 계좌무관(공통)
    es.add_evidence("news", source="c", source_date=_days_ago(1), summary="수요 증가",
                    confidence=0.8, related_ticker="005930", related_account=None)

    a1 = es.evidence_for_account(1)
    accts = {it["related_account"] for it in a1["items"]}
    assert 2 not in accts                  # 다른 계좌 격리
    assert 1 in accts and None in accts     # 자기 계좌 + 공통 포함


def test_evidence_for_account_conflict_detection():
    setup()
    conn = store_db.connect()
    try:
        _make_account_with_holdings_and_universe(conn, 1)
    finally:
        conn.close()
    es.add_evidence("news", source="a", source_date=_days_ago(1),
                    summary="삼성전자 수요 증가 호재", confidence=0.8, related_ticker="005930")
    es.add_evidence("news", source="b", source_date=_days_ago(1),
                    summary="삼성전자 어닝쇼크 목표가 하향", confidence=0.8, related_ticker="005930")
    res = es.evidence_for_account(1)
    assert res["conflicts"]
    assert res["conflicts"][0]["key"] == "005930"
    assert res["conflicts"][0]["positive_evidence_ids"]
    assert res["conflicts"][0]["negative_evidence_ids"]


# --------------------------------------------------------------------------- growth
def test_source_type_trust_grows_with_feedback():
    setup()
    # 같은 source_type 에 accepted 다수 → multiplier > 1
    for _ in range(4):
        eid = es.add_evidence("financials", source="DART", source_date=_days_ago(1),
                              summary="사상 최대 실적", confidence=0.8, related_ticker="005930")
        es.record_feedback(eid, "accepted")
    trust = es.source_type_trust("financials")
    assert trust["samples"] == 4
    assert trust["multiplier"] > 1.0


def test_source_type_trust_drops_with_rejections():
    setup()
    for _ in range(4):
        eid = es.add_evidence("news", source="x", source_date=_days_ago(1),
                              summary="수요 증가", confidence=0.8, related_ticker="005930")
        es.record_feedback(eid, "rejected_as_wrong")
    trust = es.source_type_trust("news")
    assert trust["multiplier"] < 1.0


def test_trust_neutral_when_few_samples():
    setup()
    es.add_evidence("macro", source="m", source_date=_days_ago(1), summary="금리 인상 전망",
                    confidence=0.6)
    assert es.source_type_trust("macro")["multiplier"] == 1.0   # 샘플 부족 → 중립


def test_effective_confidence_combines_decay_and_trust():
    setup()
    for _ in range(4):
        eid = es.add_evidence("filing", source="DART", source_date=_days_ago(1),
                              summary="수주 증가", confidence=0.8, related_ticker="005930")
        es.record_feedback(eid, "accepted")
    eff = es.effective_confidence("filing", 0.8, _days_ago(0))
    assert 0.0 < eff <= 1.0
    assert eff > 0.8 * 0.9   # 신뢰 보정으로 단순 decay 보다 보존/상향


def test_record_feedback_invalid():
    setup()
    eid = es.add_evidence("news", summary="수요 증가", confidence=0.5, related_ticker="005930")
    try:
        es.record_feedback(eid, "loved_it")
        assert False
    except ValueError:
        pass


# --------------------------------------------------------------------------- honesty / stub
def test_data_source_status_honest():
    st = es.data_source_status()
    assert st["manual_input"] == "available"
    assert st["dart_filings"] == "not_connected"
    assert st["news_api"] == "not_connected"
    assert "미연동" in st["note"]


def test_ingest_stub_refuses_when_not_connected():
    setup()
    out = es.ingest_stub("filing", {"summary": "x"})
    assert out["ok"] is False
    assert out["ingested"] == 0
    assert out["reason"] == "connector_not_connected"


# --------------------------------------------------------------------------- source_type lens
def test_summarize_includes_source_type_followup():
    out = es.summarize({"summary": "사상 최대 실적", "confidence": 0.8,
                        "source_type": "financials"})
    assert out["source_type"] == "financials"
    assert out["source_lens"] == "재무제표"
    assert any("재무제표] 확인 관점" in f for f in out["추가확인"])


def test_summarize_filing_vs_news_lens_differ():
    fl = es.summarize({"summary": "유상증자 결정", "source_type": "filing", "confidence": 0.7})
    nw = es.summarize({"summary": "수요 둔화 우려", "source_type": "news", "confidence": 0.7})
    assert fl["source_lens"] == "공시"
    assert nw["source_lens"] == "뉴스/기사"
    assert fl["추가확인"] != nw["추가확인"]


# --------------------------------------------------------------------------- brief (출력형식)
def test_brief_full_output_format():
    out = es.brief({
        "summary": "HBM 공급 부족, 수요 증가로 사상 최대 실적",
        "source_type": "financials", "source": "DART", "source_date": _days_ago(1),
        "confidence": 0.85, "related_ticker": "005930",
    })
    # 출력형식 키 전체 존재
    for k in ("자료", "관련종목ETF", "긍정요인", "부정요인", "불확실성",
              "포트폴리오영향", "추가확인", "confidence", "freshness"):
        assert k in out
    assert out["자료"]["유형"] == "재무제표"
    assert out["관련종목ETF"]["ticker"] == "005930"
    assert out["긍정요인"]
    assert out["포트폴리오영향"]["방향"] == "positive"
    assert out["suggested_action"] == "consider_long_review"  # confidence 충분 → 강한 검토
    assert out["data_source_status"]["dart_filings"] == "not_connected"  # 정직


def test_brief_gates_weak_confidence_to_watch_only():
    out = es.brief({"summary": "수요 증가 호재", "source_type": "news",
                    "confidence": 0.2, "source_date": _days_ago(1),
                    "related_ticker": "005930"})
    assert out["suggested_action"] == "watch_only"   # 근거 게이트


def test_brief_gates_conflicting_to_watch_only():
    out = es.brief({"summary": "수주 증가 호재이나 소송·유상증자 리스크",
                    "source_type": "news", "confidence": 0.9,
                    "source_date": _days_ago(1), "related_ticker": "005930"})
    assert out["conflicting"] is True
    assert out["suggested_action"] == "watch_only"   # 상충 → 강한 조언 금지


def test_brief_stale_marks_and_gates():
    out = es.brief({"summary": "수요 증가 호재", "source_type": "news",
                    "confidence": 0.8, "source_date": _days_ago(500),
                    "related_ticker": "005930"})
    assert out["stale"] is True
    assert out["suggested_action"] == "watch_only"


# --------------------------------------------------------------------------- briefs_by_source_type
def test_briefs_by_source_type_groups_and_links():
    setup()
    conn = store_db.connect()
    try:
        _make_account_with_holdings_and_universe(conn, 1)
    finally:
        conn.close()
    es.add_evidence("financials", source="DART", source_date=_days_ago(1),
                    summary="삼성전자 사상 최대 실적", confidence=0.8, related_ticker="005930")
    es.add_evidence("news", source="news1", source_date=_days_ago(1),
                    summary="삼성전자 수요 증가", confidence=0.7, related_ticker="005930")
    es.add_evidence("etf", source="etf1", source_date=_days_ago(1),
                    summary="KODEX200 자금 유입", confidence=0.7, related_etf="069500")
    # 무관 종목 — 제외
    es.add_evidence("news", source="x", source_date=_days_ago(1),
                    summary="무관 호재", confidence=0.9, related_ticker="999999")

    out = es.briefs_by_source_type(1)
    groups = out["by_source_type"]
    assert "financials" in groups and "news" in groups and "etf" in groups
    all_keys = set()
    for items in groups.values():
        for it in items:
            all_keys |= set(it["관련종목ETF"].values())
    assert "005930" in all_keys and "069500" in all_keys
    assert "999999" not in all_keys
    assert out["data_source_status"]["news_api"] == "not_connected"


def test_briefs_by_source_type_stale_gated():
    setup()
    conn = store_db.connect()
    try:
        _make_account_with_holdings_and_universe(conn, 1)
    finally:
        conn.close()
    es.add_evidence("news", source="old", source_date=_days_ago(500),
                    summary="삼성전자 수요 증가 호재", confidence=0.85, related_ticker="005930")
    out = es.briefs_by_source_type(1)
    news = out["by_source_type"].get("news", [])
    assert news
    assert news[0]["stale"] is True
    assert news[0]["suggested_action"] == "watch_only"   # stale → 강한 조언 금지
