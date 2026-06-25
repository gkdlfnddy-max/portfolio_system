# UI / UX Agent 시스템 프로세스 정리

> 본 문서는 Portfolio OS 웹 프런트엔드(Next.js App Router, `web/`) 의 "조회 전용 운영 화면" 영역을 실제 코드 기준으로 정리한다.
> 원칙: 단기 trading 이 아니라 **포트폴리오 비중 관리 + 분할 리밸런싱**. 웹은 **DB(SQLite) truth 를 조회만** 하고, KIS 호출·쓰기는 백엔드(Python sync/job)만 수행한다.

---

## 1. 목적

- CEO(사용자)가 계좌를 위임한 뒤 **의사결정 위계(대전제→중전제→소전제)** 를 입력하고, 그 결과로 계산된 목표비중·drift·분할 리밸런싱 계획·리스크 게이트 결과를 **읽기 쉬운 화면**으로 확인하게 한다.
- 화면은 "주문 버튼" 중심이 아니라 **포트폴리오 구조(현금밴드·섹터노출·종목비중·drift)** 중심이다.
- 모든 표시값은 백엔드가 DB에 적재한 운영 truth 의 조회 결과이며, 화면에서 mock/하드코딩 수치를 만들지 않는다. (`web/lib/server/portfolioDb.ts` 가 `readOnly:true` 로만 DB를 연다.)
- 정보 우선순위: **계좌 구조/잔고 → 전략(대·중전제) → 3안 확정(목표비중) → drift/리밸런싱 계획 → 리스크 게이트 → (예정) 승인**.

---

## 2. 전체 흐름

```text
홈(page.tsx)  ─ 관리 중인 계좌 카드(getAccounts) + mode/sync 배지
  └─ 계좌상세(/accounts/[id])  ─ getAccountView: 연결 4단계 진행도 + 실잔고 스냅샷
        ├─ 1. 운용 전략(/strategy)        대전제·중전제 입력 → /api/.../profile, /api/profile/distill
        │      └─ 정리 문서(/strategy/view) getProfile/getProfileHistory/getLatestPolicy (읽기전용)
        ├─ 2. 목표 포트폴리오 확정(/allocation)  3안(보수/기준/공격) pre-check 후 select
        ├─ 3. 종목 유니버스(/universe)     KIS 검증 종목 + 목표비중 (소전제)
        └─ 4. 포트폴리오 비중 관리(/portfolio) getLatestDecision: drift·분할계획·리스크게이트
```

- 클라이언트 컴포넌트(`"use client"`)는 `fetch(`/api/...`)` 로 백엔드 API를 호출해 DB 조회 결과를 받는다. 서버 컴포넌트(home, [id], strategy/view)는 `portfolioDb.ts` 함수를 직접 호출한다.
- `AccountSync.tsx` 는 스냅샷이 최신이 아니면(`!view.isFresh`) 1회 자동으로 `POST /api/accounts/[id]/sync`(백엔드 job 트리거)를 호출한 뒤 DB를 재조회한다 — UI가 직접 KIS를 부르지 않는다.

---

## 3. 입력

이 영역의 입력은 "사용자 폼 입력" 과 "URL 파라미터" 두 종류다.

| 화면 | 입력 | 코드 근거 |
|---|---|---|
| 계좌 연결(`accounts/new/page.tsx`) | alias, mode(paper/live), appKey, appSecret, accountNo, productCode | `accountFormSchema`(`lib/forms/accountSchema.ts`) zod 검증 |
| 전략(`strategy/page.tsx`) | posture_text, risk_tolerance, short_policy, cash_min_pct, cash_max_pct, horizon, interests_text, views_text, individual_cap_pct, individual_count, region_pref, rebalance_pace | `type Form` |
| 전략 distill | posture_text(자유서술) → `POST /api/profile/distill` 로 구조화 제안 | `distill()` |
| 3안(`allocation/page.tsx`) | action(`generate`/`select`/`cancel`), proposal_id, variant, user_override | `act()`, `selectVariant()` |
| 유니버스(`universe/page.tsx`) | ticker(국내 6자리), weight | `add()`/`setWeight()`/`remove()` (정규식 `/^\d{6}$/`) |
| 포트폴리오(`portfolio/page.tsx`) | (입력 없음) `POST /api/.../decision` 재계산 트리거만 | `recompute()` |
| 공통 | URL의 `params.id` = `account_index` | 각 페이지 `useParams()`/`params` |

