# PG Cutover — SQLite ↔ PostgreSQL 전환 (Track C)

> 목적: 현 운영(SQLite)을 깨지 않고 PostgreSQL 운영-truth 로 전환하는 **capability** 를 opt-in 으로 구축.
> 최종 "PG primary 로 flip" 은 중앙 DB/Architecture owner(CEO 위임)가 수행. 본 문서는 gap·순서·규칙의 SSOT.

- **기본 백엔드 = SQLite** (`DB_BACKEND` 미설정 또는 `sqlite`). 깨짐 0 보장.
- **PG 활성화 = opt-in**: `.env` 의 `DB_BACKEND=postgres` (또는 `postgresql`) + `DATABASE_URL` 존재.
- **자격증명/DATABASE_URL 미노출**: 코드/로그/에러/DB 어디에도 평문 금지 (.env 전용).
- **PG 스키마 = `portfolio`** (42 tables). `public` 운영 테이블 = **0** (강제, search_path=portfolio).
- **롤(role)**: `portfolio_app` = rw (실제로는 INSERT/SELECT/UPDATE — **DELETE 없음 → 운영 truth 는 append-only**).

---

## 1. 이름 매핑 (SQLite → PostgreSQL)

| SQLite | PostgreSQL (schema=portfolio) | 비고 |
|---|---|---|
| `accounts` (PK `account_index`) | `accounts` (PK `id` bigint, **UNIQUE** `account_index`) | 키 구조 변경 — surrogate id 도입 |
| `account_snapshots` (`account_index`) | `account_snapshots` (`account_id` → accounts.id) | **FK 키 변경** (index→id) |
| `holdings` (`snapshot_id`,`account_index`) | `position_snapshots` (`account_id`,`account_snapshot_id`) | **이름+키 변경** |
| `quotes` (`ticker`) | `price_snapshots` (`ticker`) | 이름 변경, 구조 동일 |
| `decisions` (`payload`) | `portfolio_decisions` | 이름 변경 |
| `allocation_selections` (`allocation` TEXT) | `selected_allocations` (`allocation_json` jsonb, `account_id`) | 이름+타입(jsonb)+키 변경 |
| `investor_profile` | `investor_profiles` | 이름 복수형 |
| `investor_profile_history` | `investor_profile_versions` | 이름 변경 |
| `rebalance_plans` / `rebalance_plan_steps` | 동일 이름 존재 (`account_id`) | 키 index→id |
| `target_allocations` | `target_allocations` | 키 index→id |
| `portfolio_policies` | `portfolio_policies` | 키 index→id, jsonb 가능 |
| `lessons` / `lesson_candidates` | 동일 이름 존재 | scope 컬럼 차이 확인 필요 |
| `consultations` / `evidence_documents` / `decision_evidence_links` / `orders` | 동일 이름 존재 | 키 index→id 매핑 |

---

## 2. 핵심 테이블 분류 (cutover 관점)

| SQLite 테이블 | 분류 | 메모 |
|---|---|---|
| `accounts` | **schema-differs** | PG 는 surrogate `id` PK + `account_index` UNIQUE + boolean/timestamptz. write 시 `id` 매핑 필요. |
| `account_snapshots` | **schema-differs** | `account_index`→`account_id`(FK). money=numeric, captured_at=timestamptz. |
| `holdings` | **schema-differs (renamed)** | → `position_snapshots`. `account_id`+`account_snapshot_id` 둘 다 필요. |
| `quotes` | **already-in-PG (renamed)** | → `price_snapshots`. 구조 사실상 동일. |
| `decisions` | **already-in-PG (renamed)** | → `portfolio_decisions`. |
| `allocation_selections` | **schema-differs (renamed)** | → `selected_allocations`. TEXT→jsonb, `account_id`. **선택규칙(active/override) 보존 필수.** |
| `investor_profile(_history)` | **renamed** | → `investor_profiles` / `investor_profile_versions`. |
| `sync_events` | **SQLite-only** | **PG 에 미존재.** 현재 PG 스키마에 동등 테이블 없음 → app 은 PG 기록을 **graceful skip** (아래 §4). 중앙 머지에서 PG `sync_events` 추가 여부 결정. |
| `universe_instruments` | **SQLite-only(현재)** | PG 엔, 자산/엣지 그래프(`account_asset_edges`, `asset_sector_edges` 등)로 정규화된 형태 — 1:1 아님. 별도 매핑 작업 필요. |
| `audit_logs` | **SQLite-only(현재)** | PG 측 동등 미확인 — 중앙 머지 검토. |
| `agent_memories`, `lessons`, `lesson_candidates`, `consultations`, `evidence_documents`, `decision_evidence_links`, `orders`, `target_allocations`, `rebalance_plans`, `rebalance_plan_steps`, `portfolio_policies` | **already-in-PG** | 이름 동일 — 키 index→id 매핑만. 본 Track C 범위 밖(읽기/쓰기 별도 작업). |
| `tasks`, `task_memory_links`, `agent_memory_scope`, `feedback_memory`, `advice_items`, `analysis_requests` | **SQLite-only(현재)** | PG 측 정규화/이름 상이 — 별도 매핑. |
| (PG only) `auth_events`,`auth_sessions`,`user_security_settings`,`order_events`,`prehook_runs`,`posthook_runs`,`research_runs`,`dashboard_metrics`,`*_edges`,`portfolio_drift_history`,`strategy_change_events`,`account_daily_snapshots`,`position_daily_snapshots`,`risk_checks`,`memory_retrieval_logs`,`allocation_options`,`allocation_history`,`task_memories`,`investor_profile_versions` | **PG-only-unused-by-app(현재)** | PG 에 존재하나 현 SQLite app 코드가 미사용. 점진적으로 PG 경로가 활용. |

