# PG SSOT Cutover — Read-Only Gap 분석 (Track G)

> **상태: 분석 전용. 적용/마이그레이션/삭제 0건.** 본 문서는 read-only psql 비교 결과이며, 실제 cutover 는 CEO 승인 후 별도 작업으로 수행한다.
> 작성: 2026-06-21. 방식: `psql` read-only 쿼리 + SQLite 스키마/행수 조회. 비밀번호는 출력/기록하지 않음.

---

## 0. 조사 환경 / 접속 가능 여부

| 대상 | 접속 문자열(마스킹) | 결과 |
|---|---|---|
| 로컬 PG | `postgresql://portfolio_app:***@localhost:5432/portfolio_os?search_path=portfolio` | **OK** (PostgreSQL 16.14) |
| 원격 PG (SSOT) | `postgresql://cyj:***@192.168.0.107:5432/portfolio_os?search_path=portfolio` | **OK** (PostgreSQL 16.14) |
| 앱 compute (SQLite) | `./data/portfolio.sqlite3` (753KB) | **OK** (SQLite 3.45) |

- `.env` 키: 로컬은 `DATABASE_URL`(app)·`DATABASE_URL_RO`(ro)·`DATABASE_URL_ADMIN`, 원격은 `REMOTE_DATABASE_URL` / `REMOTE_PG_*`.
- 주의: `.env` 의 `POSTGRES_DB=portfolio_os_db` 이지만 앱이 실제 사용하는 `DATABASE_URL` 의 DB 명은 **`portfolio_os`** (스키마 `portfolio`). `POSTGRES_DB` 변수는 현재 코드 경로와 불일치 — cutover 전 정리 필요(혼동 위험, write 아님).

### ⭐ 핵심 발견 1 — 로컬 PG ≡ 원격 PG (이미 동일)

3축 중 **두 PG 는 사실상 동일하다.**

- 테이블 수: 로컬 57 = 원격 57 (동일 목록).
- 컬럼 시그니처(table.column:type, 585개): **diff 0**.
- 비어있지 않은 테이블 행수: **로컬 = 원격 완전 일치** (아래 표).

즉 "원격이 더 발전된 canonical" 이라는 배경 가정과 달리, **현재 시점 로컬 PG 는 이미 원격과 같은 canonical 스키마/데이터를 갖고 있다.** (양쪽 모두 `migrations/pg/*.sql` 100~800 적용된 상태.) 따라서 진짜 gap 은 **PG(canonical) ↔ SQLite(앱 compute)** 한 축이다.

| 테이블 | 로컬 PG | 원격 PG |
|---|---|---|
| accounts | 2 | 2 |
| account_snapshots | 29 | 29 |
| users | 4 | 4 |
| user_sessions | 9 | 9 |
| user_account_access | 2 | 2 |
| user_auth_events | 16 | 16 |
| user_security_settings | 1 | 1 |
| password_reset_tokens | 2 | 2 |
| auth_events | 8 | 8 |
| auth_sessions | 7 | 7 |
| portfolio_decisions | 2 | 2 |
| risk_checks | 2 | 2 |
| selected_allocations | 1 | 1 |

### ⭐ 핵심 발견 2 — SQLite 와 PG 는 "거울"이 아니라 "상보적"

운영 데이터가 **양쪽에 분산**되어 있다. 어느 한쪽으로 cutover 하면 반대쪽 데이터를 잃는다.

- **PG 에만 있는 운영 데이터**: 인증 전체(`users` 4, `user_sessions` 9, `user_auth_events` 16, `auth_*`, `user_security_settings`, `password_reset_tokens`, `user_account_access`) + `portfolio_decisions` 2, `risk_checks` 2.
- **SQLite 에만 있는 운영 데이터(대량)**: `target_allocations` 120, `task_memory_links` 450, `tasks` 44, `field_consultations` 38, `decisions` 18, `advice_items` 19, `agent_memory_scope` 29, `field_advice_events` 24, `lesson_candidates` 14, `account_snapshots` 14, `investor_profile_history` 12, `rebalance_plans` 12, `rebalance_plan_steps` 51, `portfolio_policies` 5, `allocation_selections` 5, `lessons` 8, `growth_reports` 6, `universe_instruments` 3, `analysis_requests` 3, `consultations` 3, `sync_events` 16, `investor_profile` 1, `daily_portfolio_reviews` 1 등.

