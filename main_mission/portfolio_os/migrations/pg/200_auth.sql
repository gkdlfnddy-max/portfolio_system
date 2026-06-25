-- ============================================================================
-- 200_auth.sql — Portfolio OS PIN/간편비밀번호 인증 스키마 (PostgreSQL)
-- ----------------------------------------------------------------------------
-- ⚠️ PIN 평문 저장 금지 — pin_hash(scrypt/argon2)만. PIN 값은 어떤 컬럼/로그에도 저장 안 함.
--
-- 전제: database/role/schema 는 이미 생성됨. 본 파일은 DDL authoring 전용 (psql 실행 금지).
-- 규칙: portfolio.<name> 로 qualified · 멱등 · id IDENTITY PK · timestamptz now() ·
--       FK ON DELETE RESTRICT · jsonb · DROP/DELETE/TRUNCATE/시드 금지.
-- ============================================================================

SET search_path TO portfolio, public;

-- ============================================================================
-- user_security_settings — 사용자별 PIN 보안 설정 (해시만 보관).
-- ============================================================================
CREATE TABLE IF NOT EXISTS portfolio.user_security_settings (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         TEXT NOT NULL,
    pin_hash        TEXT,                                  -- scrypt/argon2 해시 (평문 절대 금지)
    pin_algo        TEXT,                                  -- scrypt | argon2id
    pin_salt        TEXT,                                  -- 해시 salt (PIN 값 아님)
    pin_enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    pin_set_at      timestamptz,
    pin_changed_at  timestamptz,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_user_security_user UNIQUE (user_id)
);
CREATE INDEX IF NOT EXISTS idx_user_security_user       ON portfolio.user_security_settings (user_id);
CREATE INDEX IF NOT EXISTS idx_user_security_created_at ON portfolio.user_security_settings (created_at);

-- ============================================================================
-- auth_sessions — 인증 세션 (unlock 후 만료까지). 토큰 평문 보관 금지 (session_id 식별자).
-- ============================================================================
CREATE TABLE IF NOT EXISTS portfolio.auth_sessions (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    unlocked_at     timestamptz,
    expires_at      timestamptz,
    last_seen_at    timestamptz,
    ip_hash         TEXT,                                  -- IP 해시 (평문 금지)
    user_agent_hash TEXT,                                  -- UA 해시 (평문 금지)
    scope           TEXT,                                  -- read | sensitive | admin
    revoked_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_auth_sessions_session UNIQUE (session_id)
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_session    ON portfolio.auth_sessions (session_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user       ON portfolio.auth_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_created_at ON portfolio.auth_sessions (created_at);

-- ============================================================================
-- auth_events — 인증 이벤트 (APPEND-ONLY 감사 로그).
-- event_type 값:
--   pin_set                 PIN 최초 설정
--   pin_verify_success      PIN 검증 성공
--   pin_verify_failed       PIN 검증 실패
--   pin_locked              연속 실패로 잠금
--   pin_changed             PIN 변경
--   pin_reset_requested     PIN 재설정 요청
--   sensitive_action_reauth 민감 작업 재인증
--   session_expired         세션 만료
-- (PIN 값/해시는 본 테이블에 저장하지 않는다.)
-- ============================================================================
CREATE TABLE IF NOT EXISTS portfolio.auth_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    success         BOOLEAN,
    reason          TEXT,
    ip_hash         TEXT,
    user_agent_hash TEXT,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_auth_events_user_created ON portfolio.auth_events (user_id, created_at);

-- end of 200_auth.sql
