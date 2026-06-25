# DB / Architecture Agent 시스템 프로세스 정리

> 본 문서는 Portfolio OS 의 **데이터 계층 / 아키텍처 영역**을 실제 코드(`main_mission/portfolio_os/store/schema.sql`, `store/db.py`)와 설계문서(`docs/portfolio/portfolio_os_design_v2.md`, `db_schema.md`, `data_architecture.md`) 기반으로 정리한다.
> 추측 없이 코드에 실제 존재하는 테이블·컬럼·함수만 기술하며, 미구현은 명시적으로 "미구현/계획"으로 표기한다.
> 공통 원칙: 단기 trading 아님(포트폴리오 비중관리 + 분할 리밸런싱), 웹은 DB truth 조회만, KIS 호출은 백엔드 sync/job 만, 운영화면 mock/하드코딩 금지, 목표비중 없이 주문후보 금지, 사람 승인 없이 주문 금지, live 주문은 `KIS_LIVE_CONFIRM` 없이 하드차단, 모든 decision 은 snapshot/version/provenance 기록, RDB=금액/잔고/주문 truth · Vector=근거검색 · Graph=관계설명. 한글 문서 / 영문 코드.

---

## 1. 목적

이 영역은 **운영 truth 의 보관소(SSOT)와 그 구조 규칙**을 책임진다.

- **운영 truth = 로컬 SQLite `data/portfolio.sqlite3`** (`store/db.py:db_path()` → `SQLITE_PATH` env, 기본 `./data/portfolio.sqlite3`). 금액·잔고·주문·체결의 정합성 기준은 항상 이 RDB.
- 데이터의 **출처/버전/시점(provenance)** 을 컬럼화하여 모든 decision 이 재현·감사 가능하도록 한다 (`source`, `version`, `*_at`, `is_stale`, `verified_at`).
- **역할 분리(설계 원칙)**: RDB=truth(금액/잔고/주문), JSON document=스키마 미고정 진화 내용, Vector=의미검색(근거), Graph=관계설명. (`portfolio_os_design_v2.md §3`)
- **append-only / 삭제금지** 데이터를 정의하여 감사·되돌리기 근거를 영구 보존한다.
- **migration 멱등성**과 **SQLite→PostgreSQL 무손실 승격** 경로를 보장한다.

본 영역은 비즈니스 로직이 아니라 그 로직들이 공유하는 **데이터 계약(schema)** 과 **계층 경계(누가 쓰고 누가 읽는가)** 를 정의한다.

---

## 2. 전체 흐름

데이터 흐름은 단방향이며 쓰기 경로와 읽기 경로가 분리된다 (`data_architecture.md §1`).

```text
KIS OpenAPI / 외부 소스(DART·뉴스)
   │  (수집은 백엔드만)
   ▼
Backend Sync Job / 도메인 모듈 (Python)   broker/sync_job.py, profile.py, selection.py, decision.py …
   │  토큰·잔고·시세 수집 + 도메인 계산 → DB 쓰기 (주문은 order_service 경유)
   ▼
운영 truth DB (SQLite)                    data/portfolio.sqlite3  ← 본 영역의 핵심
   │  store/db.py: connect() → executescript(schema.sql) → _migrate()
   ▼
Web API (조회 전용, node:sqlite)          web/lib/server/portfolioDb.ts  (INSERT 없음 — SELECT only)
   │  KIS 직접 호출 금지
   ▼
Web UI (저장된 truth 렌더)
```

- DB 부팅: `store/db.py:connect()` 가 최초 1회(`_bootstrapped`) `schema.sql` 전체를 `executescript`(모두 `IF NOT EXISTS` — 멱등) 한 뒤 `_migrate()`(멱등 `ALTER`)를 실행한다.
- 웹의 유일한 쓰기 트리거는 `POST /api/accounts/[id]/sync` → 백엔드 sync job 실행(웹 자체는 INSERT 안 함). 확인됨: `web/lib/server/portfolioDb.ts` 에 `INSERT INTO` 없음.

