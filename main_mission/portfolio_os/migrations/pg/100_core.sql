-- ============================================================================
-- 100_core.sql — Portfolio OS core schema (PostgreSQL promotion)
-- ----------------------------------------------------------------------------
-- 본 파일은 SQLite (store/schema.sql) 운영 truth 의 PostgreSQL 승격본이다.
-- 컬럼명/의미는 SQLite 원본과 충실히 정렬한다 (divergent 금지).
--
-- 전제 (이 파일에서 하지 않음):
--   - 데이터베이스/롤/스키마 생성은 이미 완료됨. 본 파일은 DDL authoring 전용.
--   - psql 연결/실행 금지. 본 파일은 작성만 한다.
--
-- 규칙 (strict):
--   - 모든 table/index/constraint 는 portfolio.<name> 로 schema-qualified.
--   - 멱등(idempotent): CREATE TABLE/INDEX IF NOT EXISTS, ADD COLUMN IF NOT EXISTS.
--   - PK: id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY.
--   - 타임스탬프: timestamptz NOT NULL DEFAULT now().
--   - FK: portfolio.<table>(id) ON DELETE RESTRICT (운영 truth 는 cascade-delete 금지).
--   - percent 컬럼: CHECK (col >= 0 AND col <= 100).
--   - money/quantity: numeric (float 금지).
--   - JSON 컬럼은 jsonb + 검색 대상은 GIN 인덱스.
--   - DROP/DELETE/TRUNCATE/파괴적 구문/데이터 시드 금지.
--
-- 자격증명(키/시크릿/토큰/평문 계좌번호)은 어떤 테이블에도 저장하지 않는다 (.env 전용).
-- ============================================================================

SET search_path TO portfolio, public;

-- ============================================================================
-- OPERATIONAL TRUTH (운영 truth — 계좌/스냅샷/주문)
-- ============================================================================

-- accounts — 계좌 메타 (.env KIS_ACCOUNT_{n}_* 미러). 자격증명 평문 저장 금지.
CREATE TABLE IF NOT EXISTS portfolio.accounts (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_index     INTEGER NOT NULL,                 -- .env KIS_ACCOUNT_{n}
    alias             TEXT,
    mode              TEXT NOT NULL DEFAULT 'paper',     -- paper | live | mock
    broker            TEXT NOT NULL DEFAULT 'KIS',
    base_currency     TEXT NOT NULL DEFAULT 'KRW',
    account_no_masked TEXT,                              -- 앞2자리+마스킹 (평문 금지)
    has_credentials   BOOLEAN NOT NULL DEFAULT FALSE,
    token_status      TEXT,                              -- ok | error | unknown
    sync_status       TEXT,                              -- ok | error | never
    last_error        TEXT,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    last_synced_at    timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_accounts_index UNIQUE (account_index),
    CONSTRAINT chk_accounts_mode CHECK (mode IN ('paper','live','mock'))
);
CREATE INDEX IF NOT EXISTS idx_accounts_active     ON portfolio.accounts (is_active);
CREATE INDEX IF NOT EXISTS idx_accounts_created_at ON portfolio.accounts (created_at);

