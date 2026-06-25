-- Daily Portfolio Review (실시간 봇 아님). 관망도 정상. 주문은 selected allocation+drift 에서만.
SET search_path TO portfolio, public;
CREATE TABLE IF NOT EXISTS portfolio.market_context_snapshots (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rates_json jsonb, fx_json jsonb, indices_json jsonb, news_json jsonb, summary text,
    captured_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS portfolio.daily_portfolio_reviews (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id BIGINT NOT NULL, review_date date NOT NULL,
    account_snapshot_id BIGINT, selected_allocation_id BIGINT, drift_score numeric,
    market_context_id BIGINT, action_decision text, action_reason text, no_trade_reason text,
    scheduled_order_plan_id BIGINT, risk_passed boolean, approved_by_user boolean NOT NULL DEFAULT false,
    payload jsonb, created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, review_date),
    CHECK (action_decision IN ('buy','sell','rebalance','hold','watch')));
CREATE INDEX IF NOT EXISTS idx_dailyreview ON portfolio.daily_portfolio_reviews(account_id, review_date DESC);
CREATE TABLE IF NOT EXISTS portfolio.scheduled_order_plans (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id BIGINT NOT NULL, review_id BIGINT, decision_id BIGINT,
    status text NOT NULL DEFAULT 'pending_approval', valid_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS idx_schedplan ON portfolio.scheduled_order_plans(account_id, id DESC);
CREATE TABLE IF NOT EXISTS portfolio.scheduled_order_steps (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plan_id BIGINT NOT NULL, ref text, ticker text, direction text,
    total_pct numeric, total_krw numeric, cycle_pct numeric, cycle_krw numeric, remaining_pct numeric,
    round_no int, total_rounds int, limit_price numeric, valid_until timestamptz,
    on_unfilled text, hold_condition text, status text NOT NULL DEFAULT 'candidate',
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (total_pct IS NULL OR (total_pct >= 0 AND total_pct <= 100)));
CREATE INDEX IF NOT EXISTS idx_schedstep ON portfolio.scheduled_order_steps(plan_id);
