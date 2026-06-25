# Allocation Agent 시스템 프로세스 정리

> 영역 범위(CEO 지정): anchor 생성 · tilt 생성(섹터 상한) · 보수/기준/공격 3안 · 3안 비교 · 사람 선택 · selected allocation 저장 · target 확정 · version 관리.
> 코드 근거: `main_mission/portfolio_os/allocation.py`, `main_mission/portfolio_os/selection.py`, `main_mission/portfolio_os/store/schema.sql`, `web/app/api/accounts/[id]/allocation/route.ts`, `web/app/accounts/[id]/allocation/page.tsx`, `web/lib/server/portfolioDb.ts`.

---

## 1. 목적

대전제(policy: 현금밴드·limits·성향) + 중전제(관심 테마)를 **추적 가능한 목표비중(target allocation)** 으로 변환한다.

- 자유 자연어("공격적/관심=로봇·바이오·양자")가 아니라, `target_allocations` 테이블에 저장된 **anchor + tilt 구조의 3안(보수/기준/공격)** 으로 변환된다(`allocation.py:8`, `generate`).
- 단기 trading 이 아니라 **포트폴리오 비중 관리 + 분할 리밸런싱**이 목적. 3안 각각에 예상 drift·리밸런싱 총액·분할 회차가 추정된다(`selection.estimate`).
- 사람이 1안을 선택해야만 그 계좌의 **공식 target allocation** 으로 확정된다(`selection.select`). 목표비중 확정 없이는 하류(decision/주문 후보) 단계가 진행될 수 없다.
- 모든 선택은 **append-only 이력**(`allocation_selections`) + provenance(policy_version, account_snapshot_id, precheck_status, selected_by, diff)와 함께 기록된다.

---

## 2. 전체 흐름

```text
[대전제 policy] policy.latest()/compile_policy()  ─┐
[중전제 테마]  investor_profile.interests_text   ─┤
                                                  ▼
allocation.generate(account_index)
  · _themes(): interests_text 를 토큰 분리(최대 8개)
  · _variant("conservative"|"base"|"aggressive", cash, themes, sector_max)
      cash → invested → tilt_total(=invested×TILT_SHARE) → broad(anchor) → per-theme tilt(섹터 상한 cap)
  · target_allocations 에 3 variant × N행 INSERT (status='draft', 동일 proposal_id)
                                                  ▼
selection.options(account_index)   ← 웹 GET 진입점
  · 최신 proposal_id 조회(없으면 generate 자동 호출)
  · 각 variant: precheck(rows,policy,stale) + estimate(rows,snapshot,policy)
  · current(account_index) = 현재 active 선택
                                                  ▼
[사람이 1안 선택]  selection.select(account_index, proposal_id, variant)  ← 웹 POST action=select
  · 이전 active → status='superseded'
  · allocation_selections INSERT (status='active', allocation JSON, provenance)
  · target_allocations: 선택 variant→'chosen', 나머지→'archived'
  · rebalance_plans + rebalance_plan_steps 구조적 회차 plan 생성(status='candidate')
                                                  ▼
[확정된 target allocation] → 하류 decision/주문 후보 단계가 이 비중 기준으로 동작
```

선택 취소: `selection.cancel` → active 행을 `status='cancelled'` 로(삭제 금지). 재생성: POST action=generate → 새 proposal_id.

---

## 3. 입력

| 입력 | 출처 | 코드 근거 |
|---|---|---|
| account_index | 웹 URL `[id]` → API `accId()` | `route.ts:9`, 1 이상 정수 검증 |
| 대전제 policy | `portfolio_policies` 최신(`policy.latest`), 없으면 `policy.compile_policy` | `allocation.py:62`, `selection.py:150` |
| cash_band(min/max/target) | policy.cash_band (기본 min=10, max=40, target=중앙값) | `allocation.py:64-67` |
| sector_max_pct | policy.limits.sector_max_pct (기본 30.0) | `allocation.py:68` |
| 중전제 테마 | `investor_profile.interests_text` → `_themes()` | `allocation.py:72-76` |
| 잔고 스냅샷 | `account_snapshots` 최신(`_snapshot`): cash_krw·total_value_krw·captured_at | `selection.py:34-38` |
| single_name_max_pct / one_order_cap_pct / pace | policy.limits / policy.pace | `selection.py:57-58,103` |