---

## 4. 출력

전부 화면 렌더링(읽기). 주요 출력:

- **홈**: 계좌 카드(alias, ModeBadge, account_no_masked, SyncBadge, last_synced_at), 빈 상태.
- **계좌상세**: 연결 진행도 막대(`progress`), 4단계 체크(credentials/token/balance/ready), 예수금·총평가액·보유종목 표(holdings), 동기화 오류 메시지(`last_error`), 다가오는 일정(미연동 빈 상태).
- **전략**: 칩 선택값, distill 키워드/보완점(gaps), 저장 상태.
- **전략 문서(view)**: 컨셉 원문, 핵심 변수 표, **투자 정책값(컴파일됨 v{version})**, 키워드, 보완점, **변경 이력(append-only 버전)**, provenance(`refined_by`, `updated_at`).
- **3안**: 안별 구조(현금/anchor/tilt 비중), 예상치(drift/리밸런싱총액/분할회차), pre-check 배지(block/warn/ok)+사유, 현재 확정 안.
- **유니버스**: 종목표(ticker/업종/현재가/목표비중), 목표비중 합(100% 여부 색상).
- **포트폴리오**: 총평가액·현금현재→목표(밴드)·목표비중합·오늘조정후보, 섹터노출 막대, 현재vs목표+drift 표(5/25 band), **이번 회차 분할 리밸런싱 카드**(direction/전체조정/이번회차/남은조정/분할·지정가/차단), **리스크 게이트 통과·차단**, provenance 푸터.

---

## 5. DB 테이블

이 영역은 직접 SQL을 실행하지 않고 `web/lib/server/portfolioDb.ts` 를 통해서만 조회한다. 사용 테이블(코드 SELECT 기준):

| 테이블 | 사용 함수 | 화면 |
|---|---|---|
| `accounts` | `getAccounts`, `getAccountView`(account_index, alias, mode, account_no_masked, has_credentials, token_status, sync_status, last_error, last_synced_at) | 홈/계좌상세 |
| `account_snapshots` | `getAccountView`(cash_krw, total_value_krw, holdings_count, source, captured_at) | 계좌상세 잔고 |
| `holdings` | `getAccountView`(ticker, name, qty, avg_price, market_value, currency; `WHERE snapshot_id=?`) | 보유종목 표 |
| `investor_profile` | `getProfile` (대·중전제 컬럼 + `doc` JSON) | 전략/문서 |
| `investor_profile_history` | `getProfileHistory`(id, snapshot, source, created_at) | 변경 이력 |
| `portfolio_policies` | `getLatestPolicy`(version, policy JSON) | 정책값 표시 |
| `allocation_selections` | `getCurrentSelection`/`getSelectionHistory`(variant, allocation, policy_version, expected_drift_pct, precheck_status, status='active') | 3안 확정 |
| `universe_instruments` | `getUniverse`(ticker, market, name, asset_class, currency, target_weight_pct, last_price, verified_at; `is_active=1`) | 유니버스 |
| `decisions` | `getLatestDecision`(payload JSON, created_at) | 포트폴리오 |

> 자격증명(appKey/appSecret/계좌번호 평문)은 어떤 테이블에도 없고 `.env` 전용이라는 원칙을 화면 카피에 반복 노출(`accounts/new`, 홈 푸터).

---

## 6. API / 함수

이 영역이 호출하는 백엔드 API 라우트(`web/app/api/...`) — 구현은 별도 Agent 영역이며, UI는 호출자다:

| 메서드/경로 | 호출 위치 | 용도 |
|---|---|---|
| `POST /api/accounts` | `accounts/new` `submit()` | 계좌 연결(.env 저장) |
| `GET /api/accounts/[id]` | `AccountSync.load()` | 계좌 뷰(JSON) |
| `POST /api/accounts/[id]/sync` | `AccountSync.runSync()` | 백엔드 동기화 job 트리거 |
| `GET/POST /api/accounts/[id]/profile` | `strategy` load/save | 프로필 조회/저장 |
| `POST /api/profile/distill` | `strategy.distill()` | 컨셉→대전제 구조화 제안 |
| `GET/POST /api/accounts/[id]/allocation` | `allocation`, `portfolio` | 3안 조회/생성/선택/취소 |
| `GET/POST/PATCH/DELETE /api/accounts/[id]/universe` | `universe` | 유니버스 CRUD |
| `GET/POST /api/accounts/[id]/decision` | `portfolio` load/recompute | 의사결정 조회/재계산 |

