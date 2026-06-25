-- 003_backtest.sql  (Portfolio OS — Wave 1)  DRAFT
-- backtest / paper trading 결과 저장 구조 (현재 스키마에 부재 → data-ops-chief 개선안 C).
-- 출처: QuantConnect 백테스트 5분리 아티팩트 + 성과지표 path-dependency(연속 NAV 보존 필수).
-- 적용 전: 001 적용. 비침습(순수 추가).
-- 주의: paper 모드 실주문(KIS 모의)은 orders(mode=paper)에. 여기는 전략단위 backtest/paper run 기록.

\set ON_ERROR_STOP on

-- (1) run 헤더 — 재현 정보
CREATE TABLE IF NOT EXISTS backtest_runs (
    id           BIGSERIAL PRIMARY KEY,
    kind         TEXT NOT NULL,                 -- backtest | paper
    concept_id   BIGINT REFERENCES investment_concepts(id) ON DELETE SET NULL,
    account_id   BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    period_start DATE, period_end DATE,
    params       JSONB NOT NULL,                -- 리밸런스 규칙 + risk_limits 스냅샷
    code_ref     TEXT,                          -- git sha 등 재현 키
    data_ref     TEXT,                          -- 사용 시세 데이터셋 식별
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_bt_kind CHECK (kind IN ('backtest','paper'))
);

-- (2) equity/NAV 시계열 — path-dependent 재계산용 (연속 보장)
CREATE TABLE IF NOT EXISTS backtest_equity_points (
    run_id       BIGINT NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    as_of        DATE NOT NULL,
    nav_krw      NUMERIC NOT NULL,
    daily_return NUMERIC,                       -- nav 기반 사전계산 캐시
    drawdown_pct NUMERIC,
    PRIMARY KEY (run_id, as_of)                 -- 시계열 자연키 = 중복방지
);

-- (3) 모의 거래 (실 orders 와 분리)
CREATE TABLE IF NOT EXISTS backtest_trades (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    instrument_id BIGINT REFERENCES instruments(id) ON DELETE SET NULL,
    side          TEXT NOT NULL, qty NUMERIC NOT NULL, price NUMERIC NOT NULL,
    traded_at     TIMESTAMP NOT NULL,
    CONSTRAINT chk_btt_side CHECK (side IN ('buy','sell'))
);
CREATE INDEX IF NOT EXISTS idx_btt_run ON backtest_trades(run_id);

-- (4) summary 지표 (재계산 가능, 조회 캐시)
CREATE TABLE IF NOT EXISTS backtest_metrics (
    run_id           BIGINT PRIMARY KEY REFERENCES backtest_runs(id) ON DELETE CASCADE,
    total_return_pct NUMERIC, cagr_pct NUMERIC,
    max_drawdown_pct NUMERIC, sharpe NUMERIC, sortino NUMERIC,
    volatility_pct   NUMERIC, win_rate_pct NUMERIC,
    computed_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE backtest_runs IS 'backtest/paper 전략 run 헤더. params에 당시 risk_limits 스냅샷 포함(재현).';
COMMENT ON TABLE backtest_equity_points IS 'NAV 일별 시계열. MDD/Sharpe는 path-dependent → 연속 보존 필수.';