**웹 레이어가 이 분산을 그대로 반영한다** (코드 확인):
- 인증: `web/lib/auth/db.ts` + `web/lib/server/pgDb.ts` → `pg.Pool(connectionString=DATABASE_URL)` = **로컬 PG**.
- 조회: `web/lib/server/portfolioDb.ts` → `node:sqlite DatabaseSync(readOnly:true)` = **SQLite**.

---

## 1. 3축 테이블 매핑 (앱 SQLite ↔ 로컬 PG ↔ 원격 PG)

로컬 PG = 원격 PG 이므로 "PG" 한 열로 표기. 41 SQLite 테이블(메타 제외) vs 57 PG 테이블.

### 1-A. 동일 이름 (20개) — 컬럼 차이는 §2 변환 규칙

`account_snapshots, accounts, agent_memories, broker_credentials, consultations, daily_portfolio_reviews, decision_evidence_links, evidence_documents, field_advice_events, field_consultations, lesson_candidates, lessons, market_context_snapshots, orders, portfolio_policies, rebalance_plan_steps, rebalance_plans, scheduled_order_plans, scheduled_order_steps, target_allocations`

### 1-B. 이름이 다른 동일 개념 (rename 매핑) — **데이터 손실 위험 구간**

| SQLite (앱) | PG (canonical) | 비고 |
|---|---|---|
| `investor_profile` (단수, 계좌당 1행, PK=account_index) | `investor_profiles` (복수, surrogate id + account_id) | **단복수 + 구조 차이**. `doc` → `doc_json(jsonb)` |
| `investor_profile_history` | `investor_profile_versions` | `snapshot` → `snapshot_json`, `version` 컬럼 추가됨 |
| `allocation_selections` | `selected_allocations` | `allocation`→`allocation_json`, `policy_version`→`policy_version_id`, `precheck_reasons`→`precheck_reasons_json`, `diff`→`diff_json` |
| `decisions` (payload JSON) | `portfolio_decisions` (정규화) | SQLite=`payload` 통짜 JSON; PG=`payload_json`+`drift_pct`+`risk_reasons_json`+`passed`+FK. **무손실 매핑 불가, 변환 로직 필요** |
| `task_memory_links` | `task_memories` | 컬럼 동일 의미, FK 타입만 bigint |
| `holdings` | `position_snapshots` (+`position_daily_snapshots`) | SQLite `snapshot_id` FK → PG `account_snapshot_id` |
| `quotes` | `price_snapshots` | `freshness` 없음, 단순 매핑 |

### 1-C. SQLite 전용 (PG 에 없음) — cutover 시 **테이블 신설 필요 or 폐기 결정**

| SQLite 전용 | 행수 | PG 대응 / 처리 |
|---|---|---|
| `advice_items` | 19 | PG 미존재 → **신설 필요** (운영 데이터 있음) |
| `agent_memory_scope` | 29 | PG 미존재 → 신설 필요 |
| `analysis_requests` | 3 | PG 미존재 → 신설 필요 |
| `audit_logs` | 0 | PG 미존재. CLAUDE.md 13조 audit 의무 → **PG 에 반드시 신설** |
| `feedback_memory` | 0 | PG 미존재 → 신설(스키마만) |
| `growth_reports` | 6 | PG 미존재 → 신설 필요 |
| `task_failure_patterns` | 2 | PG 미존재 → 신설 필요 |
| `task_regression_tests` | 1 | PG 미존재 → 신설 필요 |
| `theme_suggestion_candidates` | 0 | PG 미존재 → 신설(스키마만) |
| `theme_advice_evidence_links` | 0 | PG: `*_edges` 계열로 흡수 가능 검토 |
| `daily_review_evidence_links` | 0 | PG: `decision_evidence_*` 패턴과 통합 검토 |
| `universe_instruments` | 3 | PG 미존재(소전제 유니버스). **신설 필요** |
| `sync_events` | 16 | PG 미존재 → 신설(freshness 근거) |