---

## 3. 입력

본 영역(데이터 계층)이 받는 입력:

- **스키마 정의 파일**: `store/schema.sql` (CREATE TABLE/INDEX), `_ADD_COLUMNS`(`store/db.py:31`) 멱등 추가 컬럼 목록.
- **환경변수**: `SQLITE_PATH`(DB 경로), `.env`(`load_dotenv(override=False)` — `os.environ` 우선).
- **도메인 모듈의 쓰기 요청**: 각 도메인 모듈이 `connect()` 로 얻은 커넥션에 INSERT/UPDATE.
  - `profile.py:save()` → `investor_profile` UPDATE + `investor_profile_history` INSERT (append).
  - `selection.py` → `allocation_selections` INSERT(append) + 직전 active 행 `status='superseded'` UPDATE.
  - `decision.py` → `decisions`, `policy.py` → `portfolio_policies`, `allocation.py` → `target_allocations`, `lessons.py` → `lessons`/`lesson_candidates`, `advice.py` → `advice_items`, `broker/sync_job.py`/`account_status.py` → `accounts`/`account_snapshots`/`holdings`/`sync_events`, `broker/order_service.py` → `orders`, `audit/logger.py` → `audit_logs`.

---

## 4. 출력

- **운영 truth DB 파일** `data/portfolio.sqlite3` (모든 영구 상태).
- **읽기 인터페이스**: Python 도메인 모듈은 `store.db.connect()` 로 `sqlite3.Connection`(`row_factory=Row`, `PRAGMA foreign_keys=ON`) 을 받는다. 웹은 `node:sqlite` 로 동일 파일을 SELECT.
- **무결성 보장 산출물**: `UNIQUE`/`CHECK`/`FOREIGN KEY` 제약, 인덱스(조회 성능·freshness 정렬).
  - 예: `orders.client_order_id UNIQUE`(idempotency), `orders.status CHECK(...)` 상태머신, `holdings.snapshot_id REFERENCES account_snapshots(id) ON DELETE CASCADE`, `universe_instruments UNIQUE(account_index,ticker,market)`.

---

## 5. DB 테이블

`store/schema.sql` 에 실제 정의된 테이블 전체 (그룹별):

### 계좌·잔고·시세 (운영 truth, RDB)
| 테이블 | 역할 | 핵심 컬럼 |
|---|---|---|
| `accounts` | 계좌 메타(`.env` 미러). **자격증명 미저장** | `account_index`(PK), `mode`, `account_no_masked`, `has_credentials`, `token_status`, `sync_status`, `last_synced_at` |
| `account_snapshots` | 잔고 스냅샷(계좌×시점) = 금액 truth | `cash_krw`, `total_value_krw`, `source`, `is_stale`, `captured_at` |
| `holdings` | 스냅샷 행 단위 보유종목 | `snapshot_id`(FK CASCADE), `ticker`, `qty`, `avg_price`, `market_value` |
| `quotes` | 현재가 스냅샷 | `ticker`, `price`, `source`, `captured_at` |
| `sync_events` | 동기화 이력(freshness 근거) | `kind`, `status`, `stage`, `error`, `finished_at` |

### 주문·감사 (안전 백본)
| 테이블 | 역할 | 핵심 컬럼 |
|---|---|---|
| `orders` | 주문 원장(idempotency + 상태머신) | `client_order_id UNIQUE`, `payload_hash`, `status CHECK(created…aborted)`, `mode`, `limit_price` |
| `audit_logs` | 모든 주문/승인/거절/차단 감사(비밀값 미저장) | `actor`, `action`, `entity_type/id`, `level CHECK(CRITICAL/WARNING/INFO)`, `payload`, `created_at` |

