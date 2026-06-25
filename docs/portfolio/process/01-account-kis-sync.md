# Account / KIS Sync Agent 시스템 프로세스 정리

> 영역 범위(CEO 지정): KIS 계좌연결 · 토큰 발급/갱신 · account snapshot 생성 · 현금/예수금/보유종목 동기화 · price snapshot 연결 · stale 기준 · 동기화 실패처리 · 웹은 DB 조회만.
> 공통 원칙: 단기 trading 아님(비중관리+분할 리밸런싱). 웹=DB truth 조회만. KIS 호출은 백엔드 sync/job 만. mock/하드코딩 금지. live 주문은 `KIS_LIVE_CONFIRM` 없이 하드차단. 모든 결정은 snapshot/provenance 기록. 한글 문서/영문 코드.

---

## 1. 목적

CEO 가 한국투자증권(KIS) 계좌를 AI 포트폴리오 관리자에게 위임할 때, **계좌를 안전하게 연결하고 잔고(현금/예수금/보유종목)를 운영 truth(SQLite)에 미러링**하는 영역이다.

- KIS Open API 와 직접 통신하는 **유일한 백엔드 경계**다. 웹/UI 는 절대 KIS 를 직접 호출하지 않고, 이 영역이 SQLite 에 적재한 결과만 조회한다 (`web/lib/server/portfolioDb.ts`).
- 자격증명/토큰은 DB·로그·메모리에 저장하지 않고 `.env` 에만 둔다 (`accounts` 테이블은 마스킹된 메타만 보관).
- 읽기 전용 수집이다. 이 영역의 어떤 코드도 주문(`place_order`)을 호출하지 않는다 (`sync_job.py` 도크스트링: "주문 없음(읽기 전용 수집)").
- 후속 영역(목표비중·리밸런싱·의사결정)이 신뢰할 수 있는 **최신 account snapshot** 을 제공하는 것이 본 영역의 산출물이다.

---

## 2. 전체 흐름

```text
[웹] 계좌 추가 폼 (alias/mode/appKey/appSecret/accountNo/productCode)
   └ POST /api/accounts (route.ts)
        ├ envStore.addAccount() → .env 에 KIS_ACCOUNT_{n}_* 기록 + primary 미러
        └ execFile python -m ...broker.sync_job --account {n}   (백엔드 job 트리거)
                                  │
[백엔드 sync_job.py] sync_balance(n, conn)
   ├ upsert_account_meta(n)         → accounts (alias/mode/account_no_masked/has_credentials)
   ├ account_status.fetch(n)
   │     ├ KisHttpClient(mode, account_index=n).require_credentials()
   │     ├ client.ensure_token()    → 토큰 발급/캐시(파일) → stage="token"
   │     ├ adapter.get_balance(acct) → KIS 잔고조회 (output1)
   │     └ adapter.get_cash_krw(acct)→ KIS 예수금 (output2.dnca_tot_amt)
   ├ 성공 → INSERT account_snapshots(+holdings 행들), accounts.sync_status='ok', last_synced_at
   ├ 실패 → accounts.sync_status='error', last_error, token_status
   └ 항상 → INSERT sync_events(kind='balance', status, stage)
                                  │
[웹] GET /api/accounts/[id] → getAccountView(index)  (DB 조회만)
   └ accounts + 최신 account_snapshots + holdings + steps/progress/isFresh
        └ /accounts/[id]/page.tsx + AccountSync.tsx 렌더
              └ isFresh=false 면 1회 자동 POST /api/accounts/[id]/sync (job 재트리거)
```

핵심: **웹→백엔드 job(트리거)→DB 적재→웹 DB 조회**. 웹은 KIS 토큰/잔고를 직접 만지지 않는다.

---

## 3. 입력

