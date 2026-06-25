-- 002_hardening.sql  (Portfolio OS — Wave 1 자료조사 반영)  DRAFT
-- 001_init 위에서 동작. data-ops/broker/memory-chief 개선안 통합.
-- 핵심: audit_logs tamper-evidence(해시체인), 주문 상태전이 이력(order_events),
--       in_doubt 주문 상태, snapshot 일자 유일성, lessons 양방향 신뢰도.
-- 적용 전: 001 적용 완료 상태. dry-run: BEGIN; \i 002_hardening.sql; ROLLBACK;
-- Rollback 주석은 각 섹션 하단.

\set ON_ERROR_STOP on
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- 1. audit_logs tamper-evidence (해시체인 + append-only)
--    출처: PostgreSQL hash-chaining tamper-evident audit trail.
-- ============================================================
ALTER TABLE audit_logs
    ADD COLUMN IF NOT EXISTS prev_hash BYTEA,
    ADD COLUMN IF NOT EXISTS row_hash  BYTEA,
    ADD COLUMN IF NOT EXISTS seq       BIGINT;

CREATE OR REPLACE FUNCTION audit_hashchain() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prev BYTEA; nseq BIGINT;
BEGIN
    SELECT row_hash, seq INTO prev, nseq FROM audit_logs ORDER BY seq DESC LIMIT 1;
    NEW.seq := COALESCE(nseq, 0) + 1;
    NEW.prev_hash := prev;                         -- 첫 행 genesis = NULL
    NEW.row_hash := digest(
        concat_ws('|', NEW.seq::text, NEW.created_at::text, NEW.actor,
            NEW.action, NEW.entity_type, NEW.entity_id::text, NEW.mode,
            COALESCE(NEW.payload,'{}'::jsonb)::text,
            encode(COALESCE(NEW.prev_hash,'\x00'::bytea),'hex')), 'sha256');
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION block_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'append-only table (tamper-evident)'; END $$;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_hashchain') THEN
        CREATE TRIGGER trg_audit_hashchain BEFORE INSERT ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION audit_hashchain();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_audit_no_mutate') THEN
        CREATE TRIGGER trg_audit_no_mutate BEFORE UPDATE OR DELETE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION block_mutation();
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_seq ON audit_logs(seq);
-- 검증: SELECT seq FROM (SELECT seq, prev_hash, LAG(row_hash) OVER(ORDER BY seq) prev
--        FROM audit_logs) t WHERE prev_hash IS DISTINCT FROM prev;  -- 끊긴 체인 탐지
-- Rollback: DROP TRIGGER trg_audit_hashchain, trg_audit_no_mutate; ALTER TABLE 컬럼 DROP.

-- ============================================================
-- 2. orders.in_doubt 상태 추가 (응답불명 → 재전송 금지)
-- ============================================================
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payload_hash TEXT;  -- idempotency: id+hash 검증
ALTER TABLE orders DROP CONSTRAINT IF EXISTS chk_orders_status;
ALTER TABLE orders ADD CONSTRAINT chk_orders_status CHECK (status IN
    ('created','risk_passed','approved','submitted','in_doubt','partial','filled','rejected','canceled','aborted'));

-- ============================================================
-- 3. order_events (주문 상태전이 append-only 이력 = 진실의 원장)
--    orders 는 현재상태 캐시(read model). 1 entity=1 table(§14).
-- ============================================================
CREATE TABLE IF NOT EXISTS order_events (
    id              BIGSERIAL PRIMARY KEY,
    order_id        BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,    -- submitted|ack|partial_fill|filled|rejected|canceled|aborted|amended|in_doubt
    from_status     TEXT,
    to_status       TEXT,
    actor           TEXT,             -- broker-chief|system|broker_callback
    broker_payload  JSONB,            -- KIS 원응답 (자격증명 제외)
    reason          TEXT,             -- reject/abort 사유 enum
    occurred_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_oe_type CHECK (event_type IN
        ('submitted','ack','partial_fill','filled','rejected','canceled','aborted','amended','in_doubt'))
);
CREATE INDEX IF NOT EXISTS idx_oe_order ON order_events(order_id, occurred_at);
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_order_events_no_mutate') THEN
        CREATE TRIGGER trg_order_events_no_mutate BEFORE UPDATE OR DELETE ON order_events
            FOR EACH ROW EXECUTE FUNCTION block_mutation();
    END IF;
END $$;
-- 규약: orders.status 변경 시 같은 트랜잭션에서 order_events INSERT 의무.

-- ============================================================
-- 4. portfolio_snapshots 연속성·중복방지 (path-dependent 성과 재계산)
-- ============================================================
ALTER TABLE portfolio_snapshots
    ADD COLUMN IF NOT EXISTS as_of_date DATE GENERATED ALWAYS AS (captured_at::date) STORED,
    ADD COLUMN IF NOT EXISTS daily_return NUMERIC,
    ADD COLUMN IF NOT EXISTS drawdown_pct NUMERIC;
CREATE UNIQUE INDEX IF NOT EXISTS uq_snap_daily
    ON portfolio_snapshots(account_id, as_of_date, source);

-- ============================================================
-- 5. lessons 양방향 신뢰도 + 점수화 (ExpeL/Reflexion + 메모리 eviction)
-- ============================================================
ALTER TABLE lessons
    ADD COLUMN IF NOT EXISTS support_count   INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS refute_count    INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS confidence      NUMERIC NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS access_count    INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS last_validated_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS importance      NUMERIC,
    ADD COLUMN IF NOT EXISTS mem_tier        TEXT NOT NULL DEFAULT 'warm',  -- hot|warm|cold|archive
    ADD COLUMN IF NOT EXISTS is_critical     BOOLEAN NOT NULL DEFAULT FALSE; -- decay 면제
CREATE INDEX IF NOT EXISTS idx_lessons_tier ON lessons(mem_tier);
CREATE INDEX IF NOT EXISTS idx_lessons_conf ON lessons(confidence);
-- 승격: recurrence>=3 AND confidence>=0.6. 강등: confidence<0.4 → stage 1단계 (자동).
-- 삭제는 CEO 승인 큐. is_critical=true 는 점수 하한 고정(cold 강등 면제).

COMMENT ON TABLE order_events IS '주문 상태전이 append-only 원장. orders는 read model 캐시. tamper-evident.';
COMMENT ON COLUMN orders.status IS 'in_doubt = place_order 응답 미수신. 재전송 금지, 재조회로만 해소.';