### 의사결정 위계 (대/중/소 전제)
| 테이블 | 역할 |
|---|---|
| `investor_profile` | 대전제(운용방식)+중전제(관심/생각). 계좌별 1행. 하드변수=컬럼 + 진화내용=`doc`(JSON) |
| `universe_instruments` | 소전제 골격: 계좌별 관심종목 + `target_weight_pct` (KIS 검증 종목만, mock 하드코딩 대체) |
| `decisions` | 의사결정 스냅샷(현재비중 vs 목표 → drift → 후보 → 리스크) `payload`(JSON) |
| `portfolio_policies` | profile→policy 컴파일 객체, `version` 관리 |
| `target_allocations` | anchor+tilt 3안(conservative/base/aggressive) 제안, `status`(draft/chosen/archived) |
| `rebalance_plans` / `rebalance_plan_steps` | 회차 단위 분할 리밸런싱 계획·스텝(total vs cycle, round_no, `limit_price`) |

### 성장·근거 (메모리 substrate, v2 승격용)
| 테이블 | 역할 |
|---|---|
| `lessons` | 누적·재사용 교훈(scope=market/economy/sector/instrument/premise/decision, `confidence`) |
| `lesson_candidates` | 승격 전 관찰(`status`=candidate/promoted/rejected) |
| `evidence_documents` | 근거 문서 메타(본문 임베딩은 Vector 승격 시) |
| `decision_evidence_links` | decision↔evidence 링크(어떤 근거→어떤 비중변경) |
| `advice_items` | 대전제 정리 시 도출 조언 + 사람 반영/보류 결정 |

### append-only 이력 (삭제금지)
| 테이블 | 역할 |
|---|---|
| `investor_profile_history` | 대/중전제 변경 이력. 매 저장 시 1행 append(`snapshot` JSON, `source`) |
| `allocation_selections` | 사람이 확정한 공식 목표비중. 재선택/취소 시 삭제 금지 → `status`(active/superseded/cancelled) |
| `audit_logs` | (위 참조) append-only |
| `lessons` | (위 참조) append-only 성장 substrate |

> 역할 분리: 위 테이블 전부 **RDB(truth)**. JSON document 는 RDB 컬럼 안의 `doc`/`payload`/`snapshot`/`policy` 필드로 진화 내용을 담는다(별도 doc store 는 미구현). **Vector DB / Graph Index 는 설계만 존재(로컬 미구현)** — `data_architecture.md §4~5`.

---

## 6. API / 함수

본 영역의 실제 공개 함수(`store/db.py`):

| 함수 | 시그니처 | 역할 |
|---|---|---|
| `db_path()` | `-> Path` | `.env` 로드(override=False) 후 `SQLITE_PATH` 해석, 상대경로는 ROOT 기준 절대화 |
| `connect()` | `-> sqlite3.Connection` | 디렉터리 생성, 연결, `row_factory=Row`, `PRAGMA foreign_keys=ON`, 최초 1회 스키마 부트스트랩+`_migrate` |
| `_migrate(conn)` | `-> None` | `_ADD_COLUMNS` 각 항목에 `ALTER TABLE ADD COLUMN` 시도, `OperationalError`(중복컬럼) 무시 → 멱등 |
| `init(conn=None)` | `-> None` | 스키마 재적용(executescript). 외부에서 명시 초기화용 |

- 멱등 마이그레이션 대상(`_ADD_COLUMNS`): `investor_profile` 에 `individual_cap_pct`(REAL), `individual_count`(INTEGER), `region_pref`(TEXT), `rebalance_pace`(TEXT), `doc`(TEXT). → `CREATE IF NOT EXISTS` 로는 안 붙는, 나중에 추가된 컬럼을 멱등 보강.
- 웹 측 읽기 함수는 `web/lib/server/portfolioDb.ts`(node:sqlite, SELECT 전용) — 쓰기 없음.

> 참고: PostgreSQL 초안 `migrations/001_init.sql`·`002_hardening.sql`·`003_backtest.sql` 가 존재하나 **SQLite 운영 경로는 `schema.sql` + `db.py:_migrate` 만 사용**. 둘은 아직 동기화되지 않은 별도 산출물.

---

## 7. UI 화면