| 입력 | 출처 | 코드 근거 |
|---|---|---|
| 계좌 생성 폼 (alias, mode, appKey, appSecret, accountNo, productCode) | 웹 POST body | `web/app/api/accounts/route.ts` POST |
| `.env` 자격증명 `KIS_ACCOUNT_{n}_{APP_KEY,APP_SECRET,ACCOUNT_NO,PRODUCT_CODE,MODE,ALIAS}` | `.env` (envStore 기록) | `kis_client.py` `__init__`, `envStore.addAccount` |
| 전역 `KIS_MODE`(기본 mock/paper), `KIS_LIVE_CONFIRM` | `.env` | `factory.py`, `kis_client.py`, `account_status.fetch` |
| `account_index` (1~50) | job 인자 `--account`, web `params.id` | `sync_job.main`, `factory.get_broker` |
| `SQLITE_PATH`(기본 `./data/portfolio.sqlite3`), `SYNC_FRESHNESS_SEC`(기본 900) | `.env`/환경변수 | `store/db.py db_path`, `portfolioDb.ts FRESHNESS_SEC` |
| KIS 응답: 잔고 `output1`(pdno/hldg_qty/pchs_avg_pric/evlu_amt), 예수금 `output2[0].dnca_tot_amt`, 토큰 `access_token/expires_in` | KIS Open API | `kis_adapter.get_balance/get_cash_krw`, `kis_client.ensure_token` |

---

## 4. 출력

| 출력 | 형태 | 코드 근거 |
|---|---|---|
| 계좌 메타 (마스킹) | `accounts` 1행/계좌 | `sync_job.upsert_account_meta` |
| 잔고 스냅샷 | `account_snapshots` 1행/동기화 (cash_krw, total_value_krw, holdings_count, source=`kis_{mode}`, is_stale=0) | `sync_job.sync_balance` |
| 보유종목 | `holdings` n행/스냅샷 (ticker, qty, avg_price, market_value, currency) | `sync_job.sync_balance` 루프 |
| 동기화 이력 | `sync_events` 1행/시도 (kind='balance', status, stage, error) | `sync_job.sync_balance` |
| job stdout JSON | `{account_index, ok, snapshot_id, cashKrw, holdings}` 또는 `{ok:false, stage, error}` | `sync_job.sync_balance` return, `account_status.fetch` |
| 웹 조회용 뷰 | `AccountView` (snapshot/holdings/steps/progress/isFresh) | `portfolioDb.getAccountView` |

---

## 5. DB 테이블

`main_mission/portfolio_os/store/schema.sql` (SQLite, `data/portfolio.sqlite3`). 본 영역이 **쓰는** 테이블:

| 테이블 | 역할 | 본 영역에서 쓰는 주요 컬럼 |
|---|---|---|
| `accounts` (PK account_index) | 계좌 메타 미러 | alias, mode, **account_no_masked**(앞2+마스킹), has_credentials, token_status(ok/error/unknown), sync_status(ok/error/never), last_error, last_synced_at, updated_at |
| `account_snapshots` (PK id) | 잔고 스냅샷=금액 truth | account_index, cash_krw, total_value_krw, holdings_count, source(kis_paper/kis_live), is_stale, captured_at. idx: (account_index, captured_at DESC) |
| `holdings` (PK id) | 스냅샷 행 단위 보유종목 | snapshot_id(FK→account_snapshots ON DELETE CASCADE), account_index, ticker, name, qty, avg_price, market_value, currency, captured_at |
| `sync_events` (PK id) | 동기화 작업 이력=freshness/감사 근거 | account_index, kind(balance), status(ok/error), stage(credentials/token/balance), error, started_at, finished_at |
| `quotes` (PK id) | 현재가 스냅샷 (ticker, market, price, source, captured_at). idx (ticker, captured_at DESC) | **본 영역이 아직 INSERT 안 함**(§14). 가격 동기화 미구현 |