> ⚠️ SQLite 전용이면서 **행수>0 인 11개** 가 cutover 시 가장 큰 신규 마이그레이션 작업이자 데이터 이관 대상.

### 1-D. PG 전용 (SQLite 에 없음) — canonical 이 더 발전한 부분

- **인증/보안(8)**: `users, user_sessions, user_account_access, user_auth_events, user_security_settings, password_reset_tokens, auth_sessions, auth_events` — 앱 SQLite 에는 인증 개념 자체 없음(웹 PG 직결).
- **Graph edges(7)**: `account_asset_edges, asset_sector_edges, asset_theme_edges, decision_evidence_edges, decision_risk_edges, etf_holding_edges, account_security_settings/account_auth_*` — growth/graph 승격 산물.
- **history/dashboard(6)**: `account_daily_snapshots, position_daily_snapshots, portfolio_drift_history, dashboard_metrics, allocation_history, strategy_change_events`.
- **growth provenance(4)**: `prehook_runs, posthook_runs, research_runs, memory_retrieval_logs`.
- **기타**: `allocation_options, order_events, risk_checks, portfolio_decisions, selected_allocations, investor_profile(s/_versions), price_snapshots, position_snapshots, task_memories`.

---

## 2. 컬럼 레벨 변환 규칙 (SQLite → PG canonical) — 전 테이블 공통 패턴

| 패턴 | SQLite | PG | 영향 |
|---|---|---|---|
| **계좌 키** | `account_index INTEGER` (자연키, 전 테이블) | `accounts.id`(surrogate PK) + 타 테이블 `account_id BIGINT` FK. `account_index` 는 `accounts` 에만 잔존 | **가장 큰 변환.** 모든 자식행을 `account_index → accounts.id` 로 재매핑 필요 |
| **PK** | `INTEGER PRIMARY KEY` (rowid) | `BIGINT GENERATED ... id` | id 재발번 → 모든 FK 재배선 |
| **JSON** | `TEXT` (`payload`,`doc`,`refs`,`summary`,`allocation`,`precheck_reasons`,`diff`,`snapshot`) | `jsonb` + `_json` 접미사 | 컬럼명 변경 + 캐스팅. 잘못된 JSON 텍스트는 적재 실패 위험 |
| **시각** | `TEXT datetime('now')` (UTC ISO) | `timestamptz` | 파싱·tz 처리 필요 |
| **불리언** | `INTEGER 0/1` (`is_active`,`user_override`,`promoted`) | `boolean` | 0/1 → false/true 캐스팅 |
| **수치** | `REAL` | `numeric` | 무손실(정밀도↑) |

예시(`investor_profile`→`investor_profiles`): `account_index(PK)` → `account_id(FK)`+신규 `id`; `doc` → `doc_json`; `updated_at TEXT` → `timestamptz`; 신규 `created_at`.

---

## 3. Migration 필요 항목 (원격 canonical 기준 정렬)

PG 가 이미 canonical 이므로 cutover 의 실체는 **"앱 compute 를 SQLite→PG 로 전환 + SQLite 잔존 운영 데이터를 PG 로 이관 + PG 에 없는 앱 테이블 신설"** 이다.

