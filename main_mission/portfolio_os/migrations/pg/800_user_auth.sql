-- 사용자 로그인 + RBAC (CEO 2026-06-21). PIN auth(auth_sessions 등)와 별개 — 로그인=사용자 식별, PIN=계좌 추가보호.
-- 비밀번호 평문 금지(hash만). reset token 원문 금지(hash만). auth_events append-only.
SET search_path TO portfolio, public;

CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email           text UNIQUE NOT NULL,
    display_name    text,
    password_hash   text NOT NULL,
    password_algo   text NOT NULL DEFAULT 'scrypt',
    password_updated_at timestamptz DEFAULT now(),
    role            text NOT NULL DEFAULT 'user'  CHECK (role IN ('admin','user')),
    status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','pending','locked')),
    reset_required  boolean NOT NULL DEFAULT false,
    failed_logins   int NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    last_login_at   timestamptz
);

CREATE TABLE IF NOT EXISTS user_sessions (
    session_id      text PRIMARY KEY,        -- opaque, httpOnly 쿠키
    user_id         BIGINT NOT NULL REFERENCES users(user_id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    revoked_at      timestamptz,
    ip_hash         text, user_agent_hash text
);
CREATE INDEX IF NOT EXISTS idx_usersess_user ON user_sessions(user_id);

-- user ↔ account 권한 (이게 없으면 일반 user는 그 계좌 접근 불가). admin은 전체.
CREATE TABLE IF NOT EXISTS user_account_access (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES users(user_id),
    account_index   INTEGER NOT NULL,
    access_role     text NOT NULL DEFAULT 'owner' CHECK (access_role IN ('owner','manager','viewer')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      BIGINT,
    UNIQUE (user_id, account_index)
);
CREATE INDEX IF NOT EXISTS idx_uaccess_user ON user_account_access(user_id);

CREATE TABLE IF NOT EXISTS user_auth_events (   -- append-only
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         BIGINT,
    event_type      text NOT NULL,   -- signup|login_success|login_failed|logout|password_changed|password_reset_requested|password_reset_completed|admin_password_reset|account_access_granted|account_access_revoked
    success         boolean,
    reason          text,
    ip_hash         text, user_agent_hash text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_userauthev_user ON user_auth_events(user_id, created_at);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES users(user_id),
    token_hash      text NOT NULL,    -- 원문 저장 금지
    expires_at      timestamptz NOT NULL,
    used_at         timestamptz,
    requested_ip_hash text, requested_user_agent_hash text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_resettok_user ON password_reset_tokens(user_id);

-- 가변 테이블 DELETE 허용(회수/로그아웃/토큰만료). audit(user_auth_events)는 append-only 유지.
GRANT SELECT,INSERT,UPDATE ON users, user_sessions, user_account_access, user_auth_events, password_reset_tokens TO portfolio_app;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA portfolio TO portfolio_app;
GRANT DELETE ON user_account_access, user_sessions, password_reset_tokens TO portfolio_app;

-- login_id (권장안 A: login_id + email 분리, 로그인은 둘 다 허용)
ALTER TABLE users ADD COLUMN IF NOT EXISTS login_id text UNIQUE;
