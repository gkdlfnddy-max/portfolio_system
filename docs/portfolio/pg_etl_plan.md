# PG 정렬 ETL — 설계 + Dry-run (Track 4)

> **상태: 설계 + read-only dry-run 전용. write 0 · cutover 0 · 삭제 0.**
> 실제 적용(데이터 적재/스키마 신설/앱 전환)은 **CEO 승인 후 별도 작업**으로만 수행한다.
> 전제 분석: [pg_gap_analysis.md](pg_gap_analysis.md) (Track G — 로컬 PG ≡ 원격 PG, PG↔SQLite 상보).
> dry-run 도구: [`main_mission/portfolio_os/store/etl_dryrun.py`](../../main_mission/portfolio_os/store/etl_dryrun.py) (read-only, write 0).
> 작성: 2026-06-21.

---

## 0. 본질 / 비목표

- **본질**: SQLite(앱 compute)에만 있는 운영 데이터를 **PG canonical 로 정렬(이관)** 하기 위한 설계와, 그 안전성을 **SELECT 만으로 자증**하는 dry-run.
- **비목표(금지)**: 무리한 일원화 아님. 인증은 PG 유지(끌어내리지 않음). 본 트랙에서 실 write/cutover 없음.
- **상보 원칙 유지**: 인증/결정/리스크 = PG truth, 얼로케이션/메모리/태스크 = (현재) SQLite. ETL 은 후자를 PG 로 **append/insert** 정렬하는 것이지 SQLite 를 삭제하는 것이 아니다.

---

## 1. PG ↔ SQLite 매핑 표

### 1-A. 키/타입 변환 공통 규칙

| 패턴 | SQLite | PG canonical | 변환 규칙 |
|---|---|---|---|
| 계좌 키 | `account_index INTEGER` (자연키, 전 자식테이블) | `accounts.id BIGINT`(surrogate PK) + 자식 `account_id BIGINT` FK | `account_index → accounts.id` 룩업 후 `account_id` 로 적재. `accounts` 는 `account_index` UNIQUE 보존(자연키) |
| PK | `INTEGER PRIMARY KEY`(rowid) | `BIGINT GENERATED ALWAYS AS IDENTITY` | id **재발번**. 매핑테이블(old_id→new_id) 유지하며 FK 재배선. 직접 id 복사 금지 |
| JSON | `TEXT` (`payload`,`doc`,`snapshot`,`allocation`,`precheck_reasons`,`diff` 등) | `jsonb` + `_json` 접미사 | strict 캐스팅. 비정상 JSON 은 적재 실패 → 격리 로그(무음 NULL 치환 금지) |
| 시각 | `TEXT` (UTC ISO `datetime('now')`) | `timestamptz` | ISO 파싱 → tz-aware. created_at 없으면 now() |
| 불리언 | `INTEGER 0/1` | `boolean` | 0/1 → false/true |
| 수치 | `REAL` / `INTEGER(krw)` | `numeric` | 무손실(정밀도↑) |

### 1-B. 테이블 매핑 (동일 이름 / rename / 신설)