입력은 모두 DB(운영 truth) 또는 policy 모듈에서 온다. 웹은 입력을 직접 만들지 않는다.

---

## 4. 출력

| 출력 | 저장/형태 | 코드 근거 |
|---|---|---|
| 3안 draft 목표비중 | `target_allocations`(variant×kind×ref×weight_pct, status='draft') | `allocation.generate` INSERT (`allocation.py:90`) |
| generate 응답 JSON | proposal_id·themes·sector_max_pct·cash_band·variants·note | `allocation.py:99-108` |
| options 응답 JSON | variants{rows, precheck, estimate} + selected + policy_version | `selection.py:166-170` |
| precheck 결과 | status(pass/warn/block) + reasons[] + one_order_cap_pct | `selection.precheck` |
| estimate 결과 | expected_drift_pct·expected_rebalance_total_krw·expected_rebalance_rounds·cycle_cap_pct·current_cash_pct·target_cash_pct | `selection.estimate` |
| 확정 선택 | `allocation_selections`(status='active', allocation JSON + provenance) | `selection.select` INSERT (`selection.py:214`) |
| 회차 plan | `rebalance_plans` + `rebalance_plan_steps`(status='candidate') | `selection.py:232-253` |
| diff | 이전 active 선택 대비 변경(`_diff`) | `selection.py:173-183` |

---

## 5. DB 테이블

| 테이블 | 역할 | 이 영역에서의 쓰기/읽기 |
|---|---|---|
| `target_allocations` | anchor+tilt 3안 제안(draft). 컬럼: account_index, proposal_id, variant, kind(cash/anchor/tilt), ref, weight_pct, status(draft/chosen/archived) | `generate` INSERT(draft) · `select` UPDATE(chosen/archived) · `options` SELECT |
| `allocation_selections` | 사람이 확정한 공식 target allocation(**append-only**). 컬럼: proposal_id, variant, allocation(JSON), policy_version, account_snapshot_id, expected_drift_pct, expected_rebalance_total_krw, expected_rebalance_rounds, precheck_status, precheck_reasons, selected_by, user_override, diff, status(active/superseded/cancelled), selected_at | `select` INSERT(active)+UPDATE(superseded) · `cancel` UPDATE(cancelled) · `current`/`history` SELECT · 웹 `getCurrentSelection`/`getSelectionHistory` SELECT |
| `rebalance_plans` / `rebalance_plan_steps` | 선택 시 생성되는 구조적 회차 plan(분할 매수 골격) | `select` INSERT |
| `portfolio_policies` | 대전제 컴파일 policy(version 관리) — **입력 읽기 전용** | `policy.latest` SELECT |
| `investor_profile` | 중전제 interests_text — **입력 읽기 전용** | `allocation.py:72` SELECT |
| `account_snapshots` | 잔고/현금 truth — **입력 읽기 전용** | `selection._snapshot` SELECT |

> version 관리 = `allocation_selections.id` append-only + status 전이 + `policy_version` 컬럼. target_allocations 자체에는 별도 version 컬럼이 없고 `proposal_id` 단위로 세대를 구분한다.

---

## 6. API / 함수

### Python (백엔드, 진짜 로직)
- `allocation.generate(account_index) -> dict` — 3안 생성·draft INSERT (`allocation.py:61`)
- `allocation._variant(name, cash, themes, sector_max)` — 단일 variant의 cash/anchor/tilt 행 구성. `TILT_SHARE={conservative:0.3, base:0.5, aggressive:0.7}` (`allocation.py:36-58`)
- `allocation._themes(interests_text)` — 구분자 `,/·`·" 및 "·다중공백으로 분리, 최대 8개 (`allocation.py:28`)
- `selection.options(account_index)` — 3안 + precheck + estimate + 현재 선택 (`selection.py:128`)
- `selection.precheck(rows, policy, stale)` — 현금밴드/섹터/단일/투자합/stale 검사 → pass|warn|block (`selection.py:49`)
- `selection.estimate(rows, snapshot, policy)` — drift·총액·회차·cycle_cap 추정 (`selection.py:91`)
- `selection.select(account_index, proposal_id, variant, selected_by, user_override)` — 확정 (`selection.py:186`)
- `selection.current(account_index)` / `selection.history(account_index, limit)` / `selection.cancel(account_index)` (`selection.py:260/272/284`)
- CLI: `python -m main_mission.portfolio_os.allocation --account N --generate`, `... selection --account N --options|--select P V|--cancel`

