# PostgreSQL 승격 (Postgres Migration) — Portfolio OS

> 목표: SQLite(MVP) → PostgreSQL(운영 truth) **점진 승격**.
> 무리한 일괄 이전 금지. 핵심 truth 테이블부터, 멱등 마이그레이션으로 단계 적용한다.
> 자격증명·DATABASE_URL·비밀번호는 **절대 로그·git·화면에 노출 금지** → `.env` 전용 (CLAUDE.md §2-8, §17).

관련: [store/schema.sql](../../main_mission/portfolio_os/store/schema.sql) (현행 SQLite) ·
[growth_architecture.md](growth_architecture.md) (§4.10 승격 매핑) · [db_schema.md](db_schema.md)

---

## 1. 원칙

1. **점진 승격**: 한 번에 모든 테이블을 옮기지 않는다. 우선순위(§6) 1번부터 차례로.
2. **append-only 보호**: history/selections/consultations/tasks/feedback/audit 등은 삭제·덮어쓰기 금지.
3. **운영 데이터 삭제 migration 금지**: DROP/DELETE 를 포함한 운영 migration 작성 금지. 교정은 새 forward migration 으로만.
4. **멱등성**: 모든 migration 은 `CREATE ... IF NOT EXISTS`, `ON CONFLICT DO NOTHING/UPDATE`, 멱등 ALTER 로 작성. 재실행해도 동일 결과.
5. **Anthropic API 미사용**(§17): 스키마/도구에 `anthropic` SDK 의존 없음. 지능은 Claude+메모리.
6. **웹 조회 전용·하드코딩 0**(§18): 수집·해석·저장은 백엔드, DB 가 운영 truth.

---

## 2. 데이터베이스·스키마·역할 구조

| 구성 | 값 | 설명 |
|---|---|---|
| database | `portfolio_os` | 운영 DB |
| schema | `portfolio` | 모든 운영 테이블이 들어가는 네임스페이스 |
| search_path | `portfolio, public` | 세션 기본 검색 경로 |
| public schema | **운영 테이블 금지** | `REVOKE CREATE ON SCHEMA public FROM PUBLIC;` |

### 역할(roles)

| role | 용도 | 권한 |
|---|---|---|
| `portfolio_admin` | DDL / schema owner / migration | schema `portfolio` 의 OWNER. CREATE/ALTER/DROP. migration 적용 주체. |
| `portfolio_app` | 애플리케이션 런타임 | **SELECT / INSERT / UPDATE only** (no DELETE → append-only 보호). |
| `portfolio_ro` | 조회·리포팅·대시보드 | SELECT only. |

> `portfolio_app` 에서 DELETE 를 제거함으로써 append-only 정책을 **DB 권한 레벨**에서 강제한다.
> 잘못된 행 정정은 새 행 추가(상태 컬럼 `superseded/cancelled/archived`)로만 처리.

### 최소권한(grant) 요약

```sql
-- 한 번만 (admin/superuser):
CREATE DATABASE portfolio_os;
CREATE SCHEMA portfolio AUTHORIZATION portfolio_admin;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER ROLE portfolio_admin SET search_path = portfolio, public;
ALTER ROLE portfolio_app   SET search_path = portfolio, public;
ALTER ROLE portfolio_ro    SET search_path = portfolio, public;

GRANT USAGE ON SCHEMA portfolio TO portfolio_app, portfolio_ro;
-- app: no DELETE
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA portfolio TO portfolio_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA portfolio TO portfolio_app;
-- ro: read only
GRANT SELECT ON ALL TABLES IN SCHEMA portfolio TO portfolio_ro;
-- 미래 테이블 기본권한 (admin 이 생성하는 테이블)
ALTER DEFAULT PRIVILEGES FOR ROLE portfolio_admin IN SCHEMA portfolio
  GRANT SELECT, INSERT, UPDATE ON TABLES TO portfolio_app;
ALTER DEFAULT PRIVILEGES FOR ROLE portfolio_admin IN SCHEMA portfolio
  GRANT SELECT ON TABLES TO portfolio_ro;
```