| SQLite | PG canonical | 종류 | 비고 |
|---|---|---|---|
| `accounts`(account_index PK) | `accounts`(id PK, account_index UNIQUE) | 루트 | 이미 PG 존재(2행). account_index 보존 |
| `target_allocations` | `target_allocations` | 동일 | account_index→account_id |
| `account_snapshots` | `account_snapshots` | 동일 | id 재발번 → 자식(holdings/quotes) 재배선 |
| `portfolio_policies` | `portfolio_policies` | 동일 | jsonb 컬럼들 캐스팅 |
| `rebalance_plans` / `rebalance_plan_steps` | 동일 | 동일 | decision_id/plan_id 재배선 |
| `scheduled_order_plans` / `_steps` | 동일 | 동일 | |
| `lessons` / `lesson_candidates` | 동일 | 동일 | 성장 메모리 |
| `agent_memories`, `evidence_documents`, `consultations`, `field_consultations`, `field_advice_events`, `daily_portfolio_reviews`, `market_context_snapshots` | 동일 | 동일 | |
| **`investor_profile`(단수, PK=account_index)** | **`investor_profiles`(복수, id PK + account_id)** | rename(**R1**) | `doc`→`doc_json`. rename 금지, CREATE+INSERT…SELECT 변환 |
| **`investor_profile_history`** | **`investor_profile_versions`** | rename | `snapshot`→`snapshot_json`, `version` 컬럼 신규 |
| **`allocation_selections`** | **`selected_allocations`** | rename | `allocation`→`allocation_json`, `policy_version`→`policy_version_id`, `precheck_reasons`→`precheck_reasons_json`, `diff`→`diff_json` |
| **`decisions`(payload 통짜)** | **`portfolio_decisions`(정규화)** | rename(**R2**, §3) | payload→payload_json + drift_pct/passed/risk_reasons_json 추출 |
| `task_memory_links` | `task_memories` | rename | account_id 보강(없으면 task→account 역추적, 불가 시 NULL) |
| `tasks`, `advice_items`, `agent_memory_scope`, `analysis_requests`, `growth_reports`, `task_failure_patterns`, `task_regression_tests`, `universe_instruments`, `sync_events` | (없음) | **신설** | PG 미존재(dry-run 확인). CREATE only |
| `audit_logs` | (없음) | **신설 필수** | CLAUDE.md 13조 audit 의무 |

> `account_index↔account_id` 와 `investor_profile 단↔investor_profiles 복`이 핵심 변환 2축이며, 둘 다 손실 위험 구간(§3·§4).

---

## 2. ETL 후보 테이블 선정 (SQLite 전용 대량 데이터 → PG canonical)

dry-run 측정 기준(2026-06-21, SQLite). PG canonical 측은 대부분 비어 있어(이관 미수행 상태) **SQLite → PG 일방 정렬**이 대상이다.

| 우선 | SQLite 테이블 | 행수 | PG 대상 | 종류 |
|---|---|---:|---|---|
| 1 | `task_memory_links` | 490 | `task_memories` | rename |
| 1 | `target_allocations` | 120 | `target_allocations` | 동일 |
| 1 | `tasks` | 48 | (신설) | 신설 |
| 1 | `field_consultations` | 38 | `field_consultations` | 동일 |
| 1 | `agent_memory_scope` | 29 | (신설) | 신설 |
| 1 | `field_advice_events` | 24 | `field_advice_events` | 동일 |
| 1 | `decisions` | 20 | `portfolio_decisions` | rename+ETL(§3) |
| 1 | `advice_items` | 19 | (신설) | 신설 |
| 1 | `sync_events` | 16 | (신설) | 신설 |
| 2 | `lesson_candidates` | 14 | `lesson_candidates` | 동일 |
| 2 | `rebalance_plans` | 14 | `rebalance_plans` | 동일 |
| 2 | `account_snapshots` | 14 | `account_snapshots` | 동일 |
| 2 | `investor_profile_history` | 12 | `investor_profile_versions` | rename |
| 2 | `lessons` | 8 | `lessons` | 동일 |
| 2 | `growth_reports` | 6 | (신설) | 신설 |
| 2 | `portfolio_policies` | 5 | `portfolio_policies` | 동일 |
| 2 | `allocation_selections` | 5 | `selected_allocations` | rename |
| 3 | `rebalance_plan_steps` | 61 | `rebalance_plan_steps` | 동일 |
| 3 | `scheduled_order_steps` | 15 | `scheduled_order_steps` | 동일 |
| 3 | `consultations`/`analysis_requests`/`universe_instruments`/`scheduled_order_plans` | 3·3·3·3 | 동일/신설 | |
| 3 | `agent_memories`/`market_context_snapshots`/`daily_portfolio_reviews` | 2·2·2 | 동일 | |
| 3 | `investor_profile`/`evidence_documents`/`task_regression_tests` | 1·1·1 | rename/동일/신설 | |
| 3 | `task_failure_patterns` | 2 | (신설) | 신설 |

