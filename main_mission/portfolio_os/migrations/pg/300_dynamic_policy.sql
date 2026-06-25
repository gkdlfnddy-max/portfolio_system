-- Dynamic Policy (유연 투자기준) — portfolio.portfolio_policies 동적 컬럼 (SQLite parity).
-- 멱등: ADD COLUMN IF NOT EXISTS. 운영 데이터 변경/삭제 없음.
-- 투자 스타일 값은 유동적(default+override), hard rule 은 코드(policy_rules.HARD_RULES)에서 불변.
SET search_path TO portfolio, public;

ALTER TABLE portfolio.portfolio_policies ADD COLUMN IF NOT EXISTS policy_type          text;   -- single_stock_focus|etf_diversified|cash_defensive|growth_theme|dividend_income|custom
ALTER TABLE portfolio.portfolio_policies ADD COLUMN IF NOT EXISTS policy_template      text;   -- 시작 템플릿 id
ALTER TABLE portfolio.portfolio_policies ADD COLUMN IF NOT EXISTS user_overrides_json  jsonb;  -- 사용자가 바꾼 기본값
ALTER TABLE portfolio.portfolio_policies ADD COLUMN IF NOT EXISTS disabled_rules_json  jsonb;  -- 끈 규칙(단, hard rule 은 불가)
ALTER TABLE portfolio.portfolio_policies ADD COLUMN IF NOT EXISTS custom_rules_json    jsonb;  -- 사용자 직접 규칙
ALTER TABLE portfolio.portfolio_policies ADD COLUMN IF NOT EXISTS policy_notes_json    jsonb;  -- 메모/근거

CREATE INDEX IF NOT EXISTS idx_pp_policy_type ON portfolio.portfolio_policies(policy_type);