---

## 3. 연결 문자열 (DATABASE_URL)

```
postgresql://portfolio_app:***@localhost:5432/portfolio_os?options=-csearch_path%3Dportfolio
```

- `***` = `.env` 의 비밀번호. **로그·git·화면 출력 금지.**
- `options=-csearch_path%3Dportfolio` → 세션 search_path 를 connection 단계에서 고정 (`%3D` = `=`).
- 환경변수 분리:
  - `DATABASE_URL` — 런타임(app 역할, no DELETE).
  - `DATABASE_URL_ADMIN` — migration 전용(admin 역할). 평시 미사용.
  - `DATABASE_URL_RO` — 리포팅(ro 역할).
- 비밀번호·URL 전체를 `.env` 에만 둔다. 코드/DB/로그/메모리/audit payload 어디에도 평문 저장 금지.

---

## 4. JSONB vs 승격 컬럼 원칙

| 데이터 성격 | 저장 방식 | 예 |
|---|---|---|
| 자주 조회·필터·집계하는 **hot 값** | **컬럼** (인덱스 가능) | `cash_krw`, `total_value_krw`, `weight_pct`, `status`, `account_id`, `snapshot_date` |
| 진화하는 자유 문서 / 가변 구조 | **JSONB** | `policy`, `doc`(investor_profile), `prehook`, `outcome`, decision `payload` |

- 원칙: **hot 한 값은 컬럼으로 승격**(쿼리·제약·인덱스), **유연한 진화 문서는 JSONB**.
- JSONB 안의 값이 반복 조회되면 컬럼으로 승격(또는 생성 컬럼 `GENERATED ... STORED` + 인덱스).
- 하이브리드 유지: 현행 `investor_profile.doc`, `portfolio_policies.policy` 처럼 단단한 변수=컬럼 / 유연한 내용=JSONB.

---

## 5. Gap 분석 — SQLite → PostgreSQL 매핑

### 5.1 1:1 (그대로 이행 — 이름·구조 유지)

| SQLite | PostgreSQL (`portfolio.*`) | 비고 |
|---|---|---|
| `accounts` | `accounts` | 계좌 메타. PK `account_index`. |
| `account_snapshots` | `account_snapshots` | 잔고 스냅샷 truth. |
| `lessons` | `lessons` | 메모리 substrate. |
| `lesson_candidates` | `lesson_candidates` | 승격 전 관찰. |
| `evidence_documents` | `evidence_documents` | 근거 메타(본문 임베딩은 Vector 승격 시). |
| `consultations` | `consultations` | 상담 로그(append-only). |

### 5.2 분리 / 개명 (정규화)

| SQLite | PostgreSQL | 변경 내용 |
|---|---|---|
| `investor_profile` | `investor_profiles` (현재행) + `investor_profile_versions` (버전) | 현재 상태와 버전 이력 분리. |
| `investor_profile_history` | `strategy_change_events` 또는 `investor_profile_versions` | history 를 버전/변경 이벤트로 정규화. |
| `allocation_selections` | `selected_allocations` | 명칭 정리. append-only 유지(status). |
| `decisions` | `portfolio_decisions` | 명칭 정리. `payload` JSONB. |
| `holdings` | `position_snapshots` | 보유=포지션 스냅샷으로 개명. |

### 5.3 신규 (PostgreSQL 운영에서 추가)