-- account_snapshots — 잔고 스냅샷 (계좌×시점) 금액 truth.
CREATE TABLE IF NOT EXISTS portfolio.account_snapshots (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id      BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    cash_krw        numeric,
    total_value_krw numeric,
    holdings_count  INTEGER,
    fx_rate         numeric,
    source          TEXT,                                -- kis_live | kis_paper | manual_sync
    is_stale        BOOLEAN NOT NULL DEFAULT FALSE,
    captured_at     timestamptz NOT NULL DEFAULT now(),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_acct_snap_account    ON portfolio.account_snapshots (account_id);
CREATE INDEX IF NOT EXISTS idx_acct_snap_created_at ON portfolio.account_snapshots (created_at);
CREATE INDEX IF NOT EXISTS idx_acct_snap_captured   ON portfolio.account_snapshots (account_id, captured_at DESC);

-- position_snapshots — 보유종목 (스냅샷 행 단위). SQLite holdings 승격.
CREATE TABLE IF NOT EXISTS portfolio.position_snapshots (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id          BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    account_snapshot_id BIGINT NOT NULL REFERENCES portfolio.account_snapshots(id) ON DELETE RESTRICT,
    ticker              TEXT NOT NULL,
    name                TEXT,
    qty                 numeric,
    avg_price           numeric,
    market_value        numeric,
    currency            TEXT NOT NULL DEFAULT 'KRW',
    captured_at         timestamptz NOT NULL DEFAULT now(),
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pos_snap_account    ON portfolio.position_snapshots (account_id);
CREATE INDEX IF NOT EXISTS idx_pos_snap_created_at ON portfolio.position_snapshots (created_at);
CREATE INDEX IF NOT EXISTS idx_pos_snap_acctsnap   ON portfolio.position_snapshots (account_snapshot_id);

-- price_snapshots — 현재가 스냅샷. SQLite quotes 승격.
CREATE TABLE IF NOT EXISTS portfolio.price_snapshots (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticker      TEXT NOT NULL,
    market      TEXT,
    price       numeric,
    source      TEXT,
    captured_at timestamptz NOT NULL DEFAULT now(),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_price_snap_ticker     ON portfolio.price_snapshots (ticker, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_price_snap_created_at ON portfolio.price_snapshots (created_at);

-- orders — 주문 원장 (idempotency + 상태머신).
CREATE TABLE IF NOT EXISTS portfolio.orders (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id      BIGINT REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    client_order_id TEXT NOT NULL,
    payload_hash    TEXT NOT NULL,
    mode            TEXT NOT NULL,
    ticker          TEXT,
    side            TEXT,                                -- buy | sell
    qty             numeric,
    order_type      TEXT,                                -- limit | market
    limit_price     numeric,
    broker_order_id TEXT,
    status          TEXT NOT NULL DEFAULT 'created',
    reason          TEXT,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_orders_client_order_id UNIQUE (client_order_id),
    CONSTRAINT chk_orders_status CHECK (status IN (
        'created','submitting','submitted','in_doubt','partial',
        'filled','rejected','canceled','aborted'))
);
CREATE INDEX IF NOT EXISTS idx_orders_account    ON portfolio.orders (account_id);
CREATE INDEX IF NOT EXISTS idx_orders_status     ON portfolio.orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON portfolio.orders (created_at);

-- order_events — 주문 상태 전이/브로커 이벤트 이력 (append-only).
CREATE TABLE IF NOT EXISTS portfolio.order_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id  BIGINT REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    order_id    BIGINT NOT NULL REFERENCES portfolio.orders(id) ON DELETE RESTRICT,
    event_type  TEXT NOT NULL,                           -- submit | ack | fill | partial_fill | reject | cancel | abort
    status      TEXT,
    filled_qty  numeric,
    filled_price numeric,
    broker_ref  TEXT,
    payload_json jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_order_events_account    ON portfolio.order_events (account_id);
CREATE INDEX IF NOT EXISTS idx_order_events_order      ON portfolio.order_events (order_id);
CREATE INDEX IF NOT EXISTS idx_order_events_created_at ON portfolio.order_events (created_at);

-- ============================================================================
-- STRATEGY (전제 — 프로필/정책/배분안/선택)
-- ============================================================================

-- investor_profiles — 대전제(운용 방식) + 중전제(관심/생각). 계좌별.
CREATE TABLE IF NOT EXISTS portfolio.investor_profiles (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id         BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    posture_text       TEXT,                             -- "어떻게 운용하고 싶은가" 자유입력
    risk_tolerance     TEXT,                             -- aggressive | neutral | defensive
    short_policy       TEXT,                             -- none | insurance | active
    cash_min_pct       numeric,
    cash_max_pct       numeric,
    horizon            TEXT,
    interests_text     TEXT,
    views_text         TEXT,
    individual_cap_pct numeric,
    individual_count   INTEGER,
    region_pref        TEXT,
    rebalance_pace     TEXT,                             -- slow | normal | fast
    doc_json           jsonb,                            -- 유연한 자유 문서 (키워드/지역분배/노트/lesson 참조)
    refined_by         TEXT,                             -- claude_agent | user
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_investor_profiles_account UNIQUE (account_id),
    CONSTRAINT chk_profile_cash_min CHECK (cash_min_pct IS NULL OR (cash_min_pct >= 0 AND cash_min_pct <= 100)),
    CONSTRAINT chk_profile_cash_max CHECK (cash_max_pct IS NULL OR (cash_max_pct >= 0 AND cash_max_pct <= 100)),
    CONSTRAINT chk_profile_indiv_cap CHECK (individual_cap_pct IS NULL OR (individual_cap_pct >= 0 AND individual_cap_pct <= 100))
);
CREATE INDEX IF NOT EXISTS idx_inv_profiles_account    ON portfolio.investor_profiles (account_id);
CREATE INDEX IF NOT EXISTS idx_inv_profiles_created_at ON portfolio.investor_profiles (created_at);
CREATE INDEX IF NOT EXISTS idx_inv_profiles_doc        ON portfolio.investor_profiles USING GIN (doc_json);

-- investor_profile_versions — 프로필 변경 이력 (append-only). SQLite investor_profile_history 승격.
CREATE TABLE IF NOT EXISTS portfolio.investor_profile_versions (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id    BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    profile_id    BIGINT REFERENCES portfolio.investor_profiles(id) ON DELETE RESTRICT,
    version       INTEGER,
    snapshot_json jsonb NOT NULL,                        -- 저장 시점 프로필 전체
    source        TEXT,                                  -- user | claude_agent | distill
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_inv_prof_ver_account    ON portfolio.investor_profile_versions (account_id);
CREATE INDEX IF NOT EXISTS idx_inv_prof_ver_created_at ON portfolio.investor_profile_versions (created_at);
CREATE INDEX IF NOT EXISTS idx_inv_prof_ver_snapshot   ON portfolio.investor_profile_versions USING GIN (snapshot_json);

-- portfolio_policies — 컴파일된 투자 정책 객체 (profile → policy). 버전 관리.
-- 핫 컬럼 승격 + 유연 jsonb 보존 (하이브리드).
CREATE TABLE IF NOT EXISTS portfolio.portfolio_policies (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id              BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    policy_version_id       BIGINT,                       -- 동일 정책 객체의 버전 식별
    version                 INTEGER NOT NULL DEFAULT 1,
    cash_target_pct         numeric,
    cash_min_pct            numeric,
    cash_max_pct            numeric,
    pace                    TEXT,                          -- slow | normal | fast
    single_position_max_pct numeric,
    sector_max_pct          numeric,
    inverse_max_pct         numeric,
    leverage_max_pct        numeric,
    policy_json             jsonb,                         -- 전체 정책 객체
    region_targets_json     jsonb,                         -- 지역 목표 분배
    bond_policy_json        jsonb,                         -- 채권 정책
    extracted_variables_json jsonb,                        -- 추출 변수
    source                  TEXT,                          -- user | claude_agent
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_pol_cash_target  CHECK (cash_target_pct IS NULL OR (cash_target_pct >= 0 AND cash_target_pct <= 100)),
    CONSTRAINT chk_pol_cash_min     CHECK (cash_min_pct IS NULL OR (cash_min_pct >= 0 AND cash_min_pct <= 100)),
    CONSTRAINT chk_pol_cash_max     CHECK (cash_max_pct IS NULL OR (cash_max_pct >= 0 AND cash_max_pct <= 100)),
    CONSTRAINT chk_pol_single_max   CHECK (single_position_max_pct IS NULL OR (single_position_max_pct >= 0 AND single_position_max_pct <= 100)),
    CONSTRAINT chk_pol_sector_max   CHECK (sector_max_pct IS NULL OR (sector_max_pct >= 0 AND sector_max_pct <= 100)),
    CONSTRAINT chk_pol_inverse_max  CHECK (inverse_max_pct IS NULL OR (inverse_max_pct >= 0 AND inverse_max_pct <= 100)),
    CONSTRAINT chk_pol_leverage_max CHECK (leverage_max_pct IS NULL OR (leverage_max_pct >= 0 AND leverage_max_pct <= 100))
);
CREATE INDEX IF NOT EXISTS idx_pol_account    ON portfolio.portfolio_policies (account_id);
CREATE INDEX IF NOT EXISTS idx_pol_created_at ON portfolio.portfolio_policies (created_at);
CREATE INDEX IF NOT EXISTS idx_pol_version    ON portfolio.portfolio_policies (account_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_pol_policy_json ON portfolio.portfolio_policies USING GIN (policy_json);

-- target_allocations — anchor+tilt 목표비중 제안 (3안 묶음). SQLite target_allocations 승격.
CREATE TABLE IF NOT EXISTS portfolio.target_allocations (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id  BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    proposal_id TEXT NOT NULL,                            -- 1회 생성 = 동일 proposal_id (3 variant 묶음)
    variant     TEXT NOT NULL,                            -- conservative | base | aggressive
    kind        TEXT NOT NULL,                            -- cash | anchor | tilt
    ref         TEXT,                                     -- 테마/섹터/자산군 (cash 는 NULL)
    weight_pct  numeric NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft',            -- draft | chosen | archived
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_target_alloc_weight CHECK (weight_pct >= 0 AND weight_pct <= 100)
);
CREATE INDEX IF NOT EXISTS idx_target_alloc_account    ON portfolio.target_allocations (account_id);
CREATE INDEX IF NOT EXISTS idx_target_alloc_created_at ON portfolio.target_allocations (created_at);
CREATE INDEX IF NOT EXISTS idx_target_alloc_proposal   ON portfolio.target_allocations (account_id, proposal_id, variant);

-- allocation_options — 생성된 배분 옵션(3안) 메타 헤더 (proposal 단위 1행/variant).
CREATE TABLE IF NOT EXISTS portfolio.allocation_options (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id        BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    policy_version_id BIGINT,
    proposal_id       TEXT NOT NULL,
    variant           TEXT NOT NULL,                      -- conservative | base | aggressive
    label             TEXT,
    expected_drift_pct numeric,
    allocation_json   jsonb,                              -- 옵션 비중 본문
    rationale_json    jsonb,                              -- 근거/설명
    status            TEXT NOT NULL DEFAULT 'draft',      -- draft | chosen | archived
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_alloc_opt_drift CHECK (expected_drift_pct IS NULL OR (expected_drift_pct >= 0 AND expected_drift_pct <= 100))
);
CREATE INDEX IF NOT EXISTS idx_alloc_opt_account    ON portfolio.allocation_options (account_id);
CREATE INDEX IF NOT EXISTS idx_alloc_opt_created_at ON portfolio.allocation_options (created_at);
CREATE INDEX IF NOT EXISTS idx_alloc_opt_proposal   ON portfolio.allocation_options (account_id, proposal_id);
CREATE INDEX IF NOT EXISTS idx_alloc_opt_alloc_json ON portfolio.allocation_options USING GIN (allocation_json);

-- selected_allocations — 사람이 확정한 공식 target allocation (append-only 이력 + provenance).
-- SQLite allocation_selections 승격. 재선택/취소는 status 만, 삭제 금지.
CREATE TABLE IF NOT EXISTS portfolio.selected_allocations (
    id                           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id                   BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    proposal_id                  TEXT,
    variant                      TEXT,                    -- conservative | base | aggressive | custom
    allocation_json              jsonb NOT NULL,          -- 확정 비중 [{kind,ref,weight_pct}]
    policy_version_id            BIGINT,                  -- provenance: 어느 정책 버전으로
    account_snapshot_id          BIGINT REFERENCES portfolio.account_snapshots(id) ON DELETE RESTRICT,
    expected_drift_pct           numeric,
    expected_rebalance_total_krw numeric,
    expected_rebalance_rounds    INTEGER,
    precheck_status              TEXT,                    -- pass | warn | block
    precheck_reasons_json        jsonb,
    selected_by                  TEXT,
    user_override                BOOLEAN NOT NULL DEFAULT FALSE,
    diff_json                    jsonb,                   -- 이전 선택 대비 변경
    status                       TEXT NOT NULL DEFAULT 'active',  -- active | superseded | cancelled
    selected_at                  timestamptz NOT NULL DEFAULT now(),
    created_at                   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_sel_alloc_drift CHECK (expected_drift_pct IS NULL OR (expected_drift_pct >= 0 AND expected_drift_pct <= 100))
);
CREATE INDEX IF NOT EXISTS idx_sel_alloc_account    ON portfolio.selected_allocations (account_id);
CREATE INDEX IF NOT EXISTS idx_sel_alloc_created_at ON portfolio.selected_allocations (created_at);
CREATE INDEX IF NOT EXISTS idx_sel_alloc_status     ON portfolio.selected_allocations (account_id, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_sel_alloc_json       ON portfolio.selected_allocations USING GIN (allocation_json);

-- strategy_change_events — 대전제/중전제/정책 변경 이벤트 (append-only 감사).
CREATE TABLE IF NOT EXISTS portfolio.strategy_change_events (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id   BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    layer        TEXT NOT NULL,                           -- grand | mid | small | policy
    change_type  TEXT,                                    -- create | update | supersede | cancel
    before_json  jsonb,
    after_json   jsonb,
    source       TEXT,                                    -- user | claude_agent | rule
    note         TEXT,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_strat_change_account    ON portfolio.strategy_change_events (account_id);
CREATE INDEX IF NOT EXISTS idx_strat_change_created_at ON portfolio.strategy_change_events (created_at);
CREATE INDEX IF NOT EXISTS idx_strat_change_after      ON portfolio.strategy_change_events USING GIN (after_json);

-- ============================================================================
-- DECISION (의사결정 — 결정/리밸런싱 계획/리스크)
-- ============================================================================

-- portfolio_decisions — 의사결정 스냅샷 (현재비중 vs 목표 → drift → 제안 → 리스크).
-- SQLite decisions 승격.
CREATE TABLE IF NOT EXISTS portfolio.portfolio_decisions (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id            BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    selected_allocation_id BIGINT REFERENCES portfolio.selected_allocations(id) ON DELETE RESTRICT,
    account_snapshot_id   BIGINT REFERENCES portfolio.account_snapshots(id) ON DELETE RESTRICT,
    payload_json          jsonb NOT NULL,                 -- total/cash/lines/risk
    drift_pct             numeric,
    risk_reasons_json     jsonb,
    passed                BOOLEAN,
    created_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_decision_drift CHECK (drift_pct IS NULL OR (drift_pct >= 0 AND drift_pct <= 100))
);
CREATE INDEX IF NOT EXISTS idx_decision_account    ON portfolio.portfolio_decisions (account_id);
CREATE INDEX IF NOT EXISTS idx_decision_created_at ON portfolio.portfolio_decisions (created_at);
CREATE INDEX IF NOT EXISTS idx_decision_payload    ON portfolio.portfolio_decisions USING GIN (payload_json);

-- rebalance_plans — 회차 단위 리밸런싱 계획 (decision 1회 = plan 1개).
CREATE TABLE IF NOT EXISTS portfolio.rebalance_plans (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id   BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    decision_id  BIGINT REFERENCES portfolio.portfolio_decisions(id) ON DELETE RESTRICT,
    pace         TEXT,
    summary_json jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rebal_plan_account    ON portfolio.rebalance_plans (account_id);
CREATE INDEX IF NOT EXISTS idx_rebal_plan_created_at ON portfolio.rebalance_plans (created_at);
CREATE INDEX IF NOT EXISTS idx_rebal_plan_decision   ON portfolio.rebalance_plans (decision_id);

-- rebalance_plan_steps — 계획 내 종목별 단계.
CREATE TABLE IF NOT EXISTS portfolio.rebalance_plan_steps (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id    BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    plan_id       BIGINT NOT NULL REFERENCES portfolio.rebalance_plans(id) ON DELETE RESTRICT,
    ticker        TEXT NOT NULL,
    direction     TEXT,                                   -- buy | sell
    total_pct     numeric,
    total_krw     numeric,
    cycle_pct     numeric,
    cycle_krw     numeric,
    cycle_qty     numeric,
    remaining_pct numeric,
    round_no      INTEGER,
    total_rounds  INTEGER,
    limit_price   numeric,
    status        TEXT,                                   -- candidate | hold | blocked
    reason        TEXT,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_step_total_pct     CHECK (total_pct IS NULL OR (total_pct >= 0 AND total_pct <= 100)),
    CONSTRAINT chk_step_cycle_pct     CHECK (cycle_pct IS NULL OR (cycle_pct >= 0 AND cycle_pct <= 100)),
    CONSTRAINT chk_step_remaining_pct CHECK (remaining_pct IS NULL OR (remaining_pct >= 0 AND remaining_pct <= 100))
);
CREATE INDEX IF NOT EXISTS idx_rebal_step_account    ON portfolio.rebalance_plan_steps (account_id);
CREATE INDEX IF NOT EXISTS idx_rebal_step_created_at ON portfolio.rebalance_plan_steps (created_at);
CREATE INDEX IF NOT EXISTS idx_rebal_step_plan       ON portfolio.rebalance_plan_steps (plan_id);

-- risk_checks — 주문 전 리스크 게이트 결과 (hard-block 근거).
CREATE TABLE IF NOT EXISTS portfolio.risk_checks (
    id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id             BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    decision_id            BIGINT REFERENCES portfolio.portfolio_decisions(id) ON DELETE RESTRICT,
    selected_allocation_id BIGINT REFERENCES portfolio.selected_allocations(id) ON DELETE RESTRICT,
    account_snapshot_id    BIGINT REFERENCES portfolio.account_snapshots(id) ON DELETE RESTRICT,
    passed                 BOOLEAN NOT NULL,
    risk_reasons_json      jsonb,                          -- [{name,ok,detail}]
    created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_risk_check_account    ON portfolio.risk_checks (account_id);
CREATE INDEX IF NOT EXISTS idx_risk_check_created_at ON portfolio.risk_checks (created_at);
CREATE INDEX IF NOT EXISTS idx_risk_check_decision   ON portfolio.risk_checks (decision_id);
CREATE INDEX IF NOT EXISTS idx_risk_check_reasons    ON portfolio.risk_checks USING GIN (risk_reasons_json);

-- ============================================================================
-- GROWTH (성장 — 상담/교훈/메모리/훅)
-- "메모리로 성장하는 에이전트" substrate. Anthropic API 미사용.
-- ============================================================================

-- consultations — Claude 분석 전문가에게 조언 구하기 (append-only 로그).
CREATE TABLE IF NOT EXISTS portfolio.consultations (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    question   TEXT NOT NULL,
    answer     TEXT,
    refs_json  jsonb,                                      -- 인용 메모리(lessons)
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_consult_account    ON portfolio.consultations (account_id);
CREATE INDEX IF NOT EXISTS idx_consult_created_at ON portfolio.consultations (created_at);

-- lesson_candidates — lesson 후보 (승격 전 관찰). SQLite lesson_candidates 승격.
CREATE TABLE IF NOT EXISTS portfolio.lesson_candidates (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id     BIGINT REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    scope          TEXT NOT NULL,                          -- market|economy|sector|instrument|premise|decision|risk
    ref            TEXT,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    evidence_ref   TEXT,
    observed_count INTEGER NOT NULL DEFAULT 1,
    outcome        TEXT,
    confidence     numeric DEFAULT 0.0,
    status         TEXT NOT NULL DEFAULT 'candidate',      -- candidate | promoted | rejected
    source         TEXT,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_less_cand_account    ON portfolio.lesson_candidates (account_id);
CREATE INDEX IF NOT EXISTS idx_less_cand_created_at ON portfolio.lesson_candidates (created_at);
CREATE INDEX IF NOT EXISTS idx_less_cand_scope      ON portfolio.lesson_candidates (scope, ref, status);

-- lessons — 성장 토대. 시장/경제/섹터/종목/전제/결정 분석 누적·재사용.
CREATE TABLE IF NOT EXISTS portfolio.lessons (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id      BIGINT REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,  -- NULL = 전역 교훈
    candidate_id    BIGINT REFERENCES portfolio.lesson_candidates(id) ON DELETE RESTRICT,
    scope           TEXT NOT NULL,                          -- market|economy|sector|instrument|premise|decision
    ref             TEXT,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    confidence      numeric,                                -- 0~1 (반복 검증되며 성장)
    source          TEXT,                                   -- claude_agent | user | outcome
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lessons_account    ON portfolio.lessons (account_id);
CREATE INDEX IF NOT EXISTS idx_lessons_created_at ON portfolio.lessons (created_at);
CREATE INDEX IF NOT EXISTS idx_lessons_scope      ON portfolio.lessons (scope, ref, id DESC);

-- agent_memories — Agent별 memory scope 레지스트리 (prehook 검색 대상). SQLite agent_memory_scope 승격.
CREATE TABLE IF NOT EXISTS portfolio.agent_memories (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id BIGINT REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    agent      TEXT NOT NULL,                              -- agent slug
    scope      TEXT NOT NULL,                              -- lessons.scope
    priority   INTEGER NOT NULL DEFAULT 100,               -- 작을수록 prehook 우선
    note       TEXT,
    props_json jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_agent_memories_agent_scope UNIQUE (agent, scope)
);
CREATE INDEX IF NOT EXISTS idx_agent_mem_account    ON portfolio.agent_memories (account_id);
CREATE INDEX IF NOT EXISTS idx_agent_mem_created_at ON portfolio.agent_memories (created_at);
CREATE INDEX IF NOT EXISTS idx_agent_mem_agent      ON portfolio.agent_memories (agent, priority);

-- task_memories — task ↔ memory provenance (prehook이 실제 참조한 것). SQLite task_memory_links 승격.
CREATE TABLE IF NOT EXISTS portfolio.task_memories (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id  BIGINT REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    task_id     BIGINT,
    memory_kind TEXT NOT NULL,                             -- lesson|lesson_candidate|evidence|feedback|policy|selected_allocation|snapshot
    memory_id   BIGINT,
    scope       TEXT,
    ref         TEXT,
    relevance   numeric,                                   -- decay-가중 점수
    note        TEXT,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_task_mem_account    ON portfolio.task_memories (account_id);
CREATE INDEX IF NOT EXISTS idx_task_mem_created_at ON portfolio.task_memories (created_at);
CREATE INDEX IF NOT EXISTS idx_task_mem_task       ON portfolio.task_memories (task_id);
CREATE INDEX IF NOT EXISTS idx_task_mem_mem        ON portfolio.task_memories (memory_kind, memory_id);

-- memory_retrieval_logs — prehook 메모리 검색 이력 (무엇을 어떤 점수로 불러왔는가).
CREATE TABLE IF NOT EXISTS portfolio.memory_retrieval_logs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id    BIGINT REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    task_id       BIGINT,
    agent         TEXT,
    query_text    TEXT,
    scope         TEXT,
    ref           TEXT,
    retrieved_count INTEGER,
    results_json  jsonb,                                   -- [{memory_kind,memory_id,score}]
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mem_retr_account    ON portfolio.memory_retrieval_logs (account_id);
CREATE INDEX IF NOT EXISTS idx_mem_retr_created_at ON portfolio.memory_retrieval_logs (created_at);
CREATE INDEX IF NOT EXISTS idx_mem_retr_task       ON portfolio.memory_retrieval_logs (task_id);
CREATE INDEX IF NOT EXISTS idx_mem_retr_results    ON portfolio.memory_retrieval_logs USING GIN (results_json);

-- prehook_runs — 작업 전 확인(gate/메모리) 실행 기록.
CREATE TABLE IF NOT EXISTS portfolio.prehook_runs (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id   BIGINT REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    task_id      BIGINT,
    agent        TEXT,
    gate         TEXT,                                     -- pass | block
    context_json jsonb,                                    -- {checks:[{name,ok,detail}], memory_count, reasons:[]}
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_prehook_account    ON portfolio.prehook_runs (account_id);
CREATE INDEX IF NOT EXISTS idx_prehook_created_at ON portfolio.prehook_runs (created_at);
CREATE INDEX IF NOT EXISTS idx_prehook_task       ON portfolio.prehook_runs (task_id);
CREATE INDEX IF NOT EXISTS idx_prehook_context    ON portfolio.prehook_runs USING GIN (context_json);

-- posthook_runs — 작업 후 정리(배운 점/다음 액션) 실행 기록.
CREATE TABLE IF NOT EXISTS portfolio.posthook_runs (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id   BIGINT REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    task_id      BIGINT,
    agent        TEXT,
    gate         TEXT,                                     -- done | blocked | failed
    context_json jsonb,                                    -- {outcome, next_action, unresolved_risk, lessons:[]}
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_posthook_account    ON portfolio.posthook_runs (account_id);
CREATE INDEX IF NOT EXISTS idx_posthook_created_at ON portfolio.posthook_runs (created_at);
CREATE INDEX IF NOT EXISTS idx_posthook_task       ON portfolio.posthook_runs (task_id);
CREATE INDEX IF NOT EXISTS idx_posthook_context    ON portfolio.posthook_runs USING GIN (context_json);

-- ============================================================================
-- EVIDENCE (근거 — 문서/링크/리서치). Vector DB 승격 prep.
-- ============================================================================

-- evidence_documents — 근거 문서 (RDB 메타 + 임베딩 상태). Vector 승격 prep.
CREATE TABLE IF NOT EXISTS portfolio.evidence_documents (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id       BIGINT REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    scope            TEXT,                                  -- news|disclosure|report|fundamental|dividend
    ref              TEXT,                                  -- 종목/테마
    source_type      TEXT,
    title            TEXT,
    body             TEXT,
    url              TEXT,
    freshness_at     timestamptz,
    confidence       numeric,
    affected_theme   TEXT,
    affected_asset   TEXT,
    embedding_status TEXT DEFAULT 'pending',                -- pending | embedded | failed
    embedding_model  TEXT,
    embedding_ref    TEXT,                                  -- Vector store 외부 ref
    metadata_json    jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_evidence_account    ON portfolio.evidence_documents (account_id);
CREATE INDEX IF NOT EXISTS idx_evidence_created_at ON portfolio.evidence_documents (created_at);
CREATE INDEX IF NOT EXISTS idx_evidence_scope      ON portfolio.evidence_documents (scope, ref);
CREATE INDEX IF NOT EXISTS idx_evidence_embed      ON portfolio.evidence_documents (embedding_status);
CREATE INDEX IF NOT EXISTS idx_evidence_metadata   ON portfolio.evidence_documents USING GIN (metadata_json);

-- decision_evidence_links — decision ↔ evidence 링크 (어떤 근거가 어떤 비중변경으로).
CREATE TABLE IF NOT EXISTS portfolio.decision_evidence_links (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id    BIGINT REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    decision_id   BIGINT NOT NULL REFERENCES portfolio.portfolio_decisions(id) ON DELETE RESTRICT,
    evidence_id   BIGINT NOT NULL REFERENCES portfolio.evidence_documents(id) ON DELETE RESTRICT,
    weight_change numeric,
    note          TEXT,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dec_ev_account    ON portfolio.decision_evidence_links (account_id);
CREATE INDEX IF NOT EXISTS idx_dec_ev_created_at ON portfolio.decision_evidence_links (created_at);
CREATE INDEX IF NOT EXISTS idx_dec_ev_decision   ON portfolio.decision_evidence_links (decision_id);
CREATE INDEX IF NOT EXISTS idx_dec_ev_evidence   ON portfolio.decision_evidence_links (evidence_id);

-- research_runs — Claude 외부 자료조사 실행 기록 (질문→수집→요약).
CREATE TABLE IF NOT EXISTS portfolio.research_runs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id    BIGINT REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    agent         TEXT,
    topic         TEXT,
    query_text    TEXT,
    summary       TEXT,
    findings_json jsonb,                                    -- [{title,url,evidence_id,confidence}]
    status        TEXT NOT NULL DEFAULT 'done',             -- done | pending | failed
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_research_account    ON portfolio.research_runs (account_id);
CREATE INDEX IF NOT EXISTS idx_research_created_at ON portfolio.research_runs (created_at);
CREATE INDEX IF NOT EXISTS idx_research_findings   ON portfolio.research_runs USING GIN (findings_json);

-- ============================================================================
-- DASHBOARD / HISTORY (대시보드 — 일별 스냅샷/drift/배분 이력/지표)
-- ============================================================================

-- account_daily_snapshots — 계좌 일별 스냅샷.
CREATE TABLE IF NOT EXISTS portfolio.account_daily_snapshots (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id      BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    snapshot_date   date NOT NULL,
    cash_krw        numeric,
    total_value_krw numeric,
    holdings_count  INTEGER,
    pnl_krw         numeric,
    pnl_pct         numeric,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_acct_daily UNIQUE (account_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_acct_daily_account    ON portfolio.account_daily_snapshots (account_id);
CREATE INDEX IF NOT EXISTS idx_acct_daily_created_at ON portfolio.account_daily_snapshots (created_at);
CREATE INDEX IF NOT EXISTS idx_acct_daily_date       ON portfolio.account_daily_snapshots (account_id, snapshot_date DESC);

-- position_daily_snapshots — 종목 일별 스냅샷.
CREATE TABLE IF NOT EXISTS portfolio.position_daily_snapshots (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id    BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    snapshot_date date NOT NULL,
    ticker        TEXT NOT NULL,
    name          TEXT,
    qty           numeric,
    avg_price     numeric,
    market_value  numeric,
    weight_pct    numeric,
    currency      TEXT NOT NULL DEFAULT 'KRW',
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_pos_daily UNIQUE (account_id, snapshot_date, ticker),
    CONSTRAINT chk_pos_daily_weight CHECK (weight_pct IS NULL OR (weight_pct >= 0 AND weight_pct <= 100))
);
CREATE INDEX IF NOT EXISTS idx_pos_daily_account    ON portfolio.position_daily_snapshots (account_id);
CREATE INDEX IF NOT EXISTS idx_pos_daily_created_at ON portfolio.position_daily_snapshots (created_at);
CREATE INDEX IF NOT EXISTS idx_pos_daily_date       ON portfolio.position_daily_snapshots (account_id, snapshot_date DESC);

-- portfolio_drift_history — 목표 대비 drift 이력.
CREATE TABLE IF NOT EXISTS portfolio.portfolio_drift_history (
    id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id             BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    selected_allocation_id BIGINT REFERENCES portfolio.selected_allocations(id) ON DELETE RESTRICT,
    snapshot_date          date NOT NULL,
    total_drift_pct        numeric,
    max_line_drift_pct     numeric,
    drift_lines_json       jsonb,                           -- [{ref,target_pct,current_pct,drift_pct}]
    created_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_drift_hist UNIQUE (account_id, snapshot_date),
    CONSTRAINT chk_drift_total CHECK (total_drift_pct IS NULL OR (total_drift_pct >= 0 AND total_drift_pct <= 100)),
    CONSTRAINT chk_drift_max   CHECK (max_line_drift_pct IS NULL OR (max_line_drift_pct >= 0 AND max_line_drift_pct <= 100))
);
CREATE INDEX IF NOT EXISTS idx_drift_hist_account    ON portfolio.portfolio_drift_history (account_id);
CREATE INDEX IF NOT EXISTS idx_drift_hist_created_at ON portfolio.portfolio_drift_history (created_at);
CREATE INDEX IF NOT EXISTS idx_drift_hist_date       ON portfolio.portfolio_drift_history (account_id, snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_drift_hist_lines      ON portfolio.portfolio_drift_history USING GIN (drift_lines_json);

-- allocation_history — 확정 배분 변경 이력 (대시보드 timeline).
CREATE TABLE IF NOT EXISTS portfolio.allocation_history (
    id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id             BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    selected_allocation_id BIGINT REFERENCES portfolio.selected_allocations(id) ON DELETE RESTRICT,
    effective_date         date,
    allocation_json        jsonb,
    diff_json              jsonb,
    created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_alloc_hist_account    ON portfolio.allocation_history (account_id);
CREATE INDEX IF NOT EXISTS idx_alloc_hist_created_at ON portfolio.allocation_history (created_at);
CREATE INDEX IF NOT EXISTS idx_alloc_hist_alloc      ON portfolio.allocation_history USING GIN (allocation_json);

-- dashboard_metrics — 사전 계산된 대시보드 지표 (계좌×지표×시점).
CREATE TABLE IF NOT EXISTS portfolio.dashboard_metrics (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id   BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    metric_key   TEXT NOT NULL,                            -- pnl_30d | sharpe | cash_pct | ...
    metric_date  date NOT NULL,
    value_num    numeric,
    value_json   jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_dash_metric UNIQUE (account_id, metric_key, metric_date)
);
CREATE INDEX IF NOT EXISTS idx_dash_metric_account    ON portfolio.dashboard_metrics (account_id);
CREATE INDEX IF NOT EXISTS idx_dash_metric_created_at ON portfolio.dashboard_metrics (created_at);
CREATE INDEX IF NOT EXISTS idx_dash_metric_key        ON portfolio.dashboard_metrics (account_id, metric_key, metric_date DESC);
CREATE INDEX IF NOT EXISTS idx_dash_metric_json       ON portfolio.dashboard_metrics USING GIN (value_json);

-- ============================================================================
-- GRAPH EDGES (그래프 승격 prep — 일반 엣지 형태)
-- 공통 패턴: (src_type, src_id) → (dst_type, dst_id), edge_kind, weight, props_json.
-- ============================================================================

-- account_asset_edges — 계좌 → 자산 보유 엣지.
CREATE TABLE IF NOT EXISTS portfolio.account_asset_edges (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id   BIGINT NOT NULL REFERENCES portfolio.accounts(id) ON DELETE RESTRICT,
    asset_symbol TEXT NOT NULL,
    edge_kind    TEXT NOT NULL DEFAULT 'holds',           -- holds | targets | watches
    weight_pct   numeric,
    props_json   jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_acct_asset_weight CHECK (weight_pct IS NULL OR (weight_pct >= 0 AND weight_pct <= 100))
);
CREATE INDEX IF NOT EXISTS idx_acct_asset_account    ON portfolio.account_asset_edges (account_id);
CREATE INDEX IF NOT EXISTS idx_acct_asset_created_at ON portfolio.account_asset_edges (created_at);
CREATE INDEX IF NOT EXISTS idx_acct_asset_symbol     ON portfolio.account_asset_edges (asset_symbol);
CREATE INDEX IF NOT EXISTS idx_acct_asset_props      ON portfolio.account_asset_edges USING GIN (props_json);

-- asset_theme_edges — 자산 → 테마 엣지.
CREATE TABLE IF NOT EXISTS portfolio.asset_theme_edges (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_symbol TEXT NOT NULL,
    theme        TEXT NOT NULL,
    edge_kind    TEXT NOT NULL DEFAULT 'belongs_to',
    weight       numeric,
    props_json   jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_asset_theme_asset      ON portfolio.asset_theme_edges (asset_symbol);
CREATE INDEX IF NOT EXISTS idx_asset_theme_theme      ON portfolio.asset_theme_edges (theme);
CREATE INDEX IF NOT EXISTS idx_asset_theme_created_at ON portfolio.asset_theme_edges (created_at);
CREATE INDEX IF NOT EXISTS idx_asset_theme_props      ON portfolio.asset_theme_edges USING GIN (props_json);

-- asset_sector_edges — 자산 → 섹터 엣지.
CREATE TABLE IF NOT EXISTS portfolio.asset_sector_edges (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asset_symbol TEXT NOT NULL,
    sector       TEXT NOT NULL,
    edge_kind    TEXT NOT NULL DEFAULT 'belongs_to',
    weight       numeric,
    props_json   jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_asset_sector_asset      ON portfolio.asset_sector_edges (asset_symbol);
CREATE INDEX IF NOT EXISTS idx_asset_sector_sector     ON portfolio.asset_sector_edges (sector);
CREATE INDEX IF NOT EXISTS idx_asset_sector_created_at ON portfolio.asset_sector_edges (created_at);
CREATE INDEX IF NOT EXISTS idx_asset_sector_props      ON portfolio.asset_sector_edges USING GIN (props_json);

-- etf_holding_edges — ETF → 구성종목 엣지.
CREATE TABLE IF NOT EXISTS portfolio.etf_holding_edges (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    etf_symbol      TEXT NOT NULL,
    holding_symbol  TEXT NOT NULL,
    edge_kind       TEXT NOT NULL DEFAULT 'holds',
    weight_pct      numeric,
    props_json      jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_etf_hold_weight CHECK (weight_pct IS NULL OR (weight_pct >= 0 AND weight_pct <= 100))
);
CREATE INDEX IF NOT EXISTS idx_etf_hold_etf        ON portfolio.etf_holding_edges (etf_symbol);
CREATE INDEX IF NOT EXISTS idx_etf_hold_holding    ON portfolio.etf_holding_edges (holding_symbol);
CREATE INDEX IF NOT EXISTS idx_etf_hold_created_at ON portfolio.etf_holding_edges (created_at);
CREATE INDEX IF NOT EXISTS idx_etf_hold_props      ON portfolio.etf_holding_edges USING GIN (props_json);

-- decision_evidence_edges — 결정 → 근거 엣지 (그래프 형태).
CREATE TABLE IF NOT EXISTS portfolio.decision_evidence_edges (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    src_type    TEXT NOT NULL DEFAULT 'decision',
    src_id      BIGINT NOT NULL,
    dst_type    TEXT NOT NULL DEFAULT 'evidence',
    dst_id      BIGINT NOT NULL,
    edge_kind   TEXT NOT NULL DEFAULT 'supported_by',
    weight      numeric,
    props_json  jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dec_ev_edge_src        ON portfolio.decision_evidence_edges (src_type, src_id);
CREATE INDEX IF NOT EXISTS idx_dec_ev_edge_dst        ON portfolio.decision_evidence_edges (dst_type, dst_id);
CREATE INDEX IF NOT EXISTS idx_dec_ev_edge_created_at ON portfolio.decision_evidence_edges (created_at);
CREATE INDEX IF NOT EXISTS idx_dec_ev_edge_props      ON portfolio.decision_evidence_edges USING GIN (props_json);

-- decision_risk_edges — 결정 → 리스크체크 엣지 (그래프 형태).
CREATE TABLE IF NOT EXISTS portfolio.decision_risk_edges (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    src_type    TEXT NOT NULL DEFAULT 'decision',
    src_id      BIGINT NOT NULL,
    dst_type    TEXT NOT NULL DEFAULT 'risk_check',
    dst_id      BIGINT NOT NULL,
    edge_kind   TEXT NOT NULL DEFAULT 'gated_by',
    weight      numeric,
    props_json  jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dec_risk_edge_src        ON portfolio.decision_risk_edges (src_type, src_id);
CREATE INDEX IF NOT EXISTS idx_dec_risk_edge_dst        ON portfolio.decision_risk_edges (dst_type, dst_id);
CREATE INDEX IF NOT EXISTS idx_dec_risk_edge_created_at ON portfolio.decision_risk_edges (created_at);
CREATE INDEX IF NOT EXISTS idx_dec_risk_edge_props      ON portfolio.decision_risk_edges USING GIN (props_json);

-- ============================================================================
-- WORKFLOW CONTEXT — (prehook_runs / posthook_runs 는 GROWTH 섹션에 정의됨)
-- task_id / agent / gate / context_json 형태로 workflow provenance 를 남긴다.
-- ============================================================================

-- end of 100_core.sql