- **자격증명 평문은 어떤 테이블에도 없음** — schema.sql 1~3행 주석 + `accounts` 는 `account_no_masked` 만.
- 본 영역이 **읽기만/안 건드리는** 테이블: `decisions`, `target_allocations`, `allocation_selections`, `orders`, `audit_logs`, `universe_instruments`, `investor_profile*` 등 (후속 Agent 소관).

---

## 6. API / 함수

### 백엔드 (Python, KIS 경계)
| 심볼 | 파일 | 책임 |
|---|---|---|
| `sync_job.sync_balance(n, conn)` | `broker/sync_job.py` | 메타 업서트 + fetch + 스냅샷/holdings/sync_events 적재 (오케스트레이터) |
| `sync_job.upsert_account_meta(conn, n)` | `broker/sync_job.py` | `.env` → `accounts` 미러 (INSERT … ON CONFLICT) |
| `sync_job.discover_indices()` | `broker/sync_job.py` | `KIS_ACCOUNT_{1..50}_APP_KEY` 있는 인덱스 탐색 (`--all`) |
| `sync_job.main()` | `broker/sync_job.py` | CLI: `--account N` / `--all` / (없으면 메타만) |
| `account_status.fetch(account_index)` | `broker/account_status.py` | read-only: 토큰→잔고→예수금→`{ok, cashKrw, holdings, totalValueKrw, mode}` (실패 시 `stage`) |
| `factory.get_broker(mode, account_index)` | `broker/factory.py` | mode→adapter 디스패치, live 는 `_require_live_confirm` |
| `KisHttpClient.ensure_token()` | `broker/kis_client.py` | 토큰 발급/파일캐시/만료5분전 선제갱신, 실패 시 `is_healthy=False` |
| `KisHttpClient.require_credentials()` | `broker/kis_client.py` | 누락 .env 값 검사 → `KisConfigError` |
| `_KisAdapterBase.get_balance/get_cash_krw` | `broker/kis_adapter.py` | KIS 잔고/예수금 조회 (`get_quote` 도 있으나 본 흐름 미사용) |

### 웹 API (DB 조회 + job 트리거만)
| 라우트 | 메서드 | 동작 | 파일 |
|---|---|---|---|
| `/api/accounts` | GET | `getAccounts()` DB 조회 | `web/app/api/accounts/route.ts` |
| `/api/accounts` | POST | envStore 기록 + sync_job 트리거 (실패 무시, 계좌는 생성) | 동일 |
| `/api/accounts/[id]` | GET | `getAccountView(index)` DB 조회만 (KIS 호출 없음 주석 명시) | `web/app/api/accounts/[id]/route.ts` |
| `/api/accounts/[id]/sync` | POST | `execFile python sync_job --account id` 트리거, stdout JSON 파싱 반환 (표시 데이터 아님) | `web/app/api/accounts/[id]/sync/route.ts` |

### 웹 서버 조회 함수 (`web/lib/server/portfolioDb.ts`, node:sqlite readOnly)
- `getAccounts()`, `getAccountView(index)`, 상수 `FRESHNESS_SEC = process.env.SYNC_FRESHNESS_SEC ?? 900`, 내부 `freshnessSeconds(iso)` → `isFresh = age ≤ FRESHNESS_SEC`.
- DB 미존재/쿼리 실패 시 빈 배열 반환(throw 안 함) — 운영화면 graceful.

---

## 7. UI 화면

| 화면/컴포넌트 | 파일 | 표시 내용 (전부 DB 출처) |
|---|---|---|
| 계좌 상세 페이지 | `web/app/accounts/[id]/page.tsx` | alias, ModeBadge(mock/모의투자/실전), `account_no_masked`, sync_status 배지(동기화됨/오류/미동기화) |
| 연결 준비 카드 | `web/components/AccountSync.tsx` | 4단계 steps(자격증명/토큰/잔고/준비완료), progress %, 오류 시 `last_error`, "마지막 동기화 …·출처 DB 스냅샷" |
| 잔고 카드 | `AccountSync.tsx` | 예수금(cash_krw), 총평가액(total_value_krw), holdings 테이블(종목/수량/평단/평가액) |
| 동기화 버튼/자동 동기화 | `AccountSync.tsx` | 수동 `runSync()` POST sync; `isFresh=false` 시 `autoTried`로 1회 자동 트리거 후 DB 재조회 |