### Web API (`route.ts`) — Python을 execFile로 호출, 직접 SQL/KIS 호출 없음
- `GET /api/accounts/[id]/allocation` → `runPy("selection", ["--account", id, "--options"])`
- `POST` action=`generate`→allocation, `select`(proposal_id·variant 필수)→selection, `cancel`→selection (`route.ts:46-55`)
- `runPy()`는 stdout 마지막 줄을 JSON 파싱(`route.ts:21`), timeout 30s.

### Web 조회 헬퍼 (`portfolioDb.ts`, node:sqlite readOnly)
- `getCurrentSelection(index)` — active 선택 1행 (`portfolioDb.ts:129`)
- `getSelectionHistory(index, limit=20)` — 전체 선택 이력 (`portfolioDb.ts:135`)
- `getLatestPolicy`/`getProfile` 등 입력 조회.

---

## 7. UI 화면

`web/app/accounts/[id]/allocation/page.tsx` — "목표 포트폴리오 확정 (3안 중 선택)".

- 3 카드(보수/기준/공격, `LABEL`): 현금 / 광범위 기본(anchor) / 테마별 tilt 비중 표시 (`page.tsx:107-128`).
- 예상치 블록: 예상 조정량(drift) · 리밸런싱 총액 · 분할 회차 (`page.tsx:130-134`).
- `PreBadge`: precheck status → "한도 위반"(block) / "주의"(warn) / "한도 내"(pass) (`page.tsx:26-30`).
- pre-check 사유는 info 제외하고 block/warn만 표시 (`page.tsx:136-142`).
- 버튼: "이 안으로 확정" / block 이면 "한도 위반 — 무시하고 선택"(user_override=1로 POST) (`page.tsx:143-146,58-59`).
- 상단: 현재 확정 배너 + "3안 다시 생성" + "선택 취소" (`page.tsx:75-97`).
- 데이터는 전부 `/api/accounts/[id]/allocation` fetch (DB truth 경유). mock/하드코딩 비중 없음.

---

## 8. 상태 전이

target_allocations.status:
```
draft ──(해당 variant 선택)──► chosen
draft ──(다른 variant 선택)──► archived
```

allocation_selections.status:
```
(INSERT) active
active ──(새 select)──► superseded
active ──(cancel)─────► cancelled
```
- 삭제 없음(append-only). 이전 active 는 항상 superseded/cancelled 로 보존(`selection.py:211,287`, 스키마 주석 `schema.sql:308-309`).

rebalance_plan_steps.status: 생성 시 `candidate`(스키마상 candidate|hold|blocked).

---

## 9. 예외 / 실패 케이스

| 케이스 | 처리 | 코드 근거 |
|---|---|---|
| proposal/variant 행 없음 | `{"ok":false, "error":"해당 proposal/variant 없음"}` | `selection.py:192` |
| 잔고 스냅샷 없음 | select 거부 `"잔고 스냅샷 없음 — 동기화 필요"` | `selection.py:195` |
| 스냅샷 stale(>24h) | precheck **block** "스냅샷이 오래됨" | `STALE_HOURS=24`, `selection.py:85-86` |
| 현금 < 밴드 하한 | precheck **block** | `selection.py:71-72` |
| 테마 > 섹터 한도 | precheck **block** | `selection.py:76-77` |
| 투자비중 합 > 100% | precheck **block** | `selection.py:83-84` |
| options 시 제안 없음 | `generate` 자동 호출 후 재조회 | `selection.py:138-148` |
| Python 미발견 | route.ts에서 `python/python3/py` 순차 시도, 모두 ENOENT면 throw | `route.ts:16-28` |
| Python 내부 예외 | CLI `main()`가 `{"ok":false,"error":"내부 오류: ..."}` 반환(비밀값 없음) | `allocation.py:118`, `selection.py:311` |
| DB 파일 없음(웹 조회) | `open()` null → 빈 결과 | `portfolioDb.ts:12-19` |