UI 측 서버 조회 함수(`portfolioDb.ts`): `getAccounts`, `getAccountView`, `getProfile`, `getProfileHistory`, `getLatestPolicy`, `getCurrentSelection`, `getSelectionHistory`, `getUniverse`, `getLatestDecision`. 모두 실패 시 빈 배열/`null` 반환(throw 안 함).

---

## 7. UI 화면

| 경로 | 파일 | 성격 |
|---|---|---|
| `/` | `web/app/page.tsx` | 서버 컴포넌트. Hero/관리자 소개/동작 3단계/계좌 카드/안전장치 6칸 |
| `/accounts/new` | `accounts/new/page.tsx` | 클라이언트 폼. zod 검증 + LiveModeConfirm 모달 |
| `/accounts/[id]` | `accounts/[id]/page.tsx` | 서버 컴포넌트 + `AccountSync` 클라이언트 |
| `/accounts/[id]/strategy` | `strategy/page.tsx` | 클라이언트. 대·중전제 입력 + distill |
| `/accounts/[id]/strategy/view` | `strategy/view/page.tsx` | 서버 컴포넌트. 읽기전용 정리 문서 |
| `/accounts/[id]/allocation` | `allocation/page.tsx` | 클라이언트. 3안 카드 + pre-check |
| `/accounts/[id]/universe` | `universe/page.tsx` | 클라이언트. 종목·목표비중 |
| `/accounts/[id]/portfolio` | `portfolio/page.tsx` | 클라이언트. 비중관리/drift/리밸런싱/리스크 |

공통 컴포넌트: `Nav.tsx`(홈/계좌연결 링크만), `AccountSync.tsx`(진행도+잔고), `LiveModeConfirm.tsx`(실전전환 체크리스트 모달), `components/ui/*`(Card/Button/Badge/Input/Label/Tabs/Checkbox/Skeleton).

배지 규칙: `ModeBadge` — mock=`데모(mock)` 회색, paper=`모의투자` 파랑, live=`실전` 빨강(error). `SyncBadge` — ok=동기화됨/error=동기화오류/그외=미동기화.

---

## 8. 상태 전이

- **계좌 연결 진행도**(`getAccountView.steps`, DB 상태 계산): `credentials(has_credentials=1)` → `token(token_status='ok')` → `balance(snapshot 존재 && sync_status='ok')` → `ready(+isFresh)`. progress=완료단계/4*100. 100%일 때만 "다음: 운용 전략" 버튼 노출.
- **동기화 신선도**: `last_synced_at` 이 `SYNC_FRESHNESS_SEC`(env, 기본 900초) 이내면 `isFresh=true`. 아니면 AccountSync가 자동 1회 sync(`autoTried` ref 가드).
- **3안 선택 상태**: variant 미선택 → `select` → `allocation_selections.status='active'`(append-only, 이전 안 보존) → `cancel` 가능. portfolio 화면은 `selected` 없으면 경고 + 진행 차단(주문 후보 생성 안 함).
- **저장 상태(전략)**: 입력 변경 시 `saved=false`, 저장 성공 시 `saved=true`(버튼 "저장됨").

---

## 9. 예외 / 실패 케이스

- **DB 없음/조회 실패**: `portfolioDb.open()`이 `null`, `query()`가 `[]` 반환 → 화면은 빈 상태("아직 동기화된 잔고가 없습니다" 등)로 폴백. 에러를 던지지 않음.
- **계좌 미존재**: `accounts/[id]/page.tsx` 에서 `getAccountView` 가 `null` → `notFound()`. `id` 가 정수<1 이어도 `notFound()`.
- **동기화 오류**: `view.sync_status==='error'` 면 `last_error` 를 빨간 박스로 표시(AccountSync).
- **distill 실패**: posture_text 비면 "먼저 컨셉을…" 안내, API 실패 시 `j.error || "정리 실패"`.
- **유니버스 입력 오류**: 6자리 정규식 불일치 시 "국내 종목코드 6자리를 입력하세요", API `!ok` 시 `d.error`.
- **스냅샷 stale**: portfolio 푸터에 "⚠ 스냅샷이 오래됨 — 동기화 권장".
- **fetch 자체 실패**: try/catch 로 `view=null` 또는 에러 메시지 표시(앱 크래시 없음).