- **mock/하드코딩 금지 준수**: progress/steps 는 DB 상태(`has_credentials`/`token_status`/snapshot/`sync_status`/isFresh)에서 계산 (`getAccountView` 주석 "하드코딩 아님"). 잔고 없을 때 빈 상태 문구만 표시.
- "다가오는 일정·이벤트" 카드는 **정직한 빈 상태**(DART/뉴스 미연동 명시) — 본 영역 아님.

---

## 8. 상태 전이

### 계좌 sync_status (`accounts.sync_status`)
```
never ──(첫 sync_balance 호출)──▶ ok / error
ok    ──(다음 sync 실패)────────▶ error
error ──(다음 sync 성공)────────▶ ok
```
- 최초 `upsert_account_meta` 는 기존값 보존: `COALESCE(... , 'never')`.
- 성공: `sync_status='ok', token_status='ok', last_error=NULL, last_synced_at=now`.
- 실패: `sync_status='error', token_status=(stage=='token'?'error':tokenOk), last_error=err`.

### 토큰 (KisHttpClient)
```
없음 ─ensure_token()─▶ 파일캐시 유효? ─▶ 재사용
                    └ 발급 성공 ─▶ 메모리+파일 캐시(expires_at), is_healthy=True
                    └ 발급/네트워크 실패 ─▶ is_healthy=False (A3 ABORT)
만료 5분 전 ─▶ 선제 재발급
```

### freshness (웹)
```
isFresh = last_synced_at 존재 AND (now - last_synced_at) ≤ FRESHNESS_SEC(900s)
isFresh=false → AccountSync 가 1회 자동 동기화
```

---

## 9. 예외 / 실패 케이스

| 케이스 | 처리 | 코드 근거 |
|---|---|---|
| 자격증명 누락 | `require_credentials()` → `KisConfigError` → fetch `{ok:false, stage:"credentials"}` → sync_status='error' | `kis_client.require_credentials`, `account_status.fetch` |
| 토큰 발급 실패/네트워크 오류 | `is_healthy=False`, `RuntimeError`, fetch `{stage:"token"}`, token_status='error' | `kis_client.ensure_token/_raw_post` |
| 잔고 조회 실패(rt_cd≠0/예외) | fetch `{stage:"balance", tokenOk:true}`, 스냅샷 미적재 | `kis_adapter.get_balance` raise, `account_status.fetch` |
| 잘못된 mode(mock 등) | fetch `{ok:false, error:"paper|live 로 연결하세요"}` | `account_status.fetch` |
| python 미설치(ENOENT) | web 라우트가 python/python3/py 순차 시도, 전부 실패 시 500 | `[id]/sync/route.ts`, `accounts/route.ts` |
| 계좌 생성 후 job 실패 | 계좌는 .env/DB 에 생성, 동기화는 이후 수동 (POST 무시) | `accounts/route.ts` POST 주석 |
| DB 파일 없음/쿼리 오류(웹) | `query()` 빈 배열 반환, 화면은 빈 상태 | `portfolioDb.open/query` |
| job stdout 비 JSON | sync 라우트 try/catch 후 `{}` | `[id]/sync/route.ts` |

모든 시도는 성공/실패 무관 `sync_events` 에 1행 적재(stage 포함) → 사후 진단 가능.

---

## 10. Hard-block 조건