| 그룹 | 신규 테이블 |
|---|---|
| 가격 | `price_snapshots` (quotes 승격), `*_daily_snapshots` |
| 주문 | `order_events` (orders 상태 전이 이벤트) |
| allocation | `allocation_options`(3안 후보), `allocation_history` |
| drift/dashboard | `portfolio_drift_history`, `dashboard_metrics` |
| 성장 메모리 | `agent_memories`, `task_memories`, `memory_retrieval_logs`, `prehook_runs`, `posthook_runs` |
| 리서치 | `research_runs` |
| graph | `*_edges` (계좌↔종목↔섹터↔국가↔통화↔테마 관계 인덱스) |
| 인증 | auth tables (→ [security_pin.md](security_pin.md): `user_security_settings`, `auth_sessions`, `auth_events`) |

---

## 6. 승격 순서 (우선순위 1~12)

핵심 truth → 의사결정 → 리스크 → 메모리 → 대시보드 순으로 점진 적용.

| # | 단계 | 대상 테이블(예) |
|---|---|---|
| 1 | account snapshot | `accounts`, `account_snapshots` |
| 2 | position snapshot | `position_snapshots`(=holdings), `price_snapshots` |
| 3 | profile / policy | `investor_profiles`, `investor_profile_versions`, `portfolio_policies` |
| 4 | target / selected allocation | `target_allocations`, `selected_allocations`, `allocation_options` |
| 5 | decision | `portfolio_decisions` |
| 6 | rebalance | `rebalance_plans`, `rebalance_plan_steps` |
| 7 | risk | `risk_checks`, risk 한도 테이블 |
| 8 | lessons | `lessons`, `lesson_candidates`, `agent_memories`, `task_memories` |
| 9 | evidence | `evidence_documents`, `decision_evidence_links` |
| 10 | consultations | `consultations`, `analysis_requests`, `advice_items`, `feedback_memory` |
| 11 | dashboard history | `portfolio_drift_history`, `allocation_history`, `dashboard_metrics` |
| 12 | auth | `user_security_settings`, `auth_sessions`, `auth_events` |

---

## 7. Rollback / Recovery 계획

- **멱등 migration**: 재실행해도 안전(IF NOT EXISTS / ON CONFLICT / 멱등 ALTER). 실패 시 같은 파일 재적용 가능.
- **운영 데이터 삭제 migration 금지**: DROP TABLE / DELETE / TRUNCATE 를 운영 migration 에 포함하지 않는다.
- **append-only 보호**: history·selections·consultations·tasks·feedback·audit 테이블의 행 삭제·덮어쓰기 금지(상태 컬럼으로만 무효화).
- **history 중복 방지**: 일자 스냅샷 history 는 `UNIQUE (account_id, snapshot_date)` 로 멱등 적재 보장(중복 행 방지).
- **백업**: schema 단위 백업 `pg_dump -n portfolio ...`. 복구는 해당 schema 만 대상.
- **잘못된 migration 교정**: 운영에서 DROP 금지. 새 **forward migration**(추가/정정 컬럼·새 행)으로만 교정한다.
- **SQLite → PG 이행**: 멱등 **upsert**(`INSERT ... ON CONFLICT (key) DO UPDATE`)로 옮긴다. 재실행해도 중복/손상 없음. 옮기는 동안 SQLite 는 그대로 두고 검증 후 truth 전환.

---

## 8. 적용 방법

```bash
# DDL 은 admin role 로만 적용 (운영 app role 로 DDL 금지)
psql "$DATABASE_URL_ADMIN" -f migrations/pg/100_core.sql

# 단계별 점진 적용 (우선순위 §6 순서)
psql "$DATABASE_URL_ADMIN" -f migrations/pg/200_allocation.sql
psql "$DATABASE_URL_ADMIN" -f migrations/pg/300_memory.sql
```

- migration 파일은 번호 순서로 forward-only. 한 파일 = 멱등.
- **connection test 는 성공/실패만 출력**한다(비밀번호·URL·행 내용 출력 금지). 예: `OK: connected to portfolio_os` / `FAIL: connection error`.
- admin 자격증명(`DATABASE_URL_ADMIN`)은 migration 시에만 로드, 런타임 app 에는 미주입.