1. **스키마 신설(PG)** — §1-C 의 SQLite 전용 13 테이블 중 PG 미존재분: `audit_logs`(필수), `advice_items, agent_memory_scope, analysis_requests, growth_reports, task_failure_patterns, task_regression_tests, universe_instruments, sync_events, feedback_memory, theme_suggestion_candidates` + 링크 2종. (CREATE only, drop 없음)
2. **데이터 이관(SQLite→PG)** — 행수>0 테이블 전부. account_index→account_id 재매핑 + JSON 캐스팅 + tz 변환.
   - 동일이름 20: upsert 변환.
   - rename 7쌍(§1-B): 매핑 함수 경유 적재.
3. **앱 코드 전환** — `DB_BACKEND` 분기 통일, `portfolioDb.ts`(SQLite read) → PG read 로 교체. (코드 작업, 본 분석 범위 밖)
4. **로컬↔원격 일원화** — 현재 둘이 동일하므로, "운영 truth = 원격" 으로 단일화하고 로컬은 replica/dev 로 강등하거나 동기화 정책 명시.

> 로컬·원격 PG 스키마/데이터가 이미 일치하므로 **PG↔PG 마이그레이션은 사실상 불필요**. 위험은 전부 SQLite 축에 있다.

---

## 4. 삭제 위험 Migration 식별 (drop/rename → 데이터 손실 가능 지점)

| # | 위험 | 손실 가능 데이터 | 회피책 |
|---|---|---|---|
| **R1 (최상)** | `investor_profile`(단수) ↔ `investor_profiles`(복수) 를 **rename 으로 처리** 시 단복수 혼동·구조차이로 행 유실 | 프로필 1행 + 히스토리 12 | rename 금지. **CREATE new + INSERT…SELECT 변환** 후 검증, old 보존 |
| **R2 (최상)** | `decisions`(payload 통짜) → `portfolio_decisions`(정규화) 직접 매핑 불가 | SQLite decisions **18행** vs PG 2행 | drop 금지. 변환 ETL 로 payload→정규화 컬럼 추출, 실패행 격리 |
| **R3 (상)** | account_index→account_id 재매핑 중 매칭 실패(고아 FK) 행 누락 | 모든 자식 테이블(target_allocations 120, task_memory_links 450 등) | accounts 선이관 + 매핑 누락 시 **abort**, 부분 적재 금지 |
| **R4 (상)** | SQLite 전용 테이블을 "PG 에 없음" 이유로 **폐기** 결정 | advice_items 19, growth_reports 6, universe_instruments 3, sync_events 16 등 | 폐기 전 CEO 승인. 기본은 신설+이관 |
| **R5 (상)** | 인증 이중화 — SQLite 로 일원화 시 PG 의 users/sessions/auth_events **전부 소실** | users 4, sessions 16, auth_events 24 | **인증은 PG 유지가 정답** (§6). SQLite 로 끌어내리지 말 것 |
| **R6 (중)** | `_json` 접미사 + jsonb 캐스팅 시 비정상 JSON 텍스트 적재 실패를 무시하면 행 누락 | JSON 컬럼 보유 행 | strict 캐스팅 + 실패 로그, NULL 무음 치환 금지 |
| **R7 (중)** | id 재발번 후 옛 정수 FK 가 새 PK 와 어긋남 | 링크/스텝 테이블 | 매핑 테이블 유지하며 FK 재배선, 직접 id 복사 금지 |
| **R8 (중)** | `holdings`→`position_snapshots` 시 `snapshot_id`→`account_snapshot_id` 미변환 | 보유종목 | snapshot 매핑 선행 |

**공통 원칙: cutover 는 append/insert 기반, DROP/TRUNCATE/RENAME 으로 기존 테이블 제거 금지. 검증 통과 전 원본(SQLite) 보존.**

---

## 5. Cutover 순서 초안 (의존성 고려)

FK 의존성: `accounts`(루트) → snapshots/profiles/policies → allocations/decisions → plans/steps → links. 인증은 `users` → sessions/access/events.