- **ETL 후보 SQLite 총 행수**: **981행** (dry-run §4).
- **PG 신설 필요(행수>0)**: **9테이블** (`tasks, advice_items, agent_memory_scope, analysis_requests, growth_reports, task_failure_patterns, task_regression_tests, universe_instruments, sync_events`). + `audit_logs`(0행이나 신설 필수).

---

## 3. decisions ETL 설계 (통짜 payload → portfolio_decisions 정규화)

### PG 대상 컬럼 (canonical, `migrations/pg/100_core.sql`)
`id(IDENTITY)`, `account_id FK`, `selected_allocation_id FK?`, `account_snapshot_id FK?`, `payload_json jsonb NOT NULL`, `drift_pct numeric`, `risk_reasons_json jsonb`, `passed boolean`, `created_at timestamptz`.

### SQLite 원본
`decisions(id, account_index, payload TEXT, created_at TEXT)`. payload top-level 키(dry-run 측정): `ok, account_index, total_value_krw, cash_current_pct, cash_target_pct, target_sum_pct, lines[], risk{}, provenance{}, sector_exposure[], hedge_*, net/gross_exposure_pct, selected_variant, snapshot_at, ...`.

### 매핑 규칙 (무손실 우선 — 원본 통짜는 payload_json 에 그대로 보존)

| PG 컬럼 | 추출 규칙 | dry-run 검증 |
|---|---|---|
| `payload_json` | `payload` 텍스트 → `jsonb` strict 캐스팅 (전체 보존) | 파싱 성공 **20/20** |
| `account_id` | `account_index` → `accounts.id` 룩업 | account 1 → id 1 (매칭 100%) |
| `passed` | `payload.ok` (bool) | 추출 가능 20/20 |
| `risk_reasons_json` | `payload.risk` (object) → jsonb | 추출 가능 20/20 |
| `drift_pct` | `abs(payload.cash_current_pct - payload.cash_target_pct)` (없으면 NULL). 체크제약 0~100 준수 | 산출 가능 20/20 |
| `selected_allocation_id` | 매핑테이블(allocation_selections→selected_allocations old→new). 매칭 없으면 NULL | 적재 시 결정 |
| `account_snapshot_id` | account_snapshots 매핑테이블. 없으면 NULL | 적재 시 결정 |
| `created_at` | `created_at` ISO TEXT → timestamptz | |

- **무손실 보장**: 정규화 추출 실패(키 부재)해도 `payload_json` 에 원본 전체가 남으므로 정보 손실 0. 핫컬럼은 조회 가속용.
- **실패행 격리**: payload JSON 캐스팅 실패 시 그 행은 적재 중단·격리 로그(현재 dry-run 기준 실패 0). 무음 NULL 치환 금지(R6).
- **drift_pct 정의 주의**: cash drift 근사치. 종목단 drift 가 필요하면 `lines[].current_pct/target_pct` 합산으로 재정의 가능(설계 옵션, 적용 전 합의).

---

## 4. account_index → accounts.id 매핑 + 고아 FK (dry-run 실측)

### 매핑 규칙
1. cutover 0단계에서 `accounts` 선이관/정합 → PG `account_index` UNIQUE 보존.
2. 메모리 매핑테이블 `m[account_index] = accounts.id` 구축 (PG 실측: `{1: 1, 104: 257}`).
3. 모든 자식행 적재 시 `account_id = m[account_index]`. **매칭 실패 시 해당 행 abort/격리** (부분 적재 금지, R3).

### dry-run 매칭률 (read-only SELECT 산출)

| 항목 | 값 |
|---|---|
| SQLite accounts account_index | `[1]` |
| PG accounts account_index→id | `{1→1, 104→257}` |
| 자식행 총계(10개 핵심 자식테이블) | 277 |
| 매칭 | 271 |
| 고아 | **6** |
| **account_index 매칭률** | **97.8%** |