**해당 없음 (데이터 계층).** 본 영역은 UI 를 직접 갖지 않는다. 단, UI 가 지켜야 할 데이터 계약을 제공한다.

- 웹은 `node:sqlite` 로 동일 SQLite 파일을 **조회만** 하고, 진행도·잔고·비중은 전부 DB 파생값으로 계산해야 한다(하드코딩 금지, `data_architecture.md §6`).
- 미충족 항목: `/portfolio` 페이지가 아직 `web/lib/portfolio/mock.ts` 를 참조(아래 §14).

---

## 8. 상태 전이

데이터 계층이 스키마(CHECK)로 강제하는 상태머신:

- **`orders.status`**: `created → submitting → submitted → (in_doubt | partial | filled | rejected | canceled | aborted)`. `CHECK` 제약으로 허용값 고정, `client_order_id UNIQUE` 로 재전송 차단. `in_doubt` 는 응답 불확실(§9) 상태.
- **`allocation_selections.status`**: `active → superseded`(재선택 시) / `active → cancelled`(취소 시). 삭제 없이 상태만 전이(`selection.py` UPDATE).
- **`target_allocations.status`**: `draft → chosen | archived`.
- **`lesson_candidates.status`**: `candidate → promoted | rejected`.
- **`advice_items.status`**: `open → accepted | rejected`.
- **스키마 자체의 부트스트랩 전이**: (없음 → 생성됨) `connect()` 최초 호출 → (컬럼 결손 → 보강됨) `_migrate()`. 멱등이라 반복 호출해도 동일 종착.

---

## 9. 예외 / 실패 케이스

- **중복 컬럼 ALTER**: `_migrate()` 가 `sqlite3.OperationalError` 를 try/except 로 흡수 → 재실행 안전.
- **idempotency 충돌**: 동일 `client_order_id` 재INSERT 시 `UNIQUE` 위반 → 도메인 계층(`order_service`)이 재전송 금지로 처리(절대규칙 §10).
- **FK 위반**: `holdings.snapshot_id` 가 없는 스냅샷 참조 시 `PRAGMA foreign_keys=ON` 으로 차단. 스냅샷 삭제 시 `ON DELETE CASCADE` 로 holdings 동반 삭제.
- **stale 데이터**: `account_snapshots.is_stale`, `sync_events` 의 freshness 로 신선도 판단(데이터 자체는 보존, 상태 플래그로 표현).
- **append-only 보호**: 프로필/선택/감사/lesson 은 UPDATE 가 아닌 INSERT 가 원칙이라, 실수 덮어쓰기로 이력 손실이 발생하지 않도록 테이블이 분리됨.
- **자격증명 누출 위험 차단**: 어떤 테이블에도 키/시크릿/토큰/평문 계좌번호를 두지 않음(마스킹값만) — `accounts.account_no_masked`.

미구현 위험: SQLite 레벨에는 `orders.mode` 와 `accounts.mode` 불일치를 막는 트리거가 없다(`db_schema.md` 가 언급한 PG 트리거는 SQLite 미적용 → 애플리케이션 검증 의존).

---

## 10. Hard-block 조건

데이터 계층이 직접 강제하는 hard-block:

- `orders.client_order_id UNIQUE` → 중복 주문 INSERT 차단(idempotency).
- `orders.status` / `audit_logs.level` / 각 status 컬럼의 `CHECK` → 정의되지 않은 상태값 저장 차단.
- `PRAGMA foreign_keys=ON` → 고아 참조 차단.
- **자격증명 저장 금지** → 스키마에 평문 키/계좌번호 컬럼 자체가 없음(설계적 hard-block).

스키마 외부(상위 계층)의 hard-block — 본 영역은 그 truth 를 제공:
- live 주문은 `KIS_LIVE_CONFIRM` 없이 차단(`broker/factory.py`/`kis_adapter`), 목표비중(`universe_instruments`/`target_allocations`) 없이 주문후보 금지, 사람 승인(`approvals`/선택 확정) 없이 주문 금지 — 모두 RDB 상태를 게이트 입력으로 사용.