### 본 Track C 가 실제로 쓰는(운영-truth write) 테이블
`accounts`, `account_snapshots`, `position_snapshots`, `price_snapshots` (+ `sync_events` 가 있으면). 모두 검증 완료(§ 라운드트립).

---

## 3. Cutover 순서 (권장)

1. **(완료)** opt-in 코드 경로 구축 — SQLite 기본 무손상, PG 는 `DB_BACKEND=postgres` 일 때만.
2. **(완료)** dual-write: sync job 이 SQLite 에 쓰면서 **추가로** PG 에도 기록(데이터 손실 0).
3. **검증기간(dual-truth)**: SQLite 가 primary read, PG 는 shadow. 두 백엔드 합산이 일치하는지 모니터.
4. **read flip (중앙)**: 웹이 `pgDb.ts` 로 읽도록 스위치 (한 view = 한 백엔드, §5).
5. **write flip (중앙)**: SQLite write 비활성, PG primary.
6. **decommission**: SQLite 보존(백업) 후 read 경로 제거.

> **Dual-truth 규칙(불변)**: 전환기에 두 DB 가 공존해도, **하나의 운영 view/의사결정은 정확히 한 백엔드에서만** 읽는다.
> 한 화면에서 PG+SQLite 를 섞으면 진실원천이 깨진다 → **hard-block** (`assert_single_backend` / `assertSingleBackend`).

---

## 4. `sync_events` 부재 처리 (중요)

- 현 `portfolio` 스키마에 `sync_events` 테이블이 **없음**.
- `store/pg.py:insert_sync_event()` 는 `information_schema` 로 존재를 먼저 확인 → 없으면 **조용히 skip(False 반환)**, 에러 없음.
- 따라서 dual-write 시 sync 이력은 SQLite 에만 남고 PG 에는 빠진다(허용 — sync_events 는 freshness 근거이지 금액 truth 아님).
- 중앙 머지 결정사항: PG 에 `sync_events`(혹은 기존 `prehook_runs`/`posthook_runs` 재활용) 를 추가할지.

---

## 5. 백엔드 선택 / 가드 (구현 위치)

- Python: `main_mission/portfolio_os/store/backend.py`
  - `current_backend()` → `'sqlite'|'postgres'` (기본 sqlite, 미지값은 안전하게 sqlite).
  - `is_postgres()` / `is_sqlite()`.
  - `require_database_url()` → DATABASE_URL 없으면 `RuntimeError("DATABASE_URL 미설정 …")` (**값 미노출, silent fallback 금지**).
  - `assert_single_backend(set)` → DualTruthError (혼합 금지).
- Web: `web/lib/server/pgDb.ts`
  - `pgEnabled()` = `DB_BACKEND==='postgres' && DATABASE_URL`.
  - `assertSingleBackend(Set)` → `DualTruthError`.
  - 기존 `portfolioDb.ts`(SQLite)는 **무변경** — pgDb 는 병렬 모듈, 중앙 머지가 스위치 배선.

---

## 6. 중앙 머지용 인터페이스 (function names)

**Python `store/pg.py`** (context-managed `connect()` — search_path=portfolio 강제, commit/rollback 자동):
`upsert_account(...)`, `account_id_for(conn, account_index)`, `update_account_status(...)`,
`insert_account_snapshot(...)`, `insert_position_snapshots(...)`, `insert_price_snapshots(...)`,
`insert_sync_event(...)`(테이블 없으면 skip), `fetch_account_snapshot_latest(conn, account_id)`,
`psycopg_available()`.

**Python `store/backend.py`**: `current_backend()`, `is_postgres()`, `is_sqlite()`, `require_database_url()`, `assert_single_backend(set)`, `DualTruthError`.

**Web `pgDb.ts`** (조회 전용): `pgEnabled()`, `getAccountId(idx)`, `getAccounts()`, `getLatestSnapshot(idx)`, `getPositions(snapshotId)`, `getCurrentSelection(idx)`, `getAccountView(idx)`, `assertSingleBackend(Set)`, `DualTruthError`.

**Sync dual-write**: `broker/sync_job.py` 가 `backend.is_postgres()` 일 때 SQLite write **이후 추가로** `_pg_write_balance_ok/_pg_write_balance_error` 호출. PG 실패는 try/except 로 흡수(자격증명 미노출 로그), sync 미중단. SQLite 경로는 절대 제거 안 됨.

---

## 7. 안전 불변식 (전환 중에도 유지)

- live-order lock / PIN / selected-allocation(active/override) 규칙 **무변경** — Track C 는 운영-truth 수집·조회만 다룸.
- money = numeric(PG). account_id 격리: 모든 write 가 account_id 동반.
- 운영 truth append-only (portfolio_app DELETE 권한 없음) — 갱신은 새 snapshot row 로.