---

## 10. Hard-block 조건

precheck `block` 은 UI에서 빨간 배지 + 버튼 문구가 "한도 위반 — 무시하고 선택"으로 바뀐다. 다음이 block:
- 현금비중 < cash_band.min (방어현금 부족)
- 테마 tilt > sector_max_pct (섹터 쏠림)
- 투자비중 합 > 100%
- 스냅샷 stale(>24h)

> ⚠️ **주의(위험)**: 현재 select 은 block 이어도 **물리적으로 차단하지 않는다**. UI가 `user_override=1` 을 실어 POST 하면 `selection.select` 는 precheck 결과를 그대로 기록만 하고 정상 INSERT 한다(`selection.py:202-222`). 즉 block 은 "기록되는 경고 + override 표식"이지 하류 주문 hard-block 의 대체가 아니다.
> live 주문 hard-block(`KIS_LIVE_CONFIRM`)·종목 qty=0/가격이상치 차단은 **이 영역이 아니라** 확정 후 decision/order 단계 책임(`selection.py:87`, `page.tsx:155` 명시). 이 영역에서 KIS 호출·주문 생성은 없음.

---

## 11. 로그 / 감사 기록

- **provenance 기록(주 감사 수단)**: 각 선택은 `allocation_selections` 에 policy_version, account_snapshot_id, precheck_status, precheck_reasons(JSON), selected_by, user_override, diff(이전 대비 변경), selected_at 과 함께 저장 → 누가/어떤 정책버전/어떤 잔고스냅샷에서/어떤 사전검사로 골랐는지 재구성 가능.
- append-only + status 전이로 모든 세대 보존(삭제 금지).
- **미연동(위험)**: 공용 `audit_logs` 테이블에 allocation generate/select/cancel 이벤트를 적재하는 코드는 **없음**. 감사 추적은 현재 `allocation_selections`/`target_allocations` 자체 이력에만 의존한다.

---

## 12. 테스트 기준

- 코드 내 테스트 파일/단정은 이 영역에 **없음**(검색 기준 allocation/selection 전용 테스트 미발견). 결정론 검증 포인트(권장 기준):
  - `_variant`: cash+anchor+tilt 합 = 100.0(상한 cap 후 잔여는 cash 로 보정, `allocation.py:55-57`).
  - `TILT_SHARE` variant별 tilt_total 비율(0.3/0.5/0.7) 정확.
  - per-theme tilt ≤ sector_max.
  - precheck block 조건(현금/섹터/합/stale) 각각.
  - estimate rounds = ceil(max_pos / cycle_cap), cycle_cap = min(one_order_cap, PACE_CAP[pace]).
  - select 후: 이전 active → superseded, target_allocations 선택→chosen/나머지→archived, plan/step 생성.

> 현재 상태: 자동 테스트 미구현 → §14.

---

## 13. 현재 구현 상태

**구현됨(동작):**
- anchor+tilt 3안 생성·draft 저장 (`allocation.generate`).
- 섹터 상한 cap(`per = min(per, sector_max)`)·over-invest 시 잔여 현금 보정.
- TILT_SHARE 기반 보수/기준/공격 적극성 차등 + 변이별 현금(공격=하한/기준=목표/보수=상한).
- 3안 비교(precheck status + drift/총액/회차 estimate) — `options`.
- 사람 선택 확정 → append-only `allocation_selections` + provenance + diff.
- 선택 variant chosen / 나머지 archived 표시.
- 회차 plan/step 구조적 생성.
- 재선택(supersede)·취소(cancel) — 이력 보존.
- 웹 화면 3카드 비교·확정·취소·재생성, DB truth 조회만(KIS 직접 호출 없음, mock 비중 없음).
- version 관리: policy_version 기록 + selection id append-only + proposal_id 세대 구분.