---

## 11. 로그 / 감사 기록

- **`audit_logs`**: 모든 주문/승인/거절/차단의 who(`actor`)/what(`action`)/when(`created_at`)/level/payload. 비밀값 미저장. 인덱스 `idx_audit_created`/`idx_audit_entity`/`idx_audit_action`. append-only. 작성자: `audit/logger.py`(+ `audit/secrets_detector.py` 로 비밀값 검출).
- **`sync_events`**: 동기화 단계별 성공/오류(`stage`, `error`) → freshness·장애 추적.
- **`investor_profile_history`**: 전제 변경 전수 이력(되돌리기/감사). `profile.py:226` INSERT.
- **`allocation_selections`**: 확정 비중 결정 이력 + `precheck_status`/`precheck_reasons`/`diff`/`user_override`(provenance 완비).
- 모든 핵심 테이블이 `created_at`/`updated_at`/`captured_at`/`selected_at` 등 시점 컬럼과 `source`/`refined_by`/`selected_by`(provenance) 를 보유 → "어떤 근거·누가·언제" 재현 가능.

---

## 12. 테스트 기준

- 본 영역 직접 테스트 파일은 **없음**(전용 `test_db.py` 미존재). 데이터 계층은 도메인 테스트가 간접 검증:
  - `tests/test_order_safety.py`: 주문 원장 idempotency·`in_doubt`·주문 전 검증(orders/audit_logs 사용). 메모리상 "16테스트 통과" 기록.
  - `tests/test_risk_gate.py`: 리스크 게이트(가공 입력).
- 권장(미구현) 데이터 계층 테스트 기준:
  1. `connect()` 2회 호출 멱등(스키마/마이그레이션 재실행 무오류).
  2. `_migrate()` 가 신규 DB·구버전 DB(컬럼 결손) 양쪽에서 동일 결과.
  3. `orders.client_order_id` 중복 INSERT → IntegrityError.
  4. append-only 테이블에 UPDATE 대신 INSERT 가 일어나 이력 보존(profile.save 후 history +1).
  5. FK CASCADE 동작(snapshot 삭제 시 holdings 동반 삭제).

---

## 13. 현재 구현 상태

**구현됨 (코드 확인):**
- ✅ SQLite 운영 truth DB + 부트스트랩/멱등 마이그레이션 (`store/db.py`, `schema.sql`).
- ✅ 전체 테이블 정의: accounts, account_snapshots, holdings, quotes, sync_events, audit_logs, orders, universe_instruments, decisions, investor_profile, lessons, portfolio_policies, target_allocations, rebalance_plans/steps, lesson_candidates, evidence_documents, decision_evidence_links, allocation_selections, advice_items, investor_profile_history.
- ✅ append-only 이력 테이블 3종 운용: `investor_profile_history`(`profile.py`), `allocation_selections`(`selection.py`), `audit_logs`(`audit/logger.py`).
- ✅ idempotency/상태머신 제약(`orders` UNIQUE/CHECK), FK CASCADE(`holdings`).
- ✅ 웹 조회 전용 분리 검증: `web/lib/server/portfolioDb.ts` 에 INSERT 없음(SELECT only).
- ✅ 자격증명 미저장(마스킹 컬럼만).

**부분 구현:**
- 〜 v2 승격 테이블(portfolio_policies/target_allocations/rebalance_plans 등)은 **스키마는 생성되나** 일부 도메인 모듈(policy.py/allocation.py/selection.py)이 채우기 시작한 단계.
- 〜 PG 마이그레이션 초안(`migrations/00*.sql`)과 SQLite `schema.sql` 이 **이중 관리**(동기화 안 됨).

---

## 14. 미구현 / placeholder