- **고아 발견**: `target_allocations` 에 `account_index=35` **6행** — PG·SQLite accounts 어디에도 없는 계좌. ETL 시 abort 또는 별도 격리 대상.
- 나머지 자식테이블(tasks·decisions·investor_profile 등)은 전부 `account_index=1` → 100% 매칭.
- **조치**: cutover 전 `account_index=35` 행의 출처 확인(테스트 잔여 추정) → 폐기/보정 결정은 CEO 승인. 자동 폐기 금지.

---

## 5. investor_profile 단/복수 정리안 + `.env POSTGRES_DB` 정리안

### 5-A. investor_profile(단) → investor_profiles(복) (R1)
- **rename 금지.** PG `investor_profiles`(id PK, account_id FK, doc_json, created_at/updated_at) 는 **계좌당 1행 + 이력 분리** 모델.
- 변환: SQLite 단수 1행 → `INSERT … SELECT` 로 `account_id=m[account_index]`, `doc→doc_json` 캐스팅, 신규 `id` 발번. UNIQUE(account_id) 충족(계좌당 1행).
- 이력은 `investor_profile_history(12행)` → `investor_profile_versions`(snapshot→snapshot_json, version 부여).
- old(SQLite) 보존 → 검증 통과까지 삭제 금지.

### 5-B. `.env POSTGRES_DB` 정리 (미사용 변수)
- 현상: `.env POSTGRES_DB=portfolio_os_db` 이나 앱 실제 사용 `DATABASE_URL` 의 DB명은 **`portfolio_os`**. `POSTGRES_DB`/`POSTGRES_HOST/PORT/USER` 는 코드 경로(`require_database_url()` → `DATABASE_URL`)에서 **미참조**(혼동원).
- 정리안(적용은 승인 후, 본 트랙 write 0):
  1. `POSTGRES_DB` 값을 `portfolio_os` 로 정정(혼동 제거), 또는
  2. `POSTGRES_*` 개별 변수 자체를 주석화/삭제하고 `DATABASE_URL` 단일 소스로 일원화(권장 — backend.py 가 이미 DATABASE_URL 만 읽음).
- 인증/접속은 PG 유지. 정정은 변수 정리이지 접속 변경 아님.

---

## 6. etl_dryrun.py (read-only 검증 스크립트)

경로: [`main_mission/portfolio_os/store/etl_dryrun.py`](../../main_mission/portfolio_os/store/etl_dryrun.py)

- **write 0 자증**: 본 파일에 `INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/TRUNCATE/RENAME` 실행문 **없음**(키워드는 docstring·주석·print 문자열에만 등장). 모든 `.execute()` 는 `SELECT`/`PRAGMA` 뿐.
- **2중 read-only 가드**: SQLite `file:...?mode=ro`(OS 레벨 쓰기 차단) + PG `default_transaction_read_only=on`(세션 read-only).
- **secret 0**: `DATABASE_URL`/비밀번호 미출력. `backend.require_database_url()` 만 사용, 값 미표시.
- **PG 미접속 graceful**: psycopg2 미설치/접속 실패 시 SQLite 단독 섹션만 출력(매칭률은 보류).
- **산출 섹션**: [1] row count 비교 · [2] account_index 매칭률·고아 · [3] decisions 정규화 추출 가능률 · [4] ETL 대상 행수.
- 실행: `python -m main_mission.portfolio_os.store.etl_dryrun`

### 검증 명령 (자증)
```bash
grep -niE '\b(insert|update|delete|create|drop|alter|truncate|rename)\b' \
  main_mission/portfolio_os/store/etl_dryrun.py   # docstring/주석/print 만 매칭
grep -nE '\.execute\(' main_mission/portfolio_os/store/etl_dryrun.py  # 전부 SELECT/PRAGMA
```

---

## 7. Cutover 순서 + 검증 게이트 + Rollback (문서화 — 적용은 승인 후)