**의도적으로 이 영역 밖(타 단계 책임):**
- 종목 단위 매핑(소전제/universe), qty=0·가격 이상치 차단, 실제 주문/KIS 호출, live 가드. tilt.ref 는 테마명일 뿐 종목이 아니며 plan step의 ticker 에 테마명/kind 가 들어간다(`selection.py:250`).

---

## 14. 미구현 / placeholder

- **자동 테스트 없음** — allocation/selection 결정론 단정 미작성.
- **audit_logs 미연동** — generate/select/cancel 이 공용 감사로그에 남지 않음(§11).
- **block 물리 차단 없음** — user_override 로 block 안도 확정 가능(§10). 정책상 "기록 후 하류 차단"인지 "선택 자체 차단"인지 미확정.
- **인버스/레버리지 precheck placeholder** — "본 3안엔 인버스/레버리지 테마 없음 → 0(pass)"로 검사 생략(`selection.py:80`). 테마에 인버스/레버리지 도입 시 미작동.
- **estimate 의 보유종목 테마 매핑 미구현** — drift 를 "구조적 일방 전개량(미보유면 0)"으로만 계산(`selection.py:99-100`). 보유 종목이 어떤 테마인지 모르므로 매도/감축 drift 미반영.
- **plan step 의 limit_price=None / cycle_qty=None** — 지정가·수량은 이 단계에서 채우지 않음(소전제/decision 단계 위임, `selection.py:251`).
- **rebalance_plans.decision_id = None** — 선택 단계에선 decision 미연결(`selection.py:233`).
- **custom variant 미구현** — 스키마는 `variant='custom'` 허용하나 생성/선택 경로 없음(보수/기준/공격만).
- **Vector(근거검색)/Graph(관계설명) 미연동** — evidence_documents/decision_evidence_links 스키마만 존재, 이 영역에서 사용 안 함.

---

## 15. 다음 개선 항목

1. allocation generate/select/cancel → `audit_logs` 적재(actor/action/entity_id=selection_id).
2. allocation/selection 결정론 테스트 추가(§12 포인트).
3. block hard-block 정책 확정: 선택 자체 차단 vs 하류 주문 차단 명문화하고 user_override 의미 정리.
4. 보유종목→테마 매핑(소전제 universe 연계)으로 estimate drift 를 매도 포함 양방향으로 정확화.
5. 인버스/레버리지 테마 도입 시 precheck 활성화(policy.limits 의 leverage/inverse 한도 연결).
6. plan step 에 지정가(limit_price)·종목/수량 채우는 decision 단계 연결.
7. custom variant(사람 직접 비중 편집) 경로 + 동일 provenance 기록.
8. proposal_id 세대에 명시적 version/created_by 메타 부여.

---

## 16. 다른 Agent와의 의존성

| 의존 대상 | 방향 | 내용 |
|---|---|---|
| Profile/Strategy(대·중전제) Agent | **입력** | `investor_profile.interests_text`(중전제 테마), `portfolio_policies`(대전제 policy: cash_band·limits·pace). `policy.latest`/`compile_policy` 호출(`allocation.py:62`, `selection.py:150`). |
| Sync/Account Agent | **입력** | `account_snapshots`(cash/total/captured_at) — estimate·stale 판정 근거. 없으면 select 거부. KIS→DB 동기화는 이 영역 밖. |
| Universe/소전제 Agent | **하류(미연결)** | tilt 테마 → 종목 매핑·목표비중·가격이상치 차단은 universe/decision 단계 책임. 현재 plan step ticker 에 테마명만 들어감(미연결). |
| Decision/Order Agent | **하류** | 확정 `allocation_selections`(active) 의 allocation 을 목표비중으로 소비. 실제 drift 계산·주문 후보·리스크 게이트·live 가드(`KIS_LIVE_CONFIRM`)는 그 단계 책임. |
| Web(조회 전용) | **읽기** | `getCurrentSelection`/`getSelectionHistory` 로 DB truth 만 조회. 쓰기는 route.ts→Python CLI 경유(웹이 SQL/KIS 직접 안 함). |
| Lessons/Evidence(성장) | **미연결** | lessons/evidence_documents/lesson_candidates 스키마 존재하나 이 영역에서 적재·조회 코드 없음(향후 근거연결 대상). |
