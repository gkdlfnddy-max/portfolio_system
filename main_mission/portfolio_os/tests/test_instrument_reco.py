"""종목/ETF 공통 지식(instrument_master) + 2계층 결합 추천(stock_reco.recommend).

CEO 지시 검증 4기준 고정:
  1) 반도체 테마 → 삼성/하이닉스 외 후보 확장
  2) ETF 요청 시 개별주와 ETF 구분
  3) 계좌별 추천이 동일 복붙 아니라 성향 따라 다름
  4) 추천이 draft 로만 저장, 승인 전 주문 아님 (auto_order_created False)

KRX 검증은 DART corp_map(파일) 사용 — 격리 sqlite 와 무관하게 동작. Anthropic API 미사용.
"""
from __future__ import annotations

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import instrument_master as im
from main_mission.portfolio_os import stock_reco as sr
from main_mission.portfolio_os import investor_objective as io


def setup_function():
    store_db.init()
    im.seed()


def _set_account(idx, *, risk, goal):
    conn = store_db.connect()
    try:
        conn.execute("INSERT OR IGNORE INTO accounts(account_index, alias, mode) VALUES(?,?,?)",
                     (idx, "t", "mock"))
        conn.execute("INSERT OR REPLACE INTO investor_profile(account_index, risk_tolerance, updated_at) "
                     "VALUES(?,?,datetime('now'))", (idx, risk))
        conn.commit()
    finally:
        conn.close()
    io.set_objective(idx, {"investment_goal": goal})


# ── 기준 1: 반도체 확장 ──
def test_semiconductor_theme_expands_beyond_samsung_hynix():
    stocks = im.by_theme("반도체", kind="stock")
    tickers = {s["ticker"] for s in stocks}
    assert {"005930", "000660"}.issubset(tickers)           # 삼성·하이닉스 포함
    assert len(tickers) > 2                                  # 그 외 확장
    assert {"042700", "240810"}.intersection(tickers)       # 한미반도체/원익IPS 등 KRX 확장
    assert {"NVDA", "ASML"}.intersection(tickers)            # 미국 반도체도


# ── 기준 2: ETF vs 개별주 구분 ──
def test_etf_and_stock_separated():
    etfs = im.by_theme("반도체", kind="etf")
    stocks = im.by_theme("반도체", kind="stock")
    assert all(e["is_etf"] == 1 for e in etfs)
    assert all(s["is_etf"] == 0 for s in stocks)
    etf_tk = {e["ticker"] for e in etfs}
    assert {"SOXX", "SMH"}.issubset(etf_tk)                  # 반도체 ETF
    assert "005930" not in etf_tk                            # 개별주는 ETF 목록에 없음


def test_recommend_marks_candidate_type():
    _set_account(31, risk="neutral", goal="growth")
    out = sr.recommend(31, theme="반도체", kind="all", n=30)  # 전체 포함(개별주가 ETF보다 상위라 n 충분히)
    types = {c["candidate_id"]: c["candidate_type"] for c in out["candidates"]}
    assert types.get("SOXX") == "etf"
    assert types.get("005930") == "stock"


# ── 기준 3: 계좌별 차등(복붙 금지) ──
def test_account_specific_ordering_differs():
    _set_account(32, risk="aggressive", goal="aggressive_growth")
    _set_account(33, risk="defensive", goal="loss_reduction")
    agg = sr.recommend(32, theme="반도체", kind="all", n=12)["candidates"]
    deff = sr.recommend(33, theme="반도체", kind="all", n=12)["candidates"]

    def first_kind(cands):
        return cands[0]["candidate_type"]
    # 공격형 → 개별주 상위, 방어형 → ETF 상위 (동일 복붙 아님)
    assert first_kind(agg) == "stock"
    assert first_kind(deff) == "etf"
    # 같은 종목(005930)의 적합도 점수가 계좌별로 다름
    s_agg = next(c["fit_to_account"]["score"] for c in agg if c["candidate_id"] == "005930")
    s_def = next(c["fit_to_account"]["score"] for c in deff if c["candidate_id"] == "005930")
    assert s_agg > s_def


def test_account_exclude_filters_candidate():
    _set_account(34, risk="neutral", goal="growth")
    conn = store_db.connect()
    try:  # 005930 를 계좌 제외(is_active=0)
        conn.execute("INSERT INTO universe_instruments(account_index, ticker, market, is_active, verified_at) "
                     "VALUES(?,?,?,?,datetime('now'))", (34, "005930", "KRX", 0))
        conn.commit()
    finally:
        conn.close()
    out = sr.recommend(34, theme="반도체", kind="stock", n=12)
    assert all(c["candidate_id"] != "005930" for c in out["candidates"])  # 제외 반영


# ── 기준 4: draft 전용, 주문 아님 ──
def test_recommend_is_draft_no_order():
    _set_account(35, risk="neutral", goal="growth")
    out = sr.recommend(35, theme="반도체", kind="all", n=5)
    assert out["auto_order_created"] is False and out["requires_user_approval"] is True


def test_save_draft_and_feedback_persist():
    _set_account(36, risk="neutral", goal="growth")
    out = sr.recommend(36, theme="반도체", kind="stock", n=5)
    d = sr.save_draft(36, request_kind="theme", request_key="반도체", kind="stock",
                      candidates=out["candidates"])
    assert d["ok"] and d["draft_id"]
    fb = sr.record_feedback(36, ticker="042700", action="selected", reco_draft_id=d["draft_id"])
    assert fb["ok"]
    conn = store_db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM account_reco_draft WHERE account_index=36").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM account_reco_feedback WHERE account_index=36").fetchone()[0] == 1
    finally:
        conn.close()


def test_only_verified_seeded():
    # 시드된 모든 master 행은 verified=1 (가짜 티커 금지).
    conn = store_db.connect()
    try:
        bad = conn.execute("SELECT COUNT(*) FROM instrument_master WHERE verified=0").fetchone()[0]
    finally:
        conn.close()
    assert bad == 0