0. **준비**: 원격 PG 를 운영 truth 로 고정. 로컬 PG·SQLite 풀백업(논리덤프). cutover 동안 앱 write 동결(read-only 모드).
1. **인증 먼저(이미 PG)**: `users → user_account_access → user_sessions/auth_sessions → user_auth_events/auth_events → user_security_settings → password_reset_tokens`. 이미 PG 에 있으므로 **검증만**(웹 로그인·PIN·RBAC 셀프체크 `web/scripts/*_selfcheck.mjs`).
2. **루트**: `accounts` 정합 확인 + `account_index → accounts.id` 매핑 테이블 생성(메모리/임시).
3. **스냅샷 축**: `account_snapshots → holdings(→position_snapshots) → quotes(→price_snapshots) → account_daily_snapshots`.
4. **프로필/정책 축**: `investor_profile(→investor_profiles) → investor_profile_history(→investor_profile_versions) → portfolio_policies`.
5. **얼로케이션/결정 축**: `target_allocations → allocation_selections(→selected_allocations) → decisions(→portfolio_decisions, ETL) → rebalance_plans → rebalance_plan_steps → scheduled_order_plans/steps → daily_portfolio_reviews`.
6. **메모리/성장 축**: `lessons, lesson_candidates, agent_memories, agent_memory_scope, tasks, task_memory_links(→task_memories), feedback_memory, growth_reports, task_failure_patterns, task_regression_tests`.
7. **근거/조언 축**: `evidence_documents → *_evidence_links, advice_items, analysis_requests, consultations, field_consultations, field_advice_events, theme_suggestion_candidates`.
8. **신설 전용**: `audit_logs`(필수), `sync_events, universe_instruments` 등 PG 미존재분 CREATE+이관.
9. **앱 전환**: `portfolioDb.ts` 읽기 경로 PG 전환, `DB_BACKEND=postgres` 고정. 셀프체크 통과 후 write 동결 해제.
10. **검증 게이트**: 단계별 행수 대조(원본=대상), 고아 FK 0, JSON 캐스팅 실패 0, 웹 로그인+조회 스모크.

각 단계는 독립 트랜잭션, 실패 시 해당 단계만 롤백 후 재시도.

---

## 6. Rollback Plan 초안

| 시점 | 롤백 절차 |
|---|---|
| 사전 | cutover 직전 **3종 백업**: 원격 PG `pg_dump`(논리), 로컬 PG `pg_dump`, SQLite 파일 복사(`portfolio.sqlite3` + WAL). 백업 무결성 확인 후에만 진행 |
| 단계 실패 | 해당 트랜잭션 ROLLBACK. 매핑 임시테이블 폐기. SQLite 원본 무변경이므로 재시도 가능 |
| 앱 전환 후 장애 | `DB_BACKEND=sqlite` 로 즉시 환원 + `portfolioDb.ts` 이전 커밋 복귀(코드 롤백). SQLite 는 read-only 였으므로 손상 없음 |
| PG 데이터 오염 | 신규 적재분만 영향: cutover 세션 시작 시각 이후 INSERT 를 `created_at` 기준 식별·격리, 또는 사전 pg_dump 로 PITR 복원 |
| 인증 | 인증은 PG 유지(이관 안 함)이므로 별도 롤백 불요. 단 step1 검증 실패 시 cutover 전체 중단 |

**불변식**: SQLite 원본은 cutover 검증 100% 통과 + N일 안정 운영 전까지 **삭제 금지**(read-only 보존).

---

## 7. Auth Truth 이중화 위험 분석

