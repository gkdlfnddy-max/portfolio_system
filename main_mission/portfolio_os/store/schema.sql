-- Portfolio OS — 로컬 SQLite (data/portfolio.sqlite3) = 운영 truth.
-- 흐름: KIS/외부 → Python sync job → 본 DB → Web API(node:sqlite 조회) → UI.
-- 자격증명(키/시크릿/토큰/평문 계좌번호)은 절대 저장하지 않음 (.env 전용).

-- ============================================================
-- 계좌 메타 (.env 의 KIS_ACCOUNT_{n}_* 를 sync job 이 미러)
-- ============================================================
CREATE TABLE IF NOT EXISTS accounts (
    account_index    INTEGER PRIMARY KEY,     -- .env KIS_ACCOUNT_{n}
    alias            TEXT,
    mode             TEXT,                    -- paper | live | mock
    account_no_masked TEXT,                   -- 앞2자리+마스킹 (평문 금지)
    has_credentials  INTEGER NOT NULL DEFAULT 0,
    token_status     TEXT,                    -- ok | error | unknown
    sync_status      TEXT,                    -- ok | error | never
    last_error       TEXT,
    last_synced_at   TEXT,                    -- UTC ISO8601
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- 잔고 스냅샷 (계좌×시점) — 금액 truth
-- ============================================================
CREATE TABLE IF NOT EXISTS account_snapshots (
    id              INTEGER PRIMARY KEY,
    account_index   INTEGER NOT NULL,
    cash_krw        REAL,
    total_value_krw REAL,
    holdings_count  INTEGER,
    fx_rate         REAL,
    source          TEXT,                     -- kis_live | kis_paper | manual_sync
    is_stale        INTEGER NOT NULL DEFAULT 0,
    captured_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_snap_account ON account_snapshots(account_index, captured_at DESC);

-- ============================================================
-- 보유종목 (스냅샷 행 단위)
-- ============================================================
CREATE TABLE IF NOT EXISTS holdings (
    id            INTEGER PRIMARY KEY,
    snapshot_id   INTEGER NOT NULL REFERENCES account_snapshots(id) ON DELETE CASCADE,
    account_index INTEGER NOT NULL,
    ticker        TEXT NOT NULL,
    name          TEXT,
    qty           REAL,
    avg_price     REAL,
    market_value  REAL,
    currency      TEXT DEFAULT 'KRW',
    captured_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_holdings_snap ON holdings(snapshot_id);

-- ============================================================
-- 현재가 스냅샷
-- ============================================================
CREATE TABLE IF NOT EXISTS quotes (
    id          INTEGER PRIMARY KEY,
    ticker      TEXT NOT NULL,
    market      TEXT,
    price       REAL,
    source      TEXT,
    captured_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_quotes_ticker ON quotes(ticker, captured_at DESC);

-- ============================================================
-- 동기화 작업 이력 (성공/오류/단계/시각 — freshness 근거)
-- ============================================================
CREATE TABLE IF NOT EXISTS sync_events (
    id            INTEGER PRIMARY KEY,
    account_index INTEGER,
    kind          TEXT,                       -- balance | accounts_from_env
    status        TEXT NOT NULL,              -- ok | error
    stage         TEXT,                       -- credentials | token | balance
    error         TEXT,
    started_at    TEXT,
    finished_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sync_account ON sync_events(account_index, finished_at DESC);

-- ============================================================
-- 감사로그 (모든 주문/승인/거절/차단 — 비밀값 미저장)
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY,
    actor       TEXT,
    action      TEXT NOT NULL,
    entity_type TEXT,
    entity_id   INTEGER,
    mode        TEXT,
    level       TEXT NOT NULL DEFAULT 'INFO',
    payload     TEXT,
    created_at  TEXT NOT NULL,
    CHECK (level IN ('CRITICAL','WARNING','INFO'))
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_entity  ON audit_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_action  ON audit_logs(action);

-- ============================================================
-- 주문 원장 (idempotency + 상태머신)
-- ============================================================
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,
    payload_hash    TEXT NOT NULL,
    account_id      INTEGER,
    mode            TEXT NOT NULL,
    ticker          TEXT,
    side            TEXT,
    qty             REAL,
    order_type      TEXT,
    limit_price     REAL,
    broker_order_id TEXT,
    status          TEXT NOT NULL DEFAULT 'created',
    reason          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (status IN ('created','submitting','submitted','in_doubt','partial','filled','rejected','canceled','aborted'))
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_account ON orders(account_id);

-- ============================================================
-- 종목 유니버스 (계좌별 관심종목 + 목표비중) — 소전제 골격
-- 사용자가 직접 입력 → KIS 검증된 종목만 저장. mock 하드코딩 목록 대체.
-- ============================================================
CREATE TABLE IF NOT EXISTS universe_instruments (
    id                INTEGER PRIMARY KEY,
    account_index     INTEGER NOT NULL,
    ticker            TEXT NOT NULL,
    market            TEXT NOT NULL DEFAULT 'KRX',
    name              TEXT,
    asset_class       TEXT,
    currency          TEXT NOT NULL DEFAULT 'KRW',
    is_leveraged      INTEGER NOT NULL DEFAULT 0,
    is_inverse        INTEGER NOT NULL DEFAULT 0,
    target_weight_pct REAL NOT NULL DEFAULT 0,
    is_active         INTEGER NOT NULL DEFAULT 1,
    last_price        REAL,
    verified_at       TEXT,          -- KIS 검증 시각 (UTC)
    source            TEXT,          -- kis_live | kis_paper | manual
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_index, ticker, market)
);
CREATE INDEX IF NOT EXISTS idx_universe_account ON universe_instruments(account_index, is_active);

-- ============================================================
-- 의사결정 스냅샷 (계좌별 현재비중 vs 목표비중 → drift → 제안 후보 → 리스크)
-- 백엔드(decision.py)가 계산해 저장. 웹은 조회만. (추후 rebalance_proposals 정규화)
-- ============================================================
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY,
    account_index INTEGER NOT NULL,
    payload       TEXT NOT NULL,   -- JSON: total/cash/lines/risk
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_decisions_account ON decisions(account_index, id DESC);

-- ============================================================
-- 투자 프로필 (대전제 운용방식 + 중전제 관심/생각) — 계좌별 1행
-- 종목(소전제)보다 먼저. 자유입력을 Claude(메모리 에이전트)가 되물어 구조화.
-- ============================================================
CREATE TABLE IF NOT EXISTS investor_profile (
    account_index   INTEGER PRIMARY KEY,
    -- 대전제 (운용 방식)
    posture_text    TEXT,          -- "어떻게 운용하고 싶은가" 자유입력
    risk_tolerance  TEXT,          -- aggressive | neutral | defensive
    short_policy    TEXT,          -- none | insurance | active
    cash_min_pct    REAL,          -- 현금 밴드 하한
    cash_max_pct    REAL,          -- 현금 밴드 상한
    horizon         TEXT,          -- 투자 기간/목적
    -- 중전제 (관심 분야 + 내 생각)
    interests_text  TEXT,          -- 관심 섹터/테마
    views_text      TEXT,          -- 내 생각/견해
    -- 운용 세부 (대전제에서 추린 다운스트림 변수)
    individual_cap_pct REAL,       -- 개별주 총합 상한(%)
    individual_count   INTEGER,    -- 개별 종목 목표 수
    region_pref        TEXT,       -- 전세계/미국/국내 등 지역 선호
    rebalance_pace     TEXT,       -- slow | normal | fast (분할 조정 속도)
    -- 진화하는 자유 문서 (RDB 컬럼으로 가둘 수 없는 부분: 키워드/보완점/지역분배/Claude 노트/lesson 참조)
    doc                TEXT,       -- JSON document (하이브리드: 단단한 변수=컬럼, 유연한 내용=문서)
    -- 메타
    refined_by      TEXT,          -- claude_agent | user (되물어 정리한 주체)
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- lessons — 성장 토대. 시장/경제/섹터/종목/전제/결정 분석이 누적·재사용된다.
-- "메모리로 성장하는 에이전트"의 DB측 substrate (Anthropic API 미사용).
-- Claude+메모리가 분석할 때 적재하고, 다음 판단에서 scope/ref 로 조회해 재사용.
-- ============================================================
CREATE TABLE IF NOT EXISTS lessons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index   INTEGER,        -- NULL = 전역 교훈
    scope           TEXT NOT NULL,  -- market | economy | sector | instrument | premise | decision
    ref             TEXT,           -- 종목코드/섹터명/주제 등 (scope 내 키)
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    confidence      REAL,           -- 0~1 (반복 검증되며 성장)
    source          TEXT,           -- claude_agent | user | outcome
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lessons_scope ON lessons(scope, ref, id DESC);

-- ============================================================
-- v2 승격: policy object / anchor+tilt / rebalance plan / lesson 후보 / evidence
-- (PostgreSQL + Vector + Graph 승격 전제. 정수 PK + scope/ref 패턴으로 Graph 이식 용이)
-- ============================================================

-- 컴파일된 투자 정책 객체 (investor_profile → policy). 버전 관리.
CREATE TABLE IF NOT EXISTS portfolio_policies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index INTEGER NOT NULL,
    version       INTEGER NOT NULL,
    policy        TEXT NOT NULL,   -- JSON: 성향·현금밴드·limits(단일/섹터/국가/통화/개별/인버스/레버리지)·pace·금지자산
    source        TEXT,            -- user | claude_agent
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_policies_acc ON portfolio_policies(account_index, version DESC);

-- anchor+tilt 목표비중 제안 (보수/기준/공격 3안). 사람 선택 전 = draft.
CREATE TABLE IF NOT EXISTS target_allocations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index INTEGER NOT NULL,
    proposal_id   TEXT NOT NULL,   -- 같은 생성 1회 = 동일 proposal_id (3 variant 묶음)
    variant       TEXT NOT NULL,   -- conservative | base | aggressive
    kind          TEXT NOT NULL,   -- cash | anchor | tilt
    ref           TEXT,            -- 테마/섹터/자산군 (cash 는 NULL)
    weight_pct    REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft',  -- draft | chosen | archived
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_alloc_prop ON target_allocations(account_index, proposal_id, variant);

-- 회차 단위 리밸런싱 계획 (decision 1회 = plan 1개).
CREATE TABLE IF NOT EXISTS rebalance_plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index INTEGER NOT NULL,
    decision_id   INTEGER,
    pace          TEXT,
    summary       TEXT,            -- JSON 요약
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS rebalance_plan_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id       INTEGER NOT NULL,
    ticker        TEXT NOT NULL,
    direction     TEXT,            -- 매수 | 매도
    total_pct     REAL, total_krw INTEGER,
    cycle_pct     REAL, cycle_krw INTEGER, cycle_qty INTEGER,
    remaining_pct REAL,
    round_no      INTEGER, total_rounds INTEGER,
    limit_price   REAL,
    status        TEXT,            -- candidate | hold | blocked
    reason        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_plansteps ON rebalance_plan_steps(plan_id);

-- lesson 후보 (승격 전 관찰). 승격 기준 충족 시 lessons 로 이동.
CREATE TABLE IF NOT EXISTS lesson_candidates (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index  INTEGER,
    scope          TEXT NOT NULL,  -- market|economy|sector|instrument|premise|decision|risk
    ref            TEXT,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    evidence_ref   TEXT,           -- evidence_documents.id 등
    observed_count INTEGER NOT NULL DEFAULT 1,
    outcome        TEXT,           -- 실제 결과(있으면)
    confidence     REAL DEFAULT 0.0,
    status         TEXT NOT NULL DEFAULT 'candidate', -- candidate | promoted | rejected
    source         TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lesscand ON lesson_candidates(scope, ref, status);

-- 근거 문서 (RDB 메타 — 본문 임베딩은 Vector 승격 시).
CREATE TABLE IF NOT EXISTS evidence_documents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    scope          TEXT,           -- news|disclosure|report|fundamental|dividend
    ref            TEXT,           -- 종목/테마
    source_type    TEXT,
    title          TEXT,
    body           TEXT,
    url            TEXT,
    freshness      TEXT,           -- 발행/수집 시점
    confidence     REAL,
    affected_theme TEXT,
    affected_asset TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- decision ↔ evidence 링크 (어떤 근거가 어떤 비중변경으로).
CREATE TABLE IF NOT EXISTS decision_evidence_links (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id   INTEGER NOT NULL,
    evidence_id   INTEGER NOT NULL,
    weight_change REAL,
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- evidence ↔ 조언/리뷰 연결 (중앙 스키마 정합 — 런타임 생성 제거).
CREATE TABLE IF NOT EXISTS theme_advice_evidence_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT, advice_id INTEGER NOT NULL,
    evidence_id INTEGER NOT NULL, note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_thadv_ev ON theme_advice_evidence_links(evidence_id);
CREATE TABLE IF NOT EXISTS daily_review_evidence_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER NOT NULL,
    evidence_id INTEGER NOT NULL, note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_drev_ev ON daily_review_evidence_links(evidence_id);

-- 3안 중 사람이 선택해 확정한 공식 target allocation (append-only 이력).
-- 재선택·취소 시 이전 행 삭제 금지 — status 만 superseded/cancelled 로.
CREATE TABLE IF NOT EXISTS allocation_selections (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index                 INTEGER NOT NULL,
    proposal_id                   TEXT,
    variant                       TEXT,        -- conservative|base|aggressive|custom
    allocation                    TEXT NOT NULL, -- JSON: 확정 비중 [{kind,ref,weight_pct}]
    policy_version                INTEGER,
    account_snapshot_id           INTEGER,
    expected_drift_pct            REAL,
    expected_rebalance_total_krw  INTEGER,
    expected_rebalance_rounds     INTEGER,
    precheck_status               TEXT,        -- pass | warn | block
    precheck_reasons              TEXT,        -- JSON []
    selected_by                   TEXT,
    user_override                 INTEGER DEFAULT 0,
    diff                          TEXT,        -- JSON: 이전 선택 대비 변경
    status                        TEXT NOT NULL DEFAULT 'active', -- active|superseded|cancelled
    selected_at                   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_allocsel ON allocation_selections(account_index, id DESC);

-- 세부 선정 위저드(종목·ETF 선정 화면)의 작업중 draft — 계좌당 현재 1건(덮어쓰기).
-- ⚠️ 이것은 policy/주문이 아니다. 화면에서 고른 종목·개별주 carve·초안 승인 표시를
--    "잃지 않게" 저장만 한다(새로고침/재접속 복원용). 실제 반영은 confirmed allocation +
--    리스크 게이트 + CEO 최종 승인 단계에서만. (allocation_selections = 확정 truth, 별개)
CREATE TABLE IF NOT EXISTS selection_drafts (
    account_index   INTEGER PRIMARY KEY,          -- 계좌당 현재 draft 1건 (upsert)
    proposal_id     TEXT,                          -- 어떤 확정 3안 기준으로 골랐는지(staleness 참고)
    picks_json      TEXT NOT NULL DEFAULT '[]',    -- JSON [{bucket,ticker,name,asset_class}]
    equity_option   TEXT NOT NULL DEFAULT 'none',  -- 개별주 carve: none|5|10 (위험자산 60% 내 분배)
    acknowledged    INTEGER NOT NULL DEFAULT 0,    -- 초안 승인 표시(0/1) — policy/주문 미반영
    acknowledged_at TEXT,                          -- 초안 승인 표시 시각
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 대전제 정리 시 도출된 개선 제안(조언) + 사람의 반영/보류 결정 (감사·append-only 성격).
-- 출처: rule(규칙) | lesson:<id>(우리 메모리) | benchmark(외부 사례) | research(Claude 외부조사).
CREATE TABLE IF NOT EXISTS advice_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index   INTEGER NOT NULL,
    title           TEXT NOT NULL,
    detail          TEXT NOT NULL,
    source          TEXT,           -- rule | lesson:<id> | benchmark | research
    severity        TEXT,           -- info | suggest | important
    suggested_field TEXT,           -- 반영 시 바꿀 profile 필드(있으면)
    suggested_value TEXT,
    status          TEXT NOT NULL DEFAULT 'open',  -- open | accepted | rejected
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_advice ON advice_items(account_index, status, id DESC);

-- 중전제(관심 분야 + 내 생각) AI 분석 요청 + 결과. 핵심 아이디어 추출 + 테마 의견 + 개선 제안.
-- 지능 = Claude+메모리(API 아님): 즉시 결과는 규칙+메모리, 심층은 Claude가 세션에서 보강.
CREATE TABLE IF NOT EXISTS analysis_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index INTEGER NOT NULL,
    kind          TEXT,            -- midpremise
    input         TEXT,            -- JSON {interests, views}
    result        TEXT,            -- JSON {ideas, themes, suggestions, ai_opinion}
    status        TEXT NOT NULL DEFAULT 'done',  -- done | pending
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_analysis ON analysis_requests(account_index, id DESC);

-- "Claude 분석 전문가에게 조언 구하기" — 자유 질문 → 입력 방법·권장값·메모리 근거 답변 (append-only 로그).
CREATE TABLE IF NOT EXISTS consultations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index INTEGER NOT NULL,
    question      TEXT NOT NULL,
    answer        TEXT,            -- 답변 본문
    refs          TEXT,            -- JSON: 인용 메모리(lessons)
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_consult ON consultations(account_index, id DESC);

-- 대전제/중전제 변경 이력 (append-only) — 진화하는 전제를 버전으로 추적.
-- 대화·직접수정·규칙정리 무엇으로 바뀌었든 매 저장 시 1행 적재. 되돌리기/감사 근거.
CREATE TABLE IF NOT EXISTS investor_profile_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index   INTEGER NOT NULL,
    snapshot        TEXT NOT NULL,   -- 저장 시점 프로필 전체(JSON)
    source          TEXT,            -- user | claude_agent | distill
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_profile_hist ON investor_profile_history(account_index, id DESC);

-- ============================================================
-- 성장 스캐폴딩 (growth scaffolding) — 모든 Agent 공통 토대.
-- 목적: Agent가 (1) 자기 task에 맞는 memory를 정확히 불러오고(prehook),
--       (2) 작업 전 정책·위험을 안전 점검하고, (3) 작업 후 배운 점을 정리하고(posthook),
--       (4) 전체 workflow가 추적·재현 가능하도록 task provenance를 남긴다.
-- 설계: 정수 PK + scope/ref + JSON payload (PostgreSQL/Vector/Graph 승격 용이). append-only 지향.
-- ============================================================

-- 1) tasks — 표준 task 상태머신 + provenance (workflow 추적/재개의 단위).
--    prehook(작업 전 확인)·posthook(작업 후 정리) 결과를 한 행에 묶는다.
CREATE TABLE IF NOT EXISTS tasks (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index          INTEGER,
    agent                  TEXT NOT NULL,   -- agent slug (broker-chief|theme-sector-advisor|view-coach|risk-chief|...)
    task_type              TEXT NOT NULL,   -- consult|profile_save|policy_compile|allocation_generate|selection|decision|risk_check|theme_advice|view_coach|sync
    status                 TEXT NOT NULL DEFAULT 'open', -- open|running|done|blocked|failed|cancelled
    -- prehook provenance (작업 전 무엇을 기준으로 삼았는가)
    policy_version         INTEGER,
    selected_allocation_id INTEGER,
    account_snapshot_id    INTEGER,
    prehook                TEXT,            -- JSON {gate:pass|block, checks:[{name,ok,detail}], memory_count, reasons:[]}
    -- posthook (작업 후 정리)
    outcome                TEXT,            -- JSON 요약(성공/실패/산출물 id)
    next_action            TEXT,            -- 다음에 해야 할 일
    unresolved_risk        TEXT,            -- 미해결 위험
    block_reason           TEXT,            -- blocked/failed 사유
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_acc   ON tasks(account_index, id DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(agent, status, id DESC);

-- 2) agent_memory_scope — Agent별 memory scope 분리 (prehook 검색 대상 레지스트리).
--    "이 Agent는 어떤 scope의 lessons/evidence를 우선 읽어야 하는가"를 데이터로 선언.
CREATE TABLE IF NOT EXISTS agent_memory_scope (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent         TEXT NOT NULL,   -- agent slug
    scope         TEXT NOT NULL,   -- lessons.scope (market|economy|sector|instrument|premise|decision|risk|region|bond)
    priority      INTEGER NOT NULL DEFAULT 100,  -- 작을수록 prehook에서 먼저 검색
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent, scope)
);
CREATE INDEX IF NOT EXISTS idx_agentscope ON agent_memory_scope(agent, priority);

-- 3) task_memory_links — task ↔ memory provenance (prehook이 실제로 무엇을 참조했는가).
CREATE TABLE IF NOT EXISTS task_memory_links (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL,
    memory_kind   TEXT NOT NULL,   -- lesson|lesson_candidate|evidence|feedback|policy|selected_allocation|snapshot
    memory_id     INTEGER,
    scope         TEXT,
    ref           TEXT,
    relevance     REAL,            -- 로드 시점 decay-가중 점수
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_taskmem     ON task_memory_links(task_id);
CREATE INDEX IF NOT EXISTS idx_taskmem_mem ON task_memory_links(memory_kind, memory_id);

-- 4) feedback_memory — 사용자의 거절/수정/미저장을 학습 가능한 negative memory로.
--    advisor가 prehook에서 읽어 "이전에 사용자가 거절한 방향"을 피하도록.
CREATE TABLE IF NOT EXISTS feedback_memory (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index INTEGER,
    agent         TEXT,
    kind          TEXT NOT NULL,   -- rejected_advice|user_edit|override|unsaved_consult
    scope         TEXT,            -- sector|premise|region|bond|...
    ref           TEXT,
    detail        TEXT NOT NULL,
    source_ref    TEXT,            -- advice_items.id|consultations.id 등 출처
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback ON feedback_memory(account_index, agent, id DESC);

-- ============================================================
-- 필드별 AI 조언 (중전제 각 입력 필드 전문 Advisor) — append-only 로그.
-- AI 조언은 바로 policy 에 반영되지 않는다: 조언 → 임시제안 → 사람 저장 → policy version.
-- ============================================================
CREATE TABLE IF NOT EXISTS field_consultations (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index            INTEGER NOT NULL,
    field_name               TEXT NOT NULL,   -- interests|views|region|defensive|pace|whole
    agent_name               TEXT,            -- theme-field-advisor|opinion-field-advisor|...
    advice_type              TEXT,            -- improve|risk_check|extract|reflect
    original_text            TEXT,
    suggested_text           TEXT,
    extracted_variables_json TEXT,            -- 추출된 정책 변수(JSON)
    risk_warnings_json       TEXT,            -- 위험 경고(JSON)
    missing_points_json      TEXT,            -- 빠진 점(JSON)
    follow_up_json           TEXT,            -- 보완 질문(JSON)
    evidence_ids             TEXT,
    lesson_ids               TEXT,
    confidence               REAL,
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fieldconsult ON field_consultations(account_index, field_name, id DESC);

-- 필드 조언에 대한 사용자 행동(append-only): applied|edited|ignored|saved.
CREATE TABLE IF NOT EXISTS field_advice_events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index         INTEGER NOT NULL,
    field_consultation_id INTEGER,
    field_name            TEXT,
    user_action           TEXT NOT NULL,   -- applied|edited|ignored|saved
    detail                TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fieldadviceev ON field_advice_events(account_index, field_consultation_id, id DESC);

-- ============================================================
-- Daily Portfolio Review — 실시간 봇 아님. 정기 점검 → 판단 보조 → 예약성 조정.
-- "오늘은 관망(hold/watch)"도 정상 결과. 주문 후보는 selected allocation + drift 에서만.
-- ============================================================
CREATE TABLE IF NOT EXISTS market_context_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    rates_json   TEXT,   -- 금리
    fx_json      TEXT,   -- 환율
    indices_json TEXT,   -- 지수
    news_json    TEXT,   -- 뉴스/공시/실적/배당 이벤트
    summary      TEXT,
    captured_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_portfolio_reviews (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index          INTEGER NOT NULL,
    review_date            TEXT NOT NULL,   -- YYYY-MM-DD (계좌×일 1행)
    account_snapshot_id    INTEGER,
    selected_allocation_id INTEGER,
    drift_score            REAL,
    market_context_id      INTEGER,
    action_decision        TEXT,            -- buy|sell|rebalance|hold|watch
    action_reason          TEXT,
    no_trade_reason        TEXT,            -- 관망/보류 사유(정상 결과)
    scheduled_order_plan_id INTEGER,
    risk_passed            INTEGER,
    approved_by_user       INTEGER NOT NULL DEFAULT 0,
    payload                TEXT,            -- JSON 상세(lines/risk/market)
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_index, review_date)
);
CREATE INDEX IF NOT EXISTS idx_dailyreview ON daily_portfolio_reviews(account_index, review_date DESC);

-- 예약성 지정가 주문 계획 (Daily Review/decision 기준). 시장가 매수 금지 — 지정가만.
CREATE TABLE IF NOT EXISTS scheduled_order_plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index INTEGER NOT NULL,
    review_id     INTEGER,
    decision_id   INTEGER,
    status        TEXT NOT NULL DEFAULT 'pending_approval', -- pending_approval|approved|expired|cancelled
    valid_until   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_schedplan ON scheduled_order_plans(account_index, id DESC);

CREATE TABLE IF NOT EXISTS scheduled_order_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id       INTEGER NOT NULL,
    ref           TEXT,            -- 테마/anchor/종목 (소전제 매핑 전 ref)
    ticker        TEXT,
    direction     TEXT,            -- 매수 | 매도 (시장가 매수 금지)
    total_pct     REAL, total_krw INTEGER,
    cycle_pct     REAL, cycle_krw INTEGER,
    remaining_pct REAL,
    round_no      INTEGER, total_rounds INTEGER,
    limit_price   REAL,            -- 지정가(예측 진입). 없으면 다음 cycle 재평가
    valid_until   TEXT,
    on_unfilled   TEXT,            -- 미체결 시 처리
    hold_condition TEXT,           -- 보류 조건
    status        TEXT NOT NULL DEFAULT 'candidate', -- candidate|hold|blocked
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_schedstep ON scheduled_order_steps(plan_id);

-- ============================================================
-- 멀티 브로커 — broker 별 자격증명(평문 금지, .env/secret ref 만). accounts.broker 로 어댑터 선택.
-- KIS 전용 코드에 키움 예외처리 추가 금지 — BrokerPort(adapter) 로 분리.
-- ============================================================
CREATE TABLE IF NOT EXISTS broker_credentials (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index    INTEGER NOT NULL,
    broker           TEXT NOT NULL,   -- kis | kiwoom | manual | paper
    key_ref          TEXT,            -- .env 키 이름 등 *참조*(평문 키 금지)
    secret_ref       TEXT,            -- .env 시크릿 이름 등 *참조*
    token_status     TEXT,            -- ok | error | unknown
    token_expires_at TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_index, broker)
);
CREATE INDEX IF NOT EXISTS idx_brokercred ON broker_credentials(account_index, broker);

-- 5) agent_memories — 통합 scoped 메모리 (CEO memory scope 지시).
--    "계좌별 실행은 분리, 전문 Agent 지식은 공통 성장, 최종 적용은 계좌별 정책 우선."
--    scope_type 으로 적용 범위를 명시: account(이 계좌 정책 메모리) | user(CEO 공통 성향) |
--    agent(공통 Agent lesson, account_index NULL · promoted=1 이면 계좌 간 재사용) | task(휘발성 현재맥락).
CREATE TABLE IF NOT EXISTS agent_memories (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type        TEXT NOT NULL,   -- account | user | agent | task
    scope_id          TEXT,
    agent_name        TEXT,
    task_type         TEXT,
    account_index     INTEGER,         -- account scope 만 채움; user/agent/task scope 는 NULL 가능
    theme             TEXT,
    sector            TEXT,
    title             TEXT,
    body              TEXT,
    confidence        REAL DEFAULT 0,
    freshness_at      TEXT,
    source            TEXT,
    promoted          INTEGER DEFAULT 0,  -- agent scope: 1 이면 계좌 간 공통 재사용 (공통 성장)
    archived          INTEGER DEFAULT 0,  -- task scope 휘발성: 빠르게 archived
    evidence_ids      TEXT,               -- JSON [] evidence_documents.id 등
    policy_version_id INTEGER,
    decision_id       INTEGER,
    created_at        TEXT,
    updated_at        TEXT,
    CHECK (scope_type IN ('account','user','agent','task'))
);
CREATE INDEX IF NOT EXISTS idx_agentmem_scope  ON agent_memories(scope_type, account_index);
CREATE INDEX IF NOT EXISTS idx_agentmem_agent  ON agent_memories(agent_name, scope_type);
CREATE INDEX IF NOT EXISTS idx_agentmem_theme  ON agent_memories(theme);
CREATE INDEX IF NOT EXISTS idx_agentmem_sector ON agent_memories(sector);

-- ============================================================
-- 성장 강제(Growth Middleware) — Task별 실패 패턴·regression·성장 리포트.
-- (prehook/posthook provenance 는 tasks + task_memory_links 가 담당)
-- ============================================================
CREATE TABLE IF NOT EXISTS task_failure_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type     TEXT NOT NULL,
    agent_name    TEXT,
    account_index INTEGER,
    detail        TEXT,                 -- 실패/차단/validation 실패 사유
    occurrences   INTEGER DEFAULT 1,
    promoted_to_regression INTEGER DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_failpat_task ON task_failure_patterns(task_type);

CREATE TABLE IF NOT EXISTS task_regression_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type     TEXT NOT NULL,
    title         TEXT NOT NULL,
    given_input   TEXT,                 -- 입력 예
    expect        TEXT,                 -- 기대 결과 (예: "반도체→short_or_hedge")
    source_failure_id INTEGER,          -- 어느 실패에서 승격됐는지
    status        TEXT DEFAULT 'active',-- active|retired
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (task_type, title)
);
CREATE INDEX IF NOT EXISTS idx_regression_task ON task_regression_tests(task_type);

CREATE TABLE IF NOT EXISTS growth_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type    TEXT NOT NULL,        -- agent | task
    scope_name    TEXT NOT NULL,        -- agent_name 또는 task_type
    account_index INTEGER,
    new_candidates INTEGER DEFAULT 0,
    promoted_count INTEGER DEFAULT 0,
    archived_count INTEGER DEFAULT 0,
    rejected_count INTEGER DEFAULT 0,
    summary_json  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_growthrep ON growth_reports(scope_type, scope_name);

-- ============================================================
-- 관심 분야 AI 후보 제안 — **자동 투자 아님**. neutral 저장 → 사용자 선택 → 조사 → 방향분류 → 저장.
-- candidate_type: adjacent|complement|diversify|hedge|watch. direction 기본 unknown_direction(자동 long 금지).
-- ============================================================
CREATE TABLE IF NOT EXISTS theme_suggestion_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index   INTEGER NOT NULL,
    source_theme    TEXT,               -- 어떤 입력 테마에서 파생(또는 '전체'/'시장')
    candidate_theme TEXT NOT NULL,
    candidate_type  TEXT NOT NULL,      -- adjacent|complement|diversify|hedge|watch
    reason          TEXT,
    relationship    TEXT,               -- 기존 관심분야와의 관계
    suggested_role  TEXT,               -- core|growth_tilt|hedge|defensive|watch
    direction       TEXT DEFAULT 'unknown_direction',  -- neutral 기본 (자동 long 금지)
    confidence      REAL,
    freshness_at    TEXT,
    evidence_ids    TEXT,               -- JSON []
    user_action     TEXT DEFAULT 'suggested',  -- suggested|added_to_research|ignored|applied_to_draft|saved_to_policy|rejected
    applied_to_research_queue INTEGER DEFAULT 0,
    applied_to_policy INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_themesugg ON theme_suggestion_candidates(account_index, candidate_theme);

-- ============================================================
-- 일봉 가격이력 — 하락 징후 분석 엔진(Pre-Decline Signal Engine)의 입력.
-- 1 entity = 1 table. 자연키 (instrument_code, trade_date) = PK (멱등 upsert).
-- source: kis_daily | kiwoom_daily | quotes_seed | manual | test
--   (실 백테스트는 브로커 일봉 fetch 전제. quotes_seed = 기존 누적 quotes 에서 근사 seed
--    — 정직: 단일 시점가 모음이라 OHLC=close 근사, 실 일봉 아님.)
-- 가격은 평문 OK (시세는 비밀 아님). additive only — drop 금지.
-- ============================================================
CREATE TABLE IF NOT EXISTS price_history (
    instrument_code TEXT NOT NULL,          -- ticker (KRX 6자리 / 미국 심볼)
    trade_date      TEXT NOT NULL,          -- 'YYYY-MM-DD' (거래일)
    open            REAL,
    high            REAL,
    low             REAL,
    close           REAL NOT NULL,          -- 종가 (신호 계산 최소 요건)
    volume          REAL,
    source          TEXT NOT NULL,          -- kis_daily | kiwoom_daily | quotes_seed | manual | test
    captured_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (instrument_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_price_history_code ON price_history(instrument_code, trade_date);

-- ============================================================
-- 하락 징후 6축 — 축별 데이터 테이블 (additive only, drop 금지. 1 entity = 1 table)
-- 정직: 아래 테이블들은 ingestion 지점이다. 실데이터 적재 전까지 축은 data_available=False.
-- 비밀/자격증명 저장 금지(시세·지표·캘린더는 비밀 아님).
-- ============================================================

-- 분산축: KR 종목별 투자자 매매동향 (KIS inquire-investor / 키움 투자자별 매매동향)
-- 순매수=양수, 순매도=음수 (금액 또는 수량 — 부호로 분산 판단).
CREATE TABLE IF NOT EXISTS investor_flows (
    instrument_code TEXT NOT NULL,          -- ticker
    trade_date      TEXT NOT NULL,          -- 'YYYY-MM-DD'
    foreign_net     REAL,                   -- 외국인 순매수(-=순매도)
    institution_net REAL,                   -- 기관 순매수
    retail_net      REAL,                   -- 개인 순매수
    volume          REAL,                   -- 거래량(분산 구간 급증 판단)
    source          TEXT NOT NULL,          -- kis_investor | kiwoom_investor | manual | test
    captured_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (instrument_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_investor_flows_code ON investor_flows(instrument_code, trade_date);

-- 거시축: ECOS(한은)/FRED 거시지표 시계열 (지표명 단위로 한 행).
CREATE TABLE IF NOT EXISTS macro_indicators (
    indicator   TEXT NOT NULL,              -- policy_rate | yield_10y | yield_2y | cpi_yoy | credit_growth_yoy | fx_usdkrw ...
    obs_date    TEXT NOT NULL,              -- 'YYYY-MM-DD' (관측일)
    value       REAL NOT NULL,
    source      TEXT NOT NULL,              -- ecos | fred | manual | test
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (indicator, obs_date)
);
CREATE INDEX IF NOT EXISTS idx_macro_ind ON macro_indicators(indicator, obs_date);

-- 이벤트축: 경제 캘린더(FOMC·금통위·CPI·고용 등) 발표 일정.
CREATE TABLE IF NOT EXISTS market_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date  TEXT NOT NULL,              -- 'YYYY-MM-DD'
    name        TEXT NOT NULL,              -- FOMC | 금통위 | CPI | 고용(NFP) ...
    impact      TEXT NOT NULL DEFAULT 'medium',  -- high | medium | low
    region      TEXT,                       -- US | KR ...
    source      TEXT NOT NULL,              -- calendar | manual | test
    captured_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_market_events_date ON market_events(event_date);

-- 심리축: VIX·풋콜비율·신용잔고 등 심리지표 시계열.
CREATE TABLE IF NOT EXISTS sentiment_index (
    indicator   TEXT NOT NULL,              -- vix | put_call_ratio | margin_balance | margin_balance_change_1m ...
    obs_date    TEXT NOT NULL,              -- 'YYYY-MM-DD'
    value       REAL NOT NULL,
    source      TEXT NOT NULL,              -- market | krx | manual | test
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (indicator, obs_date)
);
CREATE INDEX IF NOT EXISTS idx_sentiment_ind ON sentiment_index(indicator, obs_date);

-- 정책/규제축: 정부 정책·규제 이벤트 (뉴스/DART → stance/severity 는 사람·메모리 판단 저장).
CREATE TABLE IF NOT EXISTS policy_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date  TEXT NOT NULL,              -- 'YYYY-MM-DD'
    sector      TEXT,                       -- 영향 섹터(전반이면 NULL)
    stance      TEXT NOT NULL DEFAULT 'neutral',  -- adverse | favorable | neutral
    severity    REAL,                       -- 0~1 (영향 강도 — 사람/메모리 판단)
    title       TEXT NOT NULL,
    source      TEXT NOT NULL,              -- news | dart | manual | test
    captured_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_policy_events_date ON policy_events(event_date, sector);

-- ============================================================
-- 하락 징후 분석 기록 (성장 루프 영속화) — 예측 시점 분석 + 사후 결과 + reliability 변화.
-- 1 entity = 1 table. additive only — drop 금지. 비밀/자격증명 저장 금지(시세·점수만).
-- lookahead bias 차단: analysis_date 는 **예측 시점**(그날까지의 데이터만으로 산출).
--   결과 평가(actual_drawdown/hit_or_miss)는 analysis_date **이후** future_return_window
--   거래일 일봉만으로 계산한다(미래 데이터 누설 금지).
-- scope 규칙: account_index 는 "이 분석을 본 계좌" 기록일 뿐, reliability 성장은
--   axis/instrument/sector 시장 공통 노하우로 누적(계좌 교차적용 아님).
-- ============================================================
CREATE TABLE IF NOT EXISTS decline_analyses (
    analysis_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index        INTEGER,                -- 이 분석을 조회한 계좌(있으면). 성장은 계좌 무관(시장 공통)
    code                 TEXT NOT NULL,          -- instrument_code (ticker)
    sector               TEXT,                   -- 섹터(있으면)
    analysis_date        TEXT NOT NULL,          -- 'YYYY-MM-DD' 예측 시점(이날까지 데이터만 사용 — lookahead 차단)
    available_axes       TEXT,                   -- JSON list — data_available=True 축
    missing_axes         TEXT,                   -- JSON list — data_available=False 축
    axis_scores          TEXT,                   -- JSON {axis: {risk_0_100, confidence, reliability, weight}}
    overall_risk         REAL,                   -- composite holistic_risk (0~100)
    overall_confidence   REAL,                   -- composite overall_confidence (0~1)
    suggested_action     TEXT,                   -- shift_conservative | hold | NULL
    policy_draft_created  INTEGER NOT NULL DEFAULT 0,  -- 보수적 전환 초안 생성 여부(0/1) — 자동적용 아님
    user_action          TEXT,                   -- ignored|accepted|modified|saved_to_policy|rejected_as_wrong|NULL(미정)
    future_return_window INTEGER,                -- 결과 평가에 쓴 거래일 수(예: 10/20)
    actual_drawdown      REAL,                   -- analysis_date 이후 window 내 실제 최대 낙폭(%, 음수). NULL=미평가
    hit_or_miss          TEXT,                   -- hit|miss|pending|no_prediction (사후 결과)
    lesson_id            INTEGER,                -- 연결된 lesson_candidate id(있으면)
    reliability_before   REAL,                   -- 결과평가 직전 대표 reliability
    reliability_after    REAL,                   -- 결과평가 직후 대표 reliability
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    evaluated_at         TEXT                    -- 결과 평가 시각(있으면)
);
CREATE INDEX IF NOT EXISTS idx_decline_analyses_code ON decline_analyses(code, analysis_date);
CREATE INDEX IF NOT EXISTS idx_decline_analyses_pending ON decline_analyses(hit_or_miss);

-- 사용자(CEO) 투자 견해/통찰 — 1급 입력. 계좌별. 대전제/중전제/단기/장기 분리.
-- 데이터보다 무조건 우위도, 무시도 아님. 시스템은 견해 vs 데이터 일치/충돌을 설명. 자동적용 금지(allocation/policy draft에만).
CREATE TABLE IF NOT EXISTS user_views (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index   INTEGER NOT NULL,            -- 계좌별 격리(교차적용 금지)
    layer           TEXT NOT NULL,               -- grand(대전제)|mid(중전제)|short(단기 견해)|long(장기 견해)
    theme           TEXT,                        -- 관련 테마(반도체/바이오/로봇/양자 등)
    ticker          TEXT,                        -- 관련 종목
    etf             TEXT,                        -- 관련 ETF
    stance          TEXT,                        -- positive|neutral|negative|observe(관찰만)
    conviction      REAL,                        -- 확신도 0~1 (user_conviction)
    horizon         TEXT,                        -- short|mid|long
    note            TEXT,                        -- 사용자 자유 견해 원문
    status          TEXT NOT NULL DEFAULT 'active', -- active|superseded|archived (견해 변경 이력)
    superseded_by   INTEGER,                     -- 새 견해 id(이력 추적)
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_user_views_acct ON user_views(account_index, status);

-- Evidence(자료 정리/요약) — 재무/공시/뉴스/ETF구성/거시/수급을 포트폴리오 판단용으로 정리.
-- 출처·날짜·freshness·confidence·상충·stale 관리. 근거 없는 강한 조언 금지.
CREATE TABLE IF NOT EXISTS evidence_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT,                       -- 출처(DART/뉴스사/리포트/ECOS 등)
    source_type       TEXT,                       -- financials|filing|news|sector|etf|macro|flow
    source_date       TEXT,                       -- 자료 발생/발표일 'YYYY-MM-DD'
    url               TEXT,
    freshness         REAL,                       -- 0~1 (decay)
    stale             INTEGER NOT NULL DEFAULT 0, -- 오래된 자료(1)
    confidence        REAL,                       -- 0~1
    related_account   INTEGER,                    -- 관련 계좌(있으면)
    related_ticker    TEXT,
    related_etf       TEXT,
    related_theme     TEXT,
    summary           TEXT,                       -- 무엇이 새로 나왔나(요약)
    positive_factors  TEXT,                       -- JSON/text 긍정 요인
    negative_factors  TEXT,                       -- JSON/text 부정 요인
    uncertainties     TEXT,                       -- 불확실성
    portfolio_impact  TEXT,                       -- 내 포트폴리오 영향(설명)
    suggested_action  TEXT,                       -- 조정 후보(주문 아님)
    user_feedback     TEXT,                       -- 사용자 반응 원문
    accepted_or_ignored TEXT,                     -- accepted|ignored|modified|rejected_as_wrong|NULL
    lesson_candidate_id INTEGER,                  -- 연결 lesson(있으면)
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_evidence_items_rel ON evidence_items(related_ticker, related_etf, related_theme);

-- ETF 구성종목 — 겹침/노출 분석용. (개별주와 다르게: 구성·비중·섹터/국가·중복보유)
CREATE TABLE IF NOT EXISTS etf_constituents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    etf_ticker          TEXT NOT NULL,
    constituent_ticker  TEXT NOT NULL,
    constituent_name    TEXT,
    weight_pct          REAL,                     -- ETF 내 비중(%)
    sector              TEXT,
    country             TEXT,
    as_of               TEXT,                     -- 구성 기준일 'YYYY-MM-DD'
    source              TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(etf_ticker, constituent_ticker, as_of)
);
CREATE INDEX IF NOT EXISTS idx_etf_constituents_etf ON etf_constituents(etf_ticker, as_of);

-- 통합 개인화 루프 — 계좌별 조언/후보 선호 가중(선택↑·무시↓). 계좌 격리(교차 금지).
-- 공통 agent memory(lessons)와 분리: 이건 *그 계좌 사용자*의 선호만.
CREATE TABLE IF NOT EXISTS personalization_weights (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_index   INTEGER NOT NULL,            -- 계좌별 격리
    scope           TEXT NOT NULL,               -- perspective|theme|advice_type|candidate_type|hedge
    key             TEXT NOT NULL,               -- 예: 'C'(공격안)·'반도체'·'hedge'·'defensive'
    accepted_count  INTEGER NOT NULL DEFAULT 0,
    ignored_count   INTEGER NOT NULL DEFAULT 0,
    modified_count  INTEGER NOT NULL DEFAULT 0,
    last_reason     TEXT,                         -- 마지막 무시/수정 이유
    weight          REAL NOT NULL DEFAULT 1.0,    -- 파생 가중(>1 선호·<1 비선호) — ranking 조정용
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(account_index, scope, key)
);
CREATE INDEX IF NOT EXISTS idx_personalization_acct ON personalization_weights(account_index, scope);

-- 자산별/시장별 누적 메모리 — 종목/ETF/섹터/테마/거시/이벤트/정책에 대한 지식이 시간이 지날수록 축적.
-- 공통 자산 메모리(account_id NULL)와 사용자 관점(account_id/user_id 지정)을 분리(교차 덮어쓰기 금지).
-- 모든 강한 메모리는 evidence 연결 + freshness/stale 필수(출처 없는 단정 금지).
CREATE TABLE IF NOT EXISTS asset_memory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type      TEXT NOT NULL,   -- stock|etf|sector|theme|macro|event|policy
    scope_key       TEXT NOT NULL,   -- 005930 | 069500 | semiconductor | robotics | interest_rate | fomc ...
    memory_type     TEXT NOT NULL,   -- fact|interpretation|user_view|outcome|lesson
    account_index   INTEGER,         -- NULL=공통 자산지식 / 지정=그 계좌 관점(격리)
    user_id         INTEGER,         -- 사용자 관점일 때
    -- 검색 키(정보 잘 찾기) --
    ticker          TEXT, market TEXT, sector TEXT, theme TEXT, asset_class TEXT, bucket TEXT,
    related_etf     TEXT, related_stock TEXT, macro_factor TEXT, event_type TEXT, time_horizon TEXT,
    -- 본문/근거 --
    title           TEXT,
    body            TEXT,            -- 요약/내용(JSON 또는 text)
    positive_factors TEXT, negative_factors TEXT, uncertainties TEXT,
    evidence_id     INTEGER,         -- evidence_items 연결
    source          TEXT, source_date TEXT,
    freshness       REAL, confidence REAL,
    reliability     REAL,            -- 결과로 갱신되는 신뢰도
    stale           INTEGER NOT NULL DEFAULT 0,
    stale_at        TEXT,            -- 이 시점 지나면 재확인 필요
    last_verified_at TEXT, last_used_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_asset_memory_scope ON asset_memory(scope_type, scope_key, memory_type);
CREATE INDEX IF NOT EXISTS idx_asset_memory_keys ON asset_memory(ticker, sector, theme);
CREATE INDEX IF NOT EXISTS idx_asset_memory_acct ON asset_memory(account_index, user_id);

-- lesson run — 판단 → 시장반응/사용자반응 → reliability 갱신 → 다음 pre-hook 재사용.
CREATE TABLE IF NOT EXISTS lesson_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type          TEXT NOT NULL,   -- stock|etf|sector|theme|macro|event|policy|user_view|account|agent|task
    scope_key           TEXT NOT NULL,
    account_index       INTEGER, user_id INTEGER,   -- 격리(계좌/사용자 판단이면)
    source_memory_ids   TEXT,            -- JSON list (asset_memory ids)
    source_evidence_ids TEXT,            -- JSON list (evidence_items ids)
    decision_context    TEXT,
    signal_summary      TEXT,
    suggested_action    TEXT,
    user_action         TEXT,            -- accepted|ignored|modified|rejected|NULL
    market_reaction_window INTEGER,      -- 결과 평가 거래일 수(예: 5/20/60)
    actual_outcome      TEXT,            -- 수익률/낙폭 등(JSON)
    hit_or_miss         TEXT,            -- hit|miss|false_alarm|pending
    reliability_before  REAL, reliability_after REAL,
    lesson_text         TEXT,
    stale_at            TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_lesson_runs_scope ON lesson_runs(scope_type, scope_key);
CREATE INDEX IF NOT EXISTS idx_lesson_runs_pending ON lesson_runs(hit_or_miss);

-- 재무제표(개별주 우량주 필터용) — DART/공식 데이터. 미연동이면 행 없음(가짜 점수 금지).
CREATE TABLE IF NOT EXISTS fundamentals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    period          TEXT NOT NULL,       -- 'YYYY-Qn' 또는 'YYYY'
    revenue         REAL, op_income REAL, net_income REAL,
    op_margin       REAL, debt_ratio REAL, cash_flow_op REAL,
    roe             REAL, per REAL, pbr REAL, ev_ebitda REAL,
    inventory       REAL, capex REAL,
    source          TEXT, as_of TEXT, freshness REAL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ticker, period)
);
CREATE INDEX IF NOT EXISTS idx_fundamentals_ticker ON fundamentals(ticker, period);
