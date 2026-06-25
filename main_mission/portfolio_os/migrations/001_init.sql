-- 001_init.sql  (Portfolio OS)
-- DB: portfolio_os_db (PostgreSQL 초안) — 로컬은 SQLite 사용 (data/portfolio.sqlite3)
-- 승인형 포트폴리오 운영 시스템 초기 스키마 — DRAFT (CEO 아키텍처 승인 후 적용).
-- 안전 핵심: orders.client_order_id UNIQUE(중복방지), mode CHECK(paper/live),
--            자격증명은 어떤 테이블에도 저장하지 않음(.env 전용).
--
-- 적용 전:
--   CREATE DATABASE portfolio_os_db;
--   dry-run: BEGIN; \i 001_init.sql; ROLLBACK;
-- Rollback: DROP TABLE 위 역순 CASCADE.

\set ON_ERROR_STOP on

-- updated_at 트리거 함수
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END; $$;

-- ============================================================
-- accounts  (자격증명 저장 금지 — account_ref 는 해시/별칭만)
-- ============================================================
CREATE TABLE IF NOT EXISTS accounts (
    id              BIGSERIAL PRIMARY KEY,
    alias           TEXT,
    mode            TEXT NOT NULL DEFAULT 'paper',
    broker          TEXT NOT NULL DEFAULT 'KIS',
    base_currency   TEXT NOT NULL DEFAULT 'KRW',
    account_ref     TEXT,                       -- 계좌번호 평문 저장 금지(해시/식별자만)
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_accounts_mode CHECK (mode IN ('paper','live'))
);

-- ============================================================
-- instruments  (종목 마스터)
-- ============================================================
CREATE TABLE IF NOT EXISTS instruments (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL,
    market          TEXT NOT NULL,              -- KRX|NASDAQ|NYSE|AMEX
    name            TEXT,
    asset_class     TEXT NOT NULL DEFAULT 'stock', -- stock|etf|cash
    currency        TEXT NOT NULL,              -- KRW|USD
    sector          TEXT,
    is_leveraged    BOOLEAN NOT NULL DEFAULT FALSE,
    is_inverse      BOOLEAN NOT NULL DEFAULT FALSE,
    leverage_factor NUMERIC NOT NULL DEFAULT 1.0,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_instruments UNIQUE (ticker, market)
);

