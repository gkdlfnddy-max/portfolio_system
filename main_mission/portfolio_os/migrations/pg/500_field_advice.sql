-- 필드별 AI 조언 (중전제 각 입력 필드 Advisor). append-only. AI 조언은 바로 policy 반영 금지.
SET search_path TO portfolio, public;
CREATE TABLE IF NOT EXISTS portfolio.field_consultations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id BIGINT NOT NULL,
    field_name text NOT NULL,
    agent_name text,
    advice_type text,
    original_text text,
    suggested_text text,
    extracted_variables_json jsonb,
    risk_warnings_json jsonb,
    missing_points_json jsonb,
    follow_up_json jsonb,
    evidence_ids text,
    lesson_ids text,
    confidence numeric,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fieldconsult ON portfolio.field_consultations(account_id, field_name, id DESC);
CREATE INDEX IF NOT EXISTS idx_fieldconsult_vars ON portfolio.field_consultations USING gin (extracted_variables_json);
CREATE TABLE IF NOT EXISTS portfolio.field_advice_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id BIGINT NOT NULL,
    field_consultation_id BIGINT,
    field_name text,
    user_action text NOT NULL,
    detail text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fieldadviceev ON portfolio.field_advice_events(account_id, field_consultation_id, id DESC);