---

## 10. Hard-block 조건

UI는 **표시·게이트 트리거**만 담당하고 실제 hard-block 은 백엔드/리스크 게이트에 있다. 화면에 반영되는 차단 표현:

- **3안 pre-check `block`**: PreBadge "한도 위반", 버튼 라벨 "한도 위반 — 무시하고 선택"(클릭 시 `user_override=1` 로 명시 기록).
- **리스크 게이트 차단**(`decision.risk.passed===false`): "차단 — N건. 이번 회차 보류" + 위반 상세(limit/observed/threshold). 통과 시에만 "승인 단계로 진행 가능" 카피.
- **종목 라인 차단**(`line.blocked`): 빨간 카드 + `block_reason`(qty=0·가격 이상치 등).
- **목표비중 없음 → 주문 후보 금지**: `selected` allocation 없으면 portfolio 가 후보를 만들지 않고 경고만(카피: "주문 후보는 이 확정 목표 기준으로만 생성됩니다").
- **live 전환**: `LiveModeConfirm` 3개 체크 전부 통과 전까지 "실전 활성화" 비활성. (단, 실제 live 주문 hard-block(`KIS_LIVE_CONFIRM`)은 백엔드 책임 — UI 는 "실전 주문은 차단 상태" 카피만 노출.)

> UI 자체가 강제할 수 있는 hard-block 은 폼검증·버튼 disable 수준이며, 금액/주문 truth 차단은 UI 영역이 아니다.

---

## 11. 로그 / 감사 기록

- 이 영역은 **로그를 직접 쓰지 않는다**(웹=조회 전용). 감사/provenance 는 백엔드가 DB에 적재하고 UI는 그것을 **표시**한다.
- 표시되는 감사·근거 요소:
  - 전략 문서: `investor_profile_history`(append-only 버전, source, created_at) + `refined_by`/`updated_at`.
  - 정책값: `portfolio_policies.version`("provenance 에 기록" 카피).
  - 3안: 선택 append-only, `precheck_status`, `selected_at`.
  - portfolio 푸터: `provenance.account_snapshot_id`, `universe_active_count`, `risk_policy.sector_max_pct`, snapshot_at/saved_at.

---

## 12. 테스트 기준

- **현재 이 영역 전용 자동화 테스트 없음(미구현)**. (메모리상 백엔드 주문 안전 백본 16테스트는 Python 측이며 UI 영역 아님.)
- 수동 검증 기준(코드로 보장되는 동작):
  1. DB 파일 없을 때 모든 화면이 크래시 없이 빈 상태로 렌더.
  2. `getAccountView` 진행도 = DB 상태(has_credentials/token_status/sync_status/snapshot)와 일치.
  3. live 모달 3체크 전 "실전 활성화" 비활성.
  4. allocation `selected` 없으면 portfolio 가 주문 후보를 표시하지 않음.
  5. 유니버스 비6자리 입력 거부.
- 권장(미구현) 테스트: 빈 상태 스냅샷 테스트, ModeBadge/SyncBadge 렌더 단위 테스트, accountSchema zod 케이스.

---

## 13. 현재 구현 상태

**구현됨(실 코드 존재·동작):**
- 홈 계좌 목록 + mode/sync 배지 + 빈 상태 (`page.tsx`).
- 계좌 연결 폼(zod 검증, live 체크리스트 모달) (`accounts/new`).
- 계좌상세 연결 진행도 4단계 + 실잔고 스냅샷 + 자동 1회 동기화 (`AccountSync`).
- 전략 입력(대·중전제 폼, 칩, distill 호출, 키워드/보완점) (`strategy`).
- 전략 정리 문서(읽기전용, 핵심변수/정책값 v버전/변경이력 append-only) (`strategy/view`).
- 3안 확정(보수/기준/공격, pre-check 배지·사유, select/cancel/generate, user_override) (`allocation`).
- 유니버스 CRUD + 목표비중 합 표시(KIS 검증 메시지) (`universe`).
- 포트폴리오 비중관리(섹터노출/현재vs목표 drift 5-25band/분할 리밸런싱 이번회차/리스크 게이트/provenance) (`portfolio`).
- paper/live/mock 표시 일관 적용, "주문은 승인 후·실전 차단" 카피 전반 노출.
- 전 화면이 DB truth 조회 기반(`portfolioDb.ts`, readOnly) — UI 하드코딩 수치 없음.