### 현재 상태
- **웹 인증 = 로컬 PG 직결** (`web/lib/auth/db.ts`, `web/lib/server/pgDb.ts` → `DATABASE_URL`). users 4 / user_sessions 9 / user_auth_events 16 / user_security_settings 1 / password_reset_tokens 2 / user_account_access 2.
- **PG 인증 테이블이 2계열 공존**:
  - **유저 인증**(800_user_auth): `users, user_sessions, user_account_access, user_auth_events, password_reset_tokens` (PK `user_id BIGINT`, `users.login_id`).
  - **PIN/계좌 게이트**(400_account_auth): `auth_sessions, auth_events, user_security_settings, account_auth_*, account_security_settings` (PK `user_id TEXT`).
  - → **user_id 타입 불일치(bigint vs text)** 와 세션 테이블 중복(`user_sessions` vs `auth_sessions`)이 이미 잠재 이중화. cutover 와 별개로 정리 권장.

### Cutover 시 위험
1. **앱 compute 를 PG 로 옮기면 인증과 동일 DB 공유** → 좋음(단일 truth 가능). 단 앱이 잘못 SQLite 인증을 만들면 안 됨(SQLite 에 인증 테이블 없음 = 자연 차단).
2. **로컬 PG ↔ 원격 PG 인증 데이터 정합**: 현재 둘이 동일하지만, "운영=원격" 으로 단일화하면 **웹의 `DATABASE_URL` 을 원격으로 바꿔야** 인증이 같은 곳을 본다. 안 바꾸면 사용자는 로컬에, 앱 compute 는 원격에 쓰여 **세션/권한 split-brain** 발생.
3. **권한(user_account_access) 정합**: `account_index` 기반. accounts.id 재발번(§4 R3) 시 access 의 account_index 와 어긋나지 않도록 — access 는 account_index 유지(자연키)이므로 accounts.account_index 보존 필수.

### 권장(분석 의견, 적용은 승인 후)
- **인증 truth = PG 단일** 으로 명시하고, cutover 시 **웹 `DATABASE_URL` 을 원격(192.168.0.107)로 통일**. 로컬 PG 는 dev/replica 로 강등.
- 2계열 인증(bigint user_id vs text user_id)을 **하나로 수렴**하는 별도 정리 태스크 분리(데이터 4+16행 소량이라 지금이 적기).
- cutover 동안 신규 로그인/세션 동결(짧은 유지보수 창), split-brain 원천 차단.

---

## 8. 금지 사항 재확인 (본 작업 준수)

- write 0 / migration apply 0 / 데이터 삭제 0 / secret 출력 0. 모든 psql 은 `SELECT`·카탈로그 조회만. 비밀번호는 마스킹·미기록.
- 본 문서는 **분석 산출물 1개**(`docs/portfolio/pg_gap_analysis.md`)만 생성. 코드/스키마/DB 무변경.

---

## 9. 요약 (CEO 보고용)

- **접속**: 로컬 PG·원격 PG·SQLite 모두 접속 성공.
- **반전된 전제**: 로컬 PG 와 원격 PG 는 스키마(585컬럼)·데이터(행수) **완전 동일** — 원격만 canonical 이 아니라 둘 다 canonical. 진짜 gap 은 **PG ↔ SQLite**.
- **분산 위험**: 운영 데이터가 PG(인증·결정·리스크)와 SQLite(프로필·얼로케이션·메모리·태스크 대량)에 **상보적으로 흩어짐**. 한쪽 일원화 = 반대쪽 손실.
- **삭제 위험 Top**: R1 `investor_profile↔profiles` 단복수 rename, R2 `decisions(18)→portfolio_decisions` 비정규 변환, R5 인증 SQLite 강등 금지, R3 account_index→account_id 재매핑 고아.
- **순서**: 인증(검증만)→accounts→스냅샷→프로필/정책→얼로/결정→메모리→근거→신설(audit_logs)→앱전환→검증.
- **rollback**: 3종 백업 + 단계 트랜잭션 + `DB_BACKEND=sqlite` 즉시 환원. SQLite 원본 보존.
- **auth 정합**: 인증 truth = PG 단일화, 웹 `DATABASE_URL` 을 원격으로 통일해 split-brain 차단. PG 내 2계열 인증(bigint/text user_id) 수렴은 별도 태스크.
- **적용은 CEO 승인 후 별도.**