-- ============================================================
-- balances  (잔고 스냅샷 라인)
-- ============================================================
CREATE TABLE IF NOT EXISTS balances (
    id              BIGSERIAL PRIMARY KEY,
    account_id      BIGINT REFERENCES accounts(id) ON DELETE CASCADE,
    instrument_id   BIGINT REFERENCES instruments(id) ON DELETE SET NULL,
    captured_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    qty             NUMERIC NOT NULL DEFAULT 0,
    avg_price       NUMERIC,
    market_value    NUMERIC,                    -- 원통화 평가액
    currency        TEXT NOT NULL,
    value_krw       NUMERIC,                    -- KRW 환산
    is_stale        BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_balances_acct ON balances(account_id, captured_at);

-- ============================================================
-- quotes  (현재가/환율 캐시)
-- ============================================================
CREATE TABLE IF NOT EXISTS quotes (
    id              BIGSERIAL PRIMARY KEY,
    instrument_id   BIGINT REFERENCES instruments(id) ON DELETE CASCADE,
    price           NUMERIC NOT NULL,
    currency        TEXT NOT NULL,
    captured_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    is_stale        BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_quotes_instr ON quotes(instrument_id, captured_at);

-- ============================================================
-- investment_concepts  (CEO 컨셉 입력)
-- ============================================================
CREATE TABLE IF NOT EXISTS investment_concepts (
    id              BIGSERIAL PRIMARY KEY,
    raw_text        TEXT NOT NULL,              -- 원문
    parsed          JSONB,                      -- 파싱 결과
    created_by      TEXT DEFAULT 'CEO',
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ============================================================
-- target_weights  (컨셉 → 목표 비중)
-- ============================================================
CREATE TABLE IF NOT EXISTS target_weights (
    id              BIGSERIAL PRIMARY KEY,
    concept_id      BIGINT REFERENCES investment_concepts(id) ON DELETE CASCADE,
    scope           TEXT NOT NULL,              -- asset_class|sector|instrument|cash|short
    key             TEXT NOT NULL,
    target_pct      NUMERIC NOT NULL,
    rationale       TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tw_concept ON target_weights(concept_id);

-- ============================================================
-- risk_limits  (SSOT)
-- ============================================================
CREATE TABLE IF NOT EXISTS risk_limits (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    value           NUMERIC NOT NULL,
    hard            BOOLEAN NOT NULL DEFAULT TRUE,
    updated_by      TEXT,
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

-- 안전 기본값 시드 (safety_rules.md B)
INSERT INTO risk_limits(name, value, hard, updated_by) VALUES
    ('cash_min_pct',            10, TRUE,  'system'),
    ('single_name_max_pct',     20, TRUE,  'system'),
    ('short_total_max_pct',     10, TRUE,  'system'),
    ('leverage_total_max_pct',  15, TRUE,  'system'),
    ('daily_loss_stop_pct',      5, TRUE,  'system'),
    ('single_order_max_pct',     5, TRUE,  'system'),
    ('max_orders_per_session',  20, FALSE, 'system')
ON CONFLICT (name) DO NOTHING;

-- ============================================================
-- rebalance_proposals
-- ============================================================
CREATE TABLE IF NOT EXISTS rebalance_proposals (
    id              BIGSERIAL PRIMARY KEY,
    session_task_id BIGINT,
    account_id      BIGINT REFERENCES accounts(id) ON DELETE SET NULL,
    concept_id      BIGINT REFERENCES investment_concepts(id) ON DELETE SET NULL,
    status          TEXT NOT NULL DEFAULT 'draft',
    drift           JSONB,
    fx_rate         NUMERIC,
    rationale       TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_proposal_status CHECK (status IN
        ('draft','risk_pending','risk_failed','approval_pending','approved','rejected','executed','expired'))
);

-- ============================================================
-- proposal_trades  (제안 내 거래 라인)
-- ============================================================
CREATE TABLE IF NOT EXISTS proposal_trades (
    id              BIGSERIAL PRIMARY KEY,
    proposal_id     BIGINT REFERENCES rebalance_proposals(id) ON DELETE CASCADE,
    instrument_id   BIGINT REFERENCES instruments(id) ON DELETE SET NULL,
    side            TEXT NOT NULL,              -- buy|sell
    qty             NUMERIC NOT NULL,
    est_value       NUMERIC,
    currency        TEXT NOT NULL,
    rationale       TEXT,                       -- 어느 drift 를 줄이는지
    CONSTRAINT chk_ptrade_side CHECK (side IN ('buy','sell'))
);

-- ============================================================
-- risk_checks
-- ============================================================
CREATE TABLE IF NOT EXISTS risk_checks (
    id              BIGSERIAL PRIMARY KEY,
    proposal_id     BIGINT REFERENCES rebalance_proposals(id) ON DELETE CASCADE,
    passed          BOOLEAN NOT NULL,
    violations      JSONB,                      -- [{limit, observed, threshold}]
    checked_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ============================================================
-- approvals  (CEO 승인)
-- ============================================================
CREATE TABLE IF NOT EXISTS approvals (
    id              BIGSERIAL PRIMARY KEY,
    proposal_id     BIGINT REFERENCES rebalance_proposals(id) ON DELETE CASCADE,
    decision        TEXT NOT NULL,              -- approved|rejected|partial
    reason          TEXT,
    decided_by      TEXT NOT NULL DEFAULT 'CEO',
    decided_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_approval_decision CHECK (decision IN ('approved','rejected','partial'))
);

-- ============================================================
-- orders  (상태머신 + idempotency)
-- ============================================================
CREATE TABLE IF NOT EXISTS orders (
    id              BIGSERIAL PRIMARY KEY,
    proposal_id     BIGINT REFERENCES rebalance_proposals(id) ON DELETE SET NULL,
    instrument_id   BIGINT REFERENCES instruments(id) ON DELETE SET NULL,
    client_order_id TEXT NOT NULL UNIQUE,       -- 중복 실행 방지 (안전 A4)
    broker_order_id TEXT,
    mode            TEXT NOT NULL DEFAULT 'paper',
    side            TEXT NOT NULL,
    qty             NUMERIC NOT NULL,
    order_type      TEXT NOT NULL DEFAULT 'limit',
    limit_price     NUMERIC,
    currency        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'created',
    filled_qty      NUMERIC NOT NULL DEFAULT 0,
    avg_fill_price  NUMERIC,
    submitted_at    TIMESTAMP,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_orders_mode CHECK (mode IN ('paper','live')),
    CONSTRAINT chk_orders_side CHECK (side IN ('buy','sell')),
    CONSTRAINT chk_orders_status CHECK (status IN
        ('created','risk_passed','approved','submitted','partial','filled','rejected','canceled','aborted'))
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_proposal ON orders(proposal_id);

-- ============================================================
-- fills  (체결, 부분체결 라인)
-- ============================================================
CREATE TABLE IF NOT EXISTS fills (
    id              BIGSERIAL PRIMARY KEY,
    order_id        BIGINT REFERENCES orders(id) ON DELETE CASCADE,
    qty             NUMERIC NOT NULL,
    price           NUMERIC NOT NULL,
    currency        TEXT NOT NULL,
    filled_at       TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);

-- ============================================================
-- portfolio_snapshots  (성과 추적)
-- ============================================================
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    account_id      BIGINT REFERENCES accounts(id) ON DELETE CASCADE,
    captured_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    total_value_krw NUMERIC,
    cash_pct        NUMERIC,
    long_pct        NUMERIC,
    short_pct       NUMERIC,
    weights         JSONB,
    fx_rate         NUMERIC,
    source          TEXT                        -- live_fetch|post_fill|scheduled
);
CREATE INDEX IF NOT EXISTS idx_snap_acct ON portfolio_snapshots(account_id, captured_at);

-- ============================================================
-- audit_logs  (모든 중요 행위)
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_logs (
    id              BIGSERIAL PRIMARY KEY,
    actor           TEXT,                       -- CEO|chief명|system
    action          TEXT NOT NULL,
    entity_type     TEXT,
    entity_id       BIGINT,
    mode            TEXT,
    payload         JSONB,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);

-- ============================================================
-- tasks  (task 트리)
-- ============================================================
CREATE TABLE IF NOT EXISTS tasks (
    id              BIGSERIAL PRIMARY KEY,
    parent_task_id  BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
    task_type       TEXT NOT NULL,              -- T1..T12
    status          TEXT NOT NULL DEFAULT 'pending',
    input           JSONB,
    output          JSONB,
    success_criteria TEXT,
    fallback        TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_tasks_status CHECK (status IN
        ('pending','in_progress','blocked','done','failed','aborted'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);

-- ============================================================
-- lessons  (회고/승격)
-- ============================================================
CREATE TABLE IF NOT EXISTS lessons (
    id              BIGSERIAL PRIMARY KEY,
    task_id         BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
    stage           TEXT NOT NULL DEFAULT 'reflection', -- raw|reflection|candidate|validated|knowhow|sop
    title           TEXT,
    body            TEXT,
    reflection      JSONB,                      -- 7질문
    recurrence_count INTEGER NOT NULL DEFAULT 1,
    promoted_to     TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lessons_stage ON lessons(stage);

-- ============================================================
-- updated_at 트리거
-- ============================================================
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['accounts','instruments','rebalance_proposals','orders','tasks','lessons'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_'||t||'_updated_at') THEN
            EXECUTE format(
                'CREATE TRIGGER trg_%1$s_updated_at BEFORE UPDATE ON %1$s FOR EACH ROW EXECUTE FUNCTION set_updated_at()', t);
        END IF;
    END LOOP;
END$$;

COMMENT ON TABLE orders IS 'client_order_id UNIQUE = 중복 주문 방지(안전 A4). mode CHECK = paper/live 격리.';
COMMENT ON TABLE risk_limits IS 'safety_rules.md B 의 SSOT. hard=true 는 risk-chief hard-block 대상.';
COMMENT ON TABLE audit_logs IS '모든 중요 행위 추적(안전 A5). 조용한 주문/실수 방지.';