- ❌ **Vector DB**: 설계만 존재. 로컬 SQLite 에 pgvector 없음 → PostgreSQL 승격 시 구현. `evidence_documents.body` 임베딩 미적재 (`data_architecture.md §4`).
- ❌ **Graph Index**: 설계만 존재. 관계(`*_links` recursive CTE/그래프 엔진) 로컬 미구현 (`§5`). 현재 `decision_evidence_links` 만 RDB 링크로 존재.
- ❌ **별도 JSON document store**: 진화 내용은 RDB 컬럼(`doc`/`payload`/`snapshot`) 안에만 있음. 독립 doc store 미구현.
- ❌ **SQLite 레벨 무결성 트리거**: orders.mode↔accounts.mode 불일치 차단 트리거 미적용(애플리케이션 검증 의존).
- ❌ **데이터 계층 전용 테스트** 미작성.
- ❌ **`/portfolio` 화면 DB 전환**: 아직 `web/lib/portfolio/mock.ts`(CURRENT_PORTFOLIO/TOTAL_VALUE_KRW) 사용 → DB 스냅샷 기반 전환 예정 (운영화면 mock 금지 원칙 위반 잔존, `data_architecture.md §6`).
- ❌ **PG 승격 자체** 미실행(로컬 SQLite 단계). `migrations/*.sql` 은 미적용 초안.
- ❌ `quotes`·체결(`fills` 등) 적재 파이프라인 일부 미완(설계 ⏳).

---

## 15. 다음 개선 항목

1. `schema.sql`(SQLite) ↔ `migrations/*.sql`(PG) **단일 SSOT 화** 또는 자동 생성으로 이중관리 제거.
2. 멱등 마이그레이션을 `_ADD_COLUMNS` 하드코딩에서 **버전 테이블 기반 migration runner** 로 승격(PG 이관 대비).
3. 데이터 계층 전용 테스트(§12 기준) 추가 — 멱등성·FK·idempotency·append 보존.
4. `orders.mode`↔`accounts.mode` 정합 강제(트리거 또는 도메인 단일 게이트).
5. `/portfolio` mock 제거 → DB 스냅샷 기반 계산으로 전환.
6. PostgreSQL 승격 시 Vector(pgvector)·Graph(recursive CTE/`*_links`) 실구현, evidence 임베딩 적재.
7. 모든 RDB 키를 정수 PK + scope/ref 패턴으로 유지(Graph 이식 용이) — 신규 테이블 추가 시 점검 규칙화 (`design_v2 §3`).

---

## 16. 다른 Agent와의 의존성

본 영역은 **모든 Agent 의 공유 기반**이다.

- **Sync / Broker Agent** (`broker/sync_job.py`, `account_status.py`, `order_service.py`): accounts/account_snapshots/holdings/quotes/sync_events/orders 쓰기 → 본 영역 스키마·`connect()` 의존. KIS 호출은 이 계층만(웹 금지).
- **Profile / 대중소전제 Agent** (`profile.py`, `policy.py`, `allocation.py`, `selection.py`, `advice.py`): investor_profile(+history), portfolio_policies, target_allocations, allocation_selections, advice_items.
- **Decision / Rebalance Agent** (`decision.py`): decisions, rebalance_plans/steps. 목표비중(universe/target_allocations) 없으면 주문후보 생성 금지 — 본 영역이 그 truth 제공.
- **Risk Gate** (`risk/gate.py`): orders/holdings/account_snapshots 를 읽어 hard-block 판정.
- **Audit / Memory Agent** (`audit/logger.py`, `lessons.py`): audit_logs, lessons, lesson_candidates, evidence_documents, decision_evidence_links. "메모리로 성장" substrate.
- **Web (조회 전용)** (`web/lib/server/portfolioDb.ts`): 동일 SQLite 파일을 node:sqlite 로 SELECT. 쓰기는 sync 트리거 → 백엔드 경유만.

데이터 계약(schema)·운영 truth 기준·append-only/삭제금지 규칙이 흔들리면 위 모든 Agent 의 정합성과 감사 가능성이 동시에 깨진다 → 본 영역이 시스템 안정성의 토대.
