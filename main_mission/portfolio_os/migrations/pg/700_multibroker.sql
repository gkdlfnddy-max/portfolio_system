-- 멀티 브로커. broker 별 자격증명 참조(평문 금지). accounts.broker 로 adapter 선택.
SET search_path TO portfolio, public;
ALTER TABLE portfolio.accounts ADD COLUMN IF NOT EXISTS broker text;  -- kis|kiwoom|manual|paper
CREATE TABLE IF NOT EXISTS portfolio.broker_credentials (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id BIGINT NOT NULL, broker text NOT NULL,
    key_ref text, secret_ref text, token_status text, token_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, broker));
CREATE INDEX IF NOT EXISTS idx_brokercred ON portfolio.broker_credentials(account_id, broker);
