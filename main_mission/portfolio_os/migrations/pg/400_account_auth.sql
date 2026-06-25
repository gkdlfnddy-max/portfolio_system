-- 계좌별 PIN / 계좌별 접근 잠금 (앱 전체 PIN 위에 추가되는 2단계 보안).
-- 평문 PIN 저장 금지 — account_pin_hash(scrypt)만. PIN 값은 어떤 컬럼/로그에도 저장 안 함.
-- 계좌별 세션 분리: 계좌 A unlock 이 계좌 B 로 전파되면 안 됨(account_id 기준 검증).
SET search_path TO portfolio, public;

-- 계좌별 보안 설정 (계좌당 1행).
CREATE TABLE IF NOT EXISTS portfolio.account_security_settings (
    id                              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id                      BIGINT NOT NULL,
    account_pin_enabled             boolean NOT NULL DEFAULT false,
    account_pin_hash                text,
    account_pin_algo                text,           -- 'scrypt'
    account_pin_salt                text,
    account_pin_set_at              timestamptz,
    account_pin_changed_at          timestamptz,
    require_pin_on_entry            boolean NOT NULL DEFAULT false,  -- 계좌 진입 시 PIN
    require_pin_for_strategy        boolean NOT NULL DEFAULT false,
    require_pin_for_rebalance       boolean NOT NULL DEFAULT false,
    require_pin_for_order_approval  boolean NOT NULL DEFAULT true,   -- 주문 승인은 항상 재인증(기본)
    failed_attempts                 int NOT NULL DEFAULT 0,
    locked_until                    timestamptz,
    created_at                      timestamptz NOT NULL DEFAULT now(),
    updated_at                      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id)
);

-- 계좌별 unlock 세션 (앱 세션과 분리 — 계좌 A unlock 이 B 로 전파 금지).
CREATE TABLE IF NOT EXISTS portfolio.account_auth_sessions (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id    text NOT NULL,        -- 앱 세션 쿠키 id (계좌별 unlock 을 이 세션에 귀속)
    account_id    BIGINT NOT NULL,
    user_id       text NOT NULL,
    unlocked_at   timestamptz,
    expires_at    timestamptz,
    last_seen_at  timestamptz,
    scope         text,
    revoked_at    timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_acct_sess ON portfolio.account_auth_sessions(account_id, session_id);

-- 계좌별 인증 이벤트 (APPEND-ONLY). PIN 값 미저장.
-- event_type: account_pin_set | account_pin_verify_success | account_pin_verify_failed
--           | account_pin_locked | account_pin_changed | account_sensitive_reauth | account_session_expired
CREATE TABLE IF NOT EXISTS portfolio.account_auth_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id      BIGINT NOT NULL,
    user_id         text,
    event_type      text NOT NULL,
    success         boolean,
    reason          text,
    ip_hash         text,
    user_agent_hash text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acct_authev ON portfolio.account_auth_events(account_id, created_at DESC);
