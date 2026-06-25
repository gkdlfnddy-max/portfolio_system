"""종목/ETF 선정 엔진 테스트 (Step 2–5) — bucket 후보 + 데이터 가용성 + evidence 수집 +
비교표 + 적합도 분류.

핵심 검증(불변 원칙):
- bucket 후보가 실재 티커 시드로 나열된다(추천 아님).
- 데이터 미연동이면 정직 표기(가짜 지표 0).
- evidence 가용분만 부착(없으면 빈 채로 정직).
- 데이터 부족이면 강한 추천 금지(strong_conclusion_allowed=False).
- 데이터 충분하면 비교 가능 + confidence 상승.
- 국채 bucket 은 A 에이전트(bond_bucket) 시드 미연동 시 빈 후보 + honest flag.
- 자동주문/policy 변경 없음(읽기 전용).

임시 SQLite 핀(SQLITE_PATH) + 직접 적재(수동 입력 경로). Anthropic 미사용.
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_security_selection.sqlite3")

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import security_selection as ss


def setup():
    os.environ["SQLITE_PATH"] = _TMP
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = _TMP + suffix
        if os.path.exists(p):
            os.remove(p)
    store_db.init()


# --------------------------------------------------------------------------- helpers
def _add_universe(conn, acct, ticker, name, asset_class="equity_etf", is_inverse=0):
    conn.execute(
        "INSERT INTO universe_instruments(account_index, ticker, market, name, asset_class, "
        "is_inverse, is_active, source) VALUES(?,?,?,?,?,?,1,'manual')",
        (acct, ticker, "KRX" if not ticker.isupper() else "US", name, asset_class, is_inverse))
    conn.commit()


def _add_constituent(conn, etf, ticker, name, w, sector="IT", country="US", as_of="2026-06-20"):
    conn.execute(
        "INSERT INTO etf_constituents(etf_ticker, constituent_ticker, constituent_name, "
        "weight_pct, sector, country, as_of, source) VALUES(?,?,?,?,?,?,?,'manual')",
        (etf, ticker, name, w, sector, country, as_of))
    conn.commit()


def _add_evidence(conn, source_type, ticker=None, etf=None, summary="x", conf=0.7,
                  source="manual", source_date="2026-06-20"):
    conn.execute(
        "INSERT INTO evidence_items(source, source_type, source_date, summary, confidence, "
        "related_ticker, related_etf) VALUES(?,?,?,?,?,?,?)",
        (source, source_type, source_date, summary, conf, ticker, etf))
    conn.commit()


def _add_prices(conn, code, n=60, base=100.0):
    # 단조 가격 시계열(변동성/스캔 계산 가능하도록 충분히).
    for i in range(n):
        d = f"2026-{4 + i // 30:02d}-{1 + i % 30:02d}"
        px = base + (i % 5) - 2  # 소폭 진동
        conn.execute(
            "INSERT OR REPLACE INTO price_history(instrument_code, trade_date, open, high, low, "
            "close, volume, source) VALUES(?,?,?,?,?,?,?,'test')",
            (code, d, px, px + 1, px - 1, px, 1000 + i))
    conn.commit()


# --------------------------------------------------------------------------- buckets
def test_list_buckets_has_all_five():
    setup()
    out = ss.list_buckets()
    for k in ("global_core", "robotics", "semiconductor", "semiconductor_inverse", "treasury"):
        assert k in out, out
    assert out["global_core"]["seed_count"] == 5  # SPY/VOO/QQQ/VT/VTI


def test_bucket_candidates_seed_real_tickers():
    setup()
    cb = ss.bucket_candidates(1, "global_core")
    assert cb["ok"]
    tickers = {c["ticker"] for c in cb["candidates"]}
    assert {"SPY", "VOO", "QQQ", "VT", "VTI"} <= tickers
    # 후보 나열일 뿐 — 추천/주문 단어 없음
    assert "추천" not in cb["note"]


def test_semiconductor_bucket_mixes_etf_and_stocks():
    setup()
    cb = ss.bucket_candidates(1, "semiconductor")
    tickers = {c["ticker"] for c in cb["candidates"]}
    assert {"SOXX", "SMH", "005930", "000660"} <= tickers
    # 개별주 메타 표기
    samsung = next(c for c in cb["candidates"] if c["ticker"] == "005930")
    assert samsung["asset_class"] == "stock"


def test_unknown_bucket_is_honest_error():
    setup()
    cb = ss.bucket_candidates(1, "crypto")
    assert cb["ok"] is False and "알 수 없는" in cb["error"]


# --------------------------------------------------------------------------- 국채(A 에이전트)
def test_treasury_empty_when_bond_bucket_not_seeded():
    setup()
    cb = ss.bucket_candidates(1, "treasury")
    assert cb["ok"] and cb["candidate_count"] == 0
    assert cb["honest_flags"], "미연동 정직 플래그 있어야"
    assert "미연동" in cb["note"]


def test_treasury_uses_bond_bucket_seed_from_universe():
    setup()
    conn = store_db.connect()
    try:
        # A 에이전트가 국채 후보를 universe 에 시드한 상황
        _add_universe(conn, 1, "KOSEF국고채10년", "KOSEF 국고채10년", asset_class="bond")
        _add_universe(conn, 1, "148070", "KOSEF 국고채10년", asset_class="bond")
    finally:
        conn.close()
    cb = ss.bucket_candidates(1, "treasury")
    assert cb["candidate_count"] == 2, cb
    assert all("A:bond_bucket" in c["source"] for c in cb["candidates"])
    assert cb["honest_flags"] == []


# --------------------------------------------------------------------------- 데이터 가용성
def test_data_availability_honest_when_nothing_connected():
    setup()
    cand = {"ticker": "SPY", "asset_class": "equity_etf"}
    av = ss.data_availability(1, cand)
    assert av["price_daily"] == "미연동"
    assert av["etf_constituents"] == "미연동"
    assert av["macro"] == "미연동"
    assert av["financials"].startswith("직접대상 아님")  # ETF


def test_data_availability_marks_connected_sources():
    setup()
    conn = store_db.connect()
    try:
        # evidence 는 계좌 보유/관심(universe)에 연결돼야 부착됨 — 후보를 관심에 등록.
        _add_universe(conn, 1, "SPY", "SPDR S&P 500", asset_class="equity_etf")
        _add_prices(conn, "SPY")
        _add_constituent(conn, "SPY", "AAPL", "Apple", 7.0)
        _add_evidence(conn, "news", etf="SPY")
    finally:
        conn.close()
    cand = {"ticker": "SPY", "asset_class": "equity_etf"}
    av = ss.data_availability(1, cand)
    assert av["price_daily"] == "connected"
    assert av["etf_constituents"] == "connected"
    assert av["news"] == "connected"


def test_stock_financials_axis_when_evidence_present():
    setup()
    conn = store_db.connect()
    try:
        _add_universe(conn, 1, "005930", "삼성전자", asset_class="stock")
        _add_evidence(conn, "financials", ticker="005930")
    finally:
        conn.close()
    cand = {"ticker": "005930", "asset_class": "stock"}
    av = ss.data_availability(1, cand)
    assert av["financials"] == "connected"


# --------------------------------------------------------------------------- evidence 수집
def test_evidence_for_empty_is_honest():
    setup()
    ev = ss.evidence_for(1, {"ticker": "QQQ", "asset_class": "equity_etf"})
    assert ev["evidence_count"] == 0
    assert "강한 결론 불가" in ev["note"]


def test_evidence_for_collects_available_only():
    setup()
    conn = store_db.connect()
    try:
        _add_universe(conn, 1, "QQQ", "QQQ", asset_class="equity_etf")
        _add_evidence(conn, "news", etf="QQQ", summary="반도체 수요")
        _add_evidence(conn, "sector", etf="QQQ", summary="섹터")
    finally:
        conn.close()
    ev = ss.evidence_for(1, {"ticker": "QQQ", "asset_class": "equity_etf"})
    assert ev["evidence_count"] == 2
    assert set(ev["by_source_type"].keys()) == {"news", "sector"}


# --------------------------------------------------------------------------- 비교표
def test_compare_bucket_no_data_blocks_strong_recommendation():
    setup()
    cmp = ss.compare_bucket(1, "robotics")
    assert cmp["ok"]
    assert cmp["strong_conclusion_possible"] is False
    assert "강한 추천 불가" in cmp["headline"]
    # 모든 후보가 강한 결론 금지
    assert all(not r["confidence"]["strong_conclusion_allowed"] for r in cmp["comparison"])
    # 비용은 미연동 → unknown(추정 금지)
    assert all(r["cost"]["available"] is False for r in cmp["comparison"])


def test_compare_bucket_volatility_and_overlap_when_data_present():
    setup()
    conn = store_db.connect()
    try:
        # 두 ETF 구성 겹침 + 가격
        _add_constituent(conn, "SOXX", "NVDA", "NVIDIA", 10.0)
        _add_constituent(conn, "SOXX", "005930", "삼성전자", 8.0, country="KR")
        _add_constituent(conn, "SMH", "NVDA", "NVIDIA", 12.0)
        _add_constituent(conn, "SMH", "MSFT", "Microsoft", 6.0)
        _add_prices(conn, "SOXX")
        _add_evidence(conn, "sector", etf="SOXX")
    finally:
        conn.close()
    cmp = ss.compare_bucket(1, "semiconductor")
    soxx = next(r for r in cmp["comparison"] if r["ticker"] == "SOXX")
    assert soxx["volatility"]["available"] is True
    # 겹침 계산됨(SOXX vs SMH)
    assert isinstance(soxx["overlap_exposure"], list) and soxx["overlap_exposure"]
    assert soxx["confidence"]["value"] > 0.0


def test_compare_strong_conclusion_possible_with_rich_data():
    setup()
    conn = store_db.connect()
    try:
        _add_universe(conn, 1, "SOXX", "iShares Semi", asset_class="equity_etf")
        _add_constituent(conn, "SOXX", "NVDA", "NVIDIA", 10.0)
        _add_constituent(conn, "SMH", "NVDA", "NVIDIA", 12.0)
        _add_prices(conn, "SOXX")
        _add_evidence(conn, "sector", etf="SOXX")
        _add_evidence(conn, "news", etf="SOXX")
    finally:
        conn.close()
    cmp = ss.compare_bucket(1, "semiconductor")
    soxx = next(r for r in cmp["comparison"] if r["ticker"] == "SOXX")
    assert soxx["confidence"]["strong_conclusion_allowed"] is True, soxx["confidence"]
    assert cmp["strong_conclusion_possible"] is True


# --------------------------------------------------------------------------- 분류
def test_classify_no_data_puts_all_in_need_more():
    setup()
    cl = ss.classify_bucket(1, "robotics")
    assert cl["ok"]
    assert cl["final_candidates"] == []
    assert len(cl["need_more_data"]) >= 1
    assert "final 후보 없음" in cl["headline"]


def test_classify_inverse_bucket_flags_hedge_only():
    setup()
    cmp = ss.compare_bucket(1, "semiconductor_inverse")
    # 인버스 후보 모두 헤지 전용 리스크 표기
    for r in cmp["comparison"]:
        assert any("헤지 전용" in risk for risk in r["risks"]), r["risks"]


def test_classify_promotes_when_data_rich():
    setup()
    conn = store_db.connect()
    try:
        _add_universe(conn, 1, "SOXX", "iShares Semi", asset_class="equity_etf")
        _add_constituent(conn, "SOXX", "NVDA", "NVIDIA", 10.0)
        _add_constituent(conn, "SMH", "NVDA", "NVIDIA", 12.0)
        _add_prices(conn, "SOXX")
        _add_evidence(conn, "sector", etf="SOXX")
        _add_evidence(conn, "news", etf="SOXX")
    finally:
        conn.close()
    cl = ss.classify_bucket(1, "semiconductor")
    tickers_final = {c["ticker"] for c in cl["final_candidates"]}
    assert "SOXX" in tickers_final, cl


# --------------------------------------------------------------------------- 우량주 필터
def test_quality_filter_honest_none_when_no_financial_data():
    setup()
    # 재무/밸류에이션 구조화 수치 미연동 → passed=None(가짜 통과/점수 금지).
    qf = ss.quality_filter("005930")
    assert qf["ok"] and qf["applicable"] is True
    assert qf["passed"] is None
    assert "데이터 필요" in qf["reason"]
    assert qf["missing_metrics"]  # 어떤 지표가 필요한지 정직 표기
    assert qf["honest"] is True


def test_quality_filter_qualitative_evidence_does_not_pass():
    setup()
    conn = store_db.connect()
    try:
        # 정성 'financials' 자료가 있어도 수치가 아니므로 통과시키면 안 됨(가짜 통과 금지).
        _add_evidence(conn, "financials", ticker="005930", summary="실적 호조")
    finally:
        conn.close()
    qf = ss.quality_filter("005930")
    assert qf["passed"] is None  # 여전히 None — 정성 자료로 통과 금지
    assert qf["qualitative_financials_evidence"] is True


def test_quality_filter_not_applicable_for_etf():
    setup()
    qf = ss.quality_filter("SPY")  # ETF 메타
    assert qf["applicable"] is False
    assert qf["passed"] is None
    assert "etf_scorecard" in qf["reason"].lower() or "ETF" in qf["reason"]


# --------------------------------------------------------------------------- ETF 스코어카드
def test_etf_scorecard_honest_unconnected_meta():
    setup()
    sc = ss.etf_scorecard("SPY", 1)
    assert sc["ok"]
    card = sc["scorecard"]
    # 운용보수/괴리율/추적오차 등 메타 미연동 → 추정 금지(unknown)
    for key in ("expense_ratio_pct", "tracking_error_pct", "premium_discount_pct",
                "fx_hedged", "distribution_yield_pct"):
        assert card[key]["status"] == "미연동"
        assert card[key]["value"] is None
    # 구성 미연동이면 강한 결론 금지
    assert sc["strong_conclusion_allowed"] is False


def test_etf_scorecard_connected_constituents_and_overlap():
    setup()
    conn = store_db.connect()
    try:
        # 후보 SOXX 구성 + 계좌 보유 ETF(SMH) 구성 → 중복노출 계산.
        _add_constituent(conn, "SOXX", "NVDA", "NVIDIA", 12.0)
        _add_constituent(conn, "SOXX", "005930", "삼성전자", 9.0, country="KR")
        _add_constituent(conn, "SMH", "NVDA", "NVIDIA", 15.0)
        _add_universe(conn, 1, "SMH", "VanEck Semi", asset_class="equity_etf")
    finally:
        conn.close()
    sc = ss.etf_scorecard("SOXX", 1)
    card = sc["scorecard"]
    assert card["top_holdings"]["status"] == "connected"
    assert card["sector_exposure"]["status"] == "connected"
    ow = card["overlap_with_holdings"]
    assert ow["status"] == "connected"
    assert any(p["with"] == "SMH" for p in ow["value"])


def test_etf_scorecard_concentration_flag_when_overlap_high():
    setup()
    conn = store_db.connect()
    try:
        # 후보와 보유 ETF 가 크게 겹침 → 20%+ concentration_flag.
        _add_constituent(conn, "SOXX", "NVDA", "NVIDIA", 25.0)
        _add_constituent(conn, "SMH", "NVDA", "NVIDIA", 30.0)
        _add_universe(conn, 1, "SMH", "VanEck Semi", asset_class="equity_etf")
    finally:
        conn.close()
    sc = ss.etf_scorecard("SOXX", 1)
    ow = sc["scorecard"]["overlap_with_holdings"]
    assert ow["concentration_flag"] is True
    assert ow["max_overlap_weight_pct"] >= 20.0


# --------------------------------------------------------------------------- compare/classify 연동
def test_compare_bucket_includes_quality_and_scorecard():
    setup()
    cmp = ss.compare_bucket(1, "semiconductor")
    # 개별주(005930)는 quality_filter, ETF(SOXX)는 etf_scorecard 가 채워짐.
    stock = next(r for r in cmp["comparison"] if r["ticker"] == "005930")
    etf = next(r for r in cmp["comparison"] if r["ticker"] == "SOXX")
    assert stock["quality_filter"] is not None and stock["etf_scorecard"] is None
    assert etf["etf_scorecard"] is not None and etf["quality_filter"] is None
    # 데이터 미연동 개별주는 강한 결론 금지 신호가 risks 에 정직 표기.
    assert any("우량주 필터 적용 불가" in c for c in stock["risks"])


def test_classify_stock_no_financials_not_in_final():
    setup()
    conn = store_db.connect()
    try:
        # 개별주를 데이터로 채워도(가격/evidence) 우량주 필터 미연동이면 final 승격 금지.
        _add_universe(conn, 1, "005930", "삼성전자", asset_class="stock")
        _add_prices(conn, "005930")
        _add_evidence(conn, "news", ticker="005930")
        _add_evidence(conn, "sector", ticker="005930")
    finally:
        conn.close()
    cl = ss.classify_bucket(1, "semiconductor")
    final_tk = {c["ticker"] for c in cl["final_candidates"]}
    assert "005930" not in final_tk  # 우량주 필터 데이터 미연동 → final 금지
    # 대안/추가확인 어딘가에 사유와 함께 존재
    others = (cl["alternatives"] + cl["need_more_data"])
    assert any(c["ticker"] == "005930" for c in others)


# --------------------------------------------------------------------------- 읽기 전용 보장
def test_engine_does_not_write_orders_or_policy():
    setup()
    # 비교/분류 실행 후 주문/정책 테이블이 비어 있어야(읽기 전용).
    ss.compare_bucket(1, "global_core")
    ss.classify_bucket(1, "semiconductor")
    ss.quality_filter("005930")
    ss.etf_scorecard("SOXX", 1)
    conn = store_db.connect()
    try:
        orders = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
        sels = conn.execute("SELECT COUNT(*) c FROM allocation_selections").fetchone()["c"]
    finally:
        conn.close()
    assert orders == 0 and sels == 0