| 조건 | 차단 동작 | 코드 근거 |
|---|---|---|
| `KIS_MODE=live` 인데 `KIS_LIVE_CONFIRM≠I_UNDERSTAND` | `get_broker(live)` → `RuntimeError` (broker 생성 차단) | `factory._require_live_confirm` |
| 토큰/네트워크 장애 | `is_healthy=False` → 주문 루프 ABORT (A3). 본 영역은 읽기지만 동일 클라이언트 공유 | `kis_client` `_healthy=False`, `port.BrokerPort.is_healthy` |
| 자격증명 누락 | `require_credentials()` → 잔고조회/주문 진입 자체 차단 | `kis_client.require_credentials` |

- 본 영역은 **읽기 전용**이라 주문 hard-block(시장가 매수 금지, 목표비중 없는 후보, 승인 없는 주문)을 직접 트리거하지 않는다 — 그 게이트는 risk/decision/order Agent 소관. live 어댑터 생성 차단만 본 영역에 직접 해당.

---

## 11. 로그 / 감사 기록

| 기록 | 위치 | 비밀값 처리 |
|---|---|---|
| 동기화 시도 이력 | `sync_events` (kind/status/stage/error/started_at/finished_at) | 평문 키/토큰 미포함 |
| 계좌 상태/오류 | `accounts.last_error`, `token_status`, `last_synced_at` | account_no 는 마스킹만 |
| 스냅샷 provenance | `account_snapshots.source` = `kis_paper`/`kis_live`, `captured_at` | — |
| 로그 마스킹 유틸 | `kis_client.mask()` (앞 keep 자리만), `credential_summary()` | app_key/secret/token/계좌번호 원문 금지 |

- **`audit_logs` 테이블은 존재하나 본 sync 흐름은 적재하지 않음** (주문/승인/거절 전용). 동기화 감사 추적은 `sync_events` 가 담당.

---

## 12. 테스트 기준

- **현재 본 영역 전용 자동 테스트는 없음.** `main_mission/portfolio_os/tests/` 에는 `test_risk_gate.py`, `test_order_safety.py` 만 존재(주문 안전 16테스트) — sync/account/token 테스트 부재 (§14).
- 수동 검증 경로(문서/CLI):
  - `python -m main_mission.portfolio_os.broker.kis_check` — 연결 점검 (CLAUDE.md §4).
  - `python -m main_mission.portfolio_os.broker.account_status --account 1` — 잔고 JSON 1줄.
  - `python -m main_mission.portfolio_os.broker.sync_job --account 1` / `--all`.
- 권장 단언(미작성): 자격증명 누락 시 stage='credentials', mock mode 거부, 성공 시 snapshot+holdings 행 수 일치, 실패해도 sync_events 1행, isFresh 경계(900s).

---

## 13. 현재 구현 상태

**구현 완료 (코드 존재·동작):**
- 멀티계좌(1~50) `.env` 기반 연결, 웹 폼→`.env`→DB 미러 (`envStore.addAccount`, `upsert_account_meta`).
- KIS 토큰 발급/파일캐시/만료5분전 선제갱신/rate limit(토큰버킷) (`kis_client.ensure_token/_throttle`).
- 국내주식 잔고(`output1`)·예수금(`output2.dnca_tot_amt`) 조회 → `account_snapshots`+`holdings` 적재 (`account_status.fetch`, `sync_job.sync_balance`).
- `total_value_krw = cash + Σ market_value` 계산, source 기록.
- 동기화 이력 `sync_events`(stage 별 실패 포함), 계좌 상태머신(never/ok/error).
- 웹 DB 조회 전용(node:sqlite readOnly), steps/progress/isFresh DB 계산, 수동·1회 자동 동기화.
- live 어댑터 생성 hard-block(`KIS_LIVE_CONFIRM`), 비밀값 마스킹, 자격증명 DB 미저장.

**부분 구현:**
- `is_stale` 컬럼은 존재하나 항상 `0` 으로 INSERT — **stale 판정은 백엔드가 안 하고 웹의 `isFresh`(FRESHNESS_SEC) 로만 표현**. `BalanceLine.is_stale`/`Quote.is_stale` 필드도 미활용.
- `get_quote` 어댑터 구현됨 — 그러나 sync 흐름/`quotes` 적재에 연결 안 됨.