**부분 구현:**
- 동기화는 UI가 트리거하지만 실제 KIS 호출·DB 적재는 백엔드 job 의존(이 영역 밖).
- 리스크/3안/decision 계산 로직은 API 라우트(다른 Agent 영역)에 있고 UI는 결과 표시만.

---

## 14. 미구현 / placeholder

- **승인 대기 UI 미구현**: 승인 화면·라우트(`/approvals`)·승인 버튼 없음. `approval_pending` 은 `web/lib/portfolio/types.ts` 의 타입 정의로만 존재하고 페이지/컴포넌트 미연결. 화면 카피만 "승인 후 실행"으로 안내(실제 승인 입력 UI 없음).
- **다가오는 일정·이벤트**(계좌상세): 실적발표/배당락/공시/뉴스 — "현재 미연동" 빈 상태 placeholder (DART·뉴스 소스 미연동).
- **종목 검색 UX**: 한글 종목명·해외주식·자동완성 미지원(현재 국내 6자리 코드 직접입력 + KIS 검증만). 카피로 명시.
- **Nav 한계**: 상단 네비에 "홈/계좌 연결" 두 링크뿐 — 전략/3안/포트폴리오 직접 진입 메뉴 없음(계좌상세에서만 진입).
- **Vector(근거검색)/Graph(관계설명) 표시 UI 미구현**: distill 키워드/gaps 텍스트 표시 수준. RDB 조회만 화면화.
- **이 영역 전용 자동화 테스트 미작성**.

---

## 15. 다음 개선 항목

1. **승인 대기 화면 신설**: decision 의 today_candidate/리스크 통과분을 승인 큐로 보여주고 CEO 승인 입력(approvals 테이블/라우트 연동). live 는 `KIS_LIVE_CONFIRM` 상태를 화면에서 가시화.
2. **다가오는 일정 연동**: DART/뉴스 소스 연결 후 placeholder 교체.
3. **종목마스터 적재 → 한글명/해외주식/자동완성** 검색 UX.
4. **Nav 개선**: 계좌 컨텍스트 내 단계 네비(전략→3안→유니버스→포트폴리오) 노출.
5. **빈 상태/에러 상태 단위 테스트** + 접근성(aria) 보강.
6. drift/리밸런싱 카드에 **근거(Vector) 펼치기**·종목 관계(Graph) 설명 표시.

---

## 16. 다른 Agent와의 의존성

- **계좌/잔고/동기화 Agent(백엔드 sync job + `/api/accounts/[id]/sync`)**: `accounts`/`account_snapshots`/`holdings` 적재. UI 진행도·잔고가 이 데이터에 전적으로 의존.
- **전략/프로필 Agent(`/api/.../profile`, `/api/profile/distill`)**: `investor_profile`·`investor_profile_history`·`portfolio_policies` 생성. 전략 화면·문서가 소비.
- **목표비중/리밸런싱 Agent(`/api/.../allocation`, `/api/.../decision`)**: 3안 pre-check, `allocation_selections`, `decisions` 페이로드 생성. allocation/portfolio 화면이 소비.
- **유니버스 Agent(`/api/.../universe`)**: KIS 검증 + `universe_instruments` 적재. universe 화면이 소비.
- **리스크 게이트 Agent(`risk/gate.py` 결과)**: decision payload 의 `risk.passed/violations`, pre-check `status` 로 화면에 표출. 차단의 truth 는 백엔드.
- **승인/주문 Agent(미구현 연결)**: 본 영역은 승인 UI 가 없어 현재 카피로만 연결. 향후 approvals 라우트 의존 예정.

> UI/UX Agent 는 위 Agent들이 DB에 남긴 결과를 **표시·입력 폼·게이트 트리거**로만 책임지며, 금액/주문/리스크 truth 의 생성·차단은 책임지지 않는다.