### 7-A. 순서 (FK 의존성: accounts → snapshots/profiles → allocations/decisions → plans/steps → links)
0. **준비**: 3종 백업(§7-C) + 무결성 확인. 앱 write 동결(read-only 창). `account_index=35` 고아 처리 결정.
1. **인증(이미 PG)**: 검증만 — 웹 로그인/PIN/RBAC 셀프체크. 이관 안 함(R5).
2. **루트**: `accounts` 정합 + `account_index→accounts.id` 매핑테이블 생성.
3. **스냅샷 축**: `account_snapshots → holdings(→position_snapshots) → quotes(→price_snapshots)`.
4. **프로필/정책 축**: `investor_profile(→investor_profiles) → investor_profile_history(→investor_profile_versions) → portfolio_policies`.
5. **얼로/결정 축**: `target_allocations → allocation_selections(→selected_allocations) → decisions(→portfolio_decisions ETL §3) → rebalance_plans → rebalance_plan_steps → scheduled_order_plans/steps → daily_portfolio_reviews`.
6. **메모리/성장 축**: `lessons, lesson_candidates, agent_memories, agent_memory_scope, tasks, task_memory_links(→task_memories), growth_reports, task_failure_patterns, task_regression_tests`.
7. **근거/조언 축**: `evidence_documents, advice_items, analysis_requests, consultations, field_consultations, field_advice_events`.
8. **신설 전용**: `audit_logs`(필수) + `sync_events, universe_instruments` 등 CREATE+이관.
9. **앱 전환**: `portfolioDb.ts` 읽기 경로 PG 전환, `DB_BACKEND=postgres` 고정 + 웹 `DATABASE_URL` 원격 통일(split-brain 차단). 셀프체크 후 write 동결 해제.

각 단계 독립 트랜잭션. 실패 시 해당 단계만 ROLLBACK 후 재시도.

### 7-B. 검증 게이트 (단계별 통과 조건)
- **row count**: 적재 후 `PG 행수 == SQLite 원본 행수`(고아 제외분 정확 일치). dry-run 표를 기준선으로 사용.
- **account_index 매칭률 100%**(고아 제외) — 고아 행은 격리 카운트로 별도 보고, 부분 적재 0.
- **checksum/sample**: JSON 컬럼 표본 N행 round-trip(payload_json::text 정규화 비교), 핵심 수치합(weight_pct 합, total_value_krw) 원본=대상.
- **JSON 캐스팅 실패 0**, **고아 FK 0**(매핑 누락 0).
- **앱 스모크**: 웹 로그인 + 포트폴리오 조회 + 결정 조회 정상.
- 게이트 1개라도 실패 → 해당 단계 ROLLBACK, cutover 중단.

### 7-C. Rollback Plan (3종 백업 + 즉시 환원)
| 시점 | 절차 |
|---|---|
| 사전 | **3종 백업**: ① 원격 PG `pg_dump`(논리) ② 로컬 PG `pg_dump` ③ SQLite 파일 복사(`portfolio.sqlite3`+WAL). 무결성 확인 후만 진행 |
| 단계 실패 | 트랜잭션 ROLLBACK + 매핑 임시테이블 폐기. SQLite 원본 무변경이라 재시도 가능 |
| 앱 전환 후 장애 | **`DB_BACKEND=sqlite` 즉시 환원** + `portfolioDb.ts` 이전 커밋 복귀. SQLite 는 read-only 였으므로 손상 0 |
| PG 오염 | cutover 세션 시작시각 이후 INSERT 를 `created_at` 기준 격리, 또는 사전 pg_dump 복원(PITR) |
| 인증 | PG 유지(이관 안 함) → 별도 롤백 불요. step1 검증 실패 시 전체 중단 |

**불변식**: SQLite 원본은 검증 100% 통과 + N일 안정 운영 전까지 **삭제 금지(read-only 보존)**.

---

## 8. 금지 사항 준수 확인 (본 트랙)

- INSERT/UPDATE/DELETE/DDL **0** (etl_dryrun.py grep 자증, §6).
- 운영데이터 삭제 **0** · 삭제 migration **0** · secret 출력 **0**.
- auth = PG 유지(SQLite 강등 금지).
- SQLite 데이터 손실 **0 계획**(원본 보존 + append/insert only + 고아 격리).
- 실 cutover **0** (CEO 승인 후 별도 작업).