---

## 14. 미구현 / placeholder

- **price snapshot(`quotes`) 동기화 미구현** — 테이블·`get_quote` 는 있으나 어떤 job 도 `quotes` 에 INSERT 안 함. 보유종목 현재가/평가 갱신 없음.
- **stale 백엔드 판정 미구현** — `account_snapshots.is_stale` 하드코딩 0. 시세 노후/장중·장후 구분 없음. `--all` 외 스케줄러/cron 없음(수동·UI 1회 자동만).
- **FX 미구현** — `get_fx_rate` `NotImplementedError`. 미국주식/USD 평가액·`fx_rate` 컬럼 미사용.
- **미국주식·체결·미체결주문 미구현** — `get_open_orders`/`get_fills` 빈 리스트, `cancel_order` `NotImplementedError`(api_adapter.md §3 범위 외).
- **holdings.name 미적재** — 잔고 응답 `prdt_name` 을 `domestic_instrument` 가 버려서 `holdings.name`/`account_snapshots` 종목명 NULL.
- **본 영역 자동 테스트 부재** (§12).
- **마이그레이션 분리 미적용** — schema.sql 은 `connect()` 시 IF NOT EXISTS 부트스트랩. `migrations/*.sql` 은 PostgreSQL 초안(CLAUDE.md §6 경고)이며 본 흐름과 별개.
- **동시성/단일 사용자 가정** — sqlite 단일 파일, 동시 sync 충돌 처리 없음.

---

## 15. 다음 개선 항목

1. price snapshot job: 보유종목+universe 티커 `get_quote` → `quotes` 적재, 스냅샷 평가액을 최신가로 재계산.
2. 백엔드 stale 판정: 시세 captured_at 기준 `is_stale` 세팅 + 장중/장후 구분, freshness 와 일관화.
3. 스케줄러(cron/loop)로 주기 `--all` 동기화 — UI 1회 자동 의존 탈피.
4. `holdings.name`(prdt_name) 적재 — UI 종목명 표시.
5. 본 영역 테스트 추가: 자격증명 누락/mock 거부/성공 적재/실패 sync_events/isFresh 경계.
6. FX·미국주식 어댑터 확장(api_adapter.md §7) — value_krw 통화 환산.
7. `sync_events` 를 `audit_logs` 와 연계하거나 보존정책 정의.

---

## 16. 다른 Agent 와의 의존성

| 관계 | 방향 | 내용 |
|---|---|---|
| **Risk / Order Agent** | 본 영역 → | 같은 `KisHttpClient.is_healthy`/`factory.get_broker`/`place_order` 인프라(`port.py`, `kis_adapter.py`)를 공유. 본 영역은 읽기만. live 차단 게이트(`_require_live_confirm`)를 본 영역 factory 가 보유 |
| **Decision / Rebalance Agent** | 본 영역 → | `account_snapshots`(cash/total/현재비중)·`holdings` 를 입력으로 drift/제안 계산 (`decisions`, `getLatestDecision`). 본 영역이 최신 snapshot 미제공 시 후속 결정 불가 |
| **Universe / Profile Agent** | ↔ | `universe_instruments`/`investor_profile` 은 별도 Agent 소관이나 같은 SQLite·웹 page 흐름(`/accounts/[id]/*`) 공유. 가격 동기화 시 universe 티커 참조 예정 |
| **웹 조회 레이어** | 본 영역 → | `portfolioDb.ts` 가 본 영역 적재 결과(accounts/snapshot/holdings)만 읽음 — 모든 화면의 데이터 의존성 시작점 |
| **envStore** | → 본 영역 | 웹이 `.env` 에 계좌 기록 → 본 영역이 그 인덱스를 `discover_indices`/`KisHttpClient`로 읽음 |
