# Rebalance Plan Agent 시스템 프로세스 정리

> 작성 기준: 실제 코드(`main_mission/portfolio_os/decision.py`, `selection.py`, `store/schema.sql`, `web/`)
> 단기 trading 이 아니라 **포트폴리오 비중관리 + 분할 리밸런싱**이다. 한 번에 목표를 다 맞추지 않는다.
> 웹은 DB truth 조회 전용 · KIS 호출은 백엔드 sync/job 만 · 한글 문서 / 영문 코드.

---

## 1. 목적

- 확정된 **목표비중(allocation_selection)** 과 **현재비중(account_snapshots/holdings)** 의 drift 를, **회차(cycle) 단위로 분할**해 며칠·일주일에 걸쳐 안전하게 좁히는 **분할 매수/매도 계획**을 산출한다.
- 산출물 = `rebalance_plans`(decision 1회 = plan 1개) + `rebalance_plan_steps`(조정 종목별 회차 step).
- 조정 속도(대전제 `rebalance_pace`)를 회차당 상한(`cycle_cap`)에 반영하고, **1주문 한도(`single_order_max_pct`)를 절대 초과하지 않는다.**
- qty=0(최소주문 미달) 등은 **보류/차단 후보(blocked)** 로 표기해 잘못된 주문을 방지한다.
- 두 진입점:
  - 구조적 계획: `selection.select()` — 3안 선택 확정 시 테마/광범위 전개 plan(`decision_id=NULL`).
  - 회차 계획: `decision.compute()` — 잔고 drift 기준 종목 단위 회차 plan(`decision_id` 연결).

---

## 2. 전체 흐름

```text
[대전제·중전제 profile]  →  policy(pace, limits, cash_band)
        │
[3안 target_allocations] ──선택──► selection.select()
        │                              ├─ precheck(현금밴드/섹터/단일/투자합/stale)
        │                              ├─ estimate(expected_drift/total_krw/rounds, cycle_cap)
        │                              └─ INSERT rebalance_plans(decision_id=NULL)
        │                                   + rebalance_plan_steps(구조적 전개, status=candidate)
        ▼
[account_snapshots + holdings + universe_instruments(목표비중)]
        │
   decision.compute(account)
        ├─ 종목별 drift = current - target,  band = min(one_order_cap, target*0.25)  (5/25)
        ├─ needs_adjust = |drift| > band and |drift| > 0.1
        ├─ 회차 분할: cycle_cap = min(one_order_cap, pace_cap[pace])
        │     total_pct → this_cycle_pct(≤cycle_cap) → remaining_pct, split_rounds=ceil(total/cap)
        ├─ cycle_qty = floor(cycle_krw / last_price); 매수 & qty==0 → blocked(보류후보)
        ├─ 포트폴리오 리스크 게이트(현금밴드/단일집중/qty0/stale/섹터)
        ├─ INSERT decisions(payload=JSON)
        └─ INSERT rebalance_plans(decision_id) + rebalance_plan_steps(status=candidate|blocked)
        ▼
[웹] /accounts/{id}/portfolio  ── decisions.payload(JSON) 조회만 표시
        ▼
   (다음 cycle) 동기화 후 다시 계산 → 남은 drift 재평가 → 새 plan
```

---

## 3. 입력

| 입력 | 출처(테이블/필드) | 비고 |
|---|---|---|
| 최신 잔고 스냅샷 | `account_snapshots.total_value_krw, cash_krw, captured_at` (최신 1행) | 없으면 즉시 중단 |
| 보유종목 | `holdings.ticker, market_value` (snapshot_id 기준) | 현재비중 계산 |
| 목표비중(소전제) | `universe_instruments.ticker, target_weight_pct, last_price, asset_class, is_active=1` | decision.compute 입력 |
| 확정 목표(구조) | `target_allocations.kind, ref, weight_pct` (proposal_id+variant) | selection.select 입력 |
| 대전제 운용 | `investor_profile.rebalance_pace, cash_min_pct, cash_max_pct, individual_cap_pct` | pace→cycle_cap, 현금밴드 |
| 컴파일 정책 | `portfolio_policies.policy`(JSON: limits/cash_band/pace) via `policy.latest()` | selection 경로 |
| 리스크 한도 | `risk.gate.RiskLimits`(single_order_max_pct=5, cash_min_pct=10, single_name_max_pct=20) | 코드 기본값 |

---

## 4. 출력

`rebalance_plans` 1행 + 조정 종목당 `rebalance_plan_steps` 1행.

step 핵심 필드(`decision.py` 기준):
- `direction`(매수|매도), `total_pct`/`total_krw`(목표까지 전체 조정)
- `cycle_pct`/`cycle_krw`/`cycle_qty`(이번 회차 = `min(total_pct, cycle_cap)`)
- `remaining_pct`(남은 조정), `round_no`(현재 1 고정), `total_rounds`(=split_rounds=ceil(total/cap))
- `limit_price`(현재 `last_price` 그대로 — 지정가 기준, "무릎" 정밀화는 미구현)
- `status`(candidate | blocked), `reason`(차단사유)

`decisions.payload`(JSON, 웹이 실제 표시) 에도 같은 분할 정보가 `lines[]`(`this_cycle_pct`, `this_cycle_qty`, `split_rounds`, `blocked`, `block_reason` 등) + `today_candidate_count`, `blocked_count` 로 들어간다.

`selection.select()` 출력: `{selection_id, plan_id, precheck, estimate{expected_drift_pct, expected_rebalance_total_krw, expected_rebalance_rounds, cycle_cap_pct}, diff}`.

---

## 5. DB 테이블

| 테이블 | 역할 | 이 영역에서의 쓰임 |
|---|---|---|
| `rebalance_plans` | 회차 계획 헤더 | `account_index, decision_id(NULL 가능), pace, summary(JSON), created_at` — INSERT 전용 |
| `rebalance_plan_steps` | 종목별 회차 step | 위 4절 필드. `status` CHECK 없음 (코드상 candidate/blocked, 스키마 주석은 candidate/hold/blocked) |
| `decisions` | 의사결정 스냅샷(JSON) | 웹이 실제로 읽는 truth. plan 정보가 payload 안에 중복 저장 |
| `allocation_selections` | 확정 목표 + 예상 회차 | `expected_rebalance_rounds` 등 — selection 경로 입력/출력 |
| `target_allocations` | 3안 목표비중 | selection.select 의 구조적 plan 입력 |
| `account_snapshots`/`holdings` | 금액·현재비중 truth | drift 계산 입력 |
| `universe_instruments` | 목표비중·시세 | decision drift/qty 입력 |
| `investor_profile`/`portfolio_policies` | pace·현금밴드 | cycle_cap·precheck 입력 |

RDB = 금액/잔고/주문 truth. (Vector=근거검색, Graph=관계설명은 본 영역 **미구현/계획**, 스키마 주석상 v2 승격 전제.)

---

## 6. API / 함수

백엔드(Python):
- `decision.compute(account_index) -> dict` (`main_mission/portfolio_os/decision.py`) — drift·회차분할·리스크게이트·`decisions`+`rebalance_plans`/`rebalance_plan_steps` 저장. CLI: `python -m main_mission.portfolio_os.decision --account N`.
- `selection.select(account_index, proposal_id, variant)` (`selection.py`) — precheck/estimate 후 구조적 plan 저장. CLI: `--select P V`.
- `selection.precheck(rows, policy, stale)` / `selection.estimate(rows, snapshot, policy)` — 차단·예상 회차 산출.
- 핵심 상수: `PACE_CAP = {"slow":3.0, "normal":5.0, "fast":5.0}`, `cycle_cap = min(one_order_cap, pace_cap)`.

웹(Next.js):
- `GET /api/accounts/[id]/decision` → `getLatestDecision()` (DB `decisions.payload` 조회만).
- `POST /api/accounts/[id]/decision` → `execFile` 로 `decision.py` 실행(백엔드 계산 트리거).
- `getLatestDecision(index)` (`web/lib/server/portfolioDb.ts`) — payload JSON 파싱 반환.

> 주의: `rebalance_plans`/`rebalance_plan_steps` 를 직접 읽는 web API/함수는 **없음**. 웹은 `decisions.payload` 만 본다(plan 테이블은 현재 감사/이력 용도로만 적재).

---

## 7. UI 화면

- `web/app/accounts/[id]/portfolio/page.tsx` — "분할 리밸런싱 계획 (이번 회차)" 카드가 본 영역의 화면.
  - 종목별: 전체 조정(`total_adjust_pct`/`krw`), 이번 회차(`this_cycle_pct`/`krw`/`qty`), 남은 조정, 분할 회차·지정가.
  - `blocked` step 은 빨간 카드 + 차단사유(`block_reason`) 표시.
  - 상단에 확정 목표(`allocation_selections`)와 예상 분할 회차(`expected_rebalance_rounds`) 요약.
  - "다시 계산" = `POST decision` (다음 cycle 재평가 트리거).
- 모든 표시값은 DB 저장값(`decisions.payload`). mock/하드코딩 없음. 주문 실행 버튼은 본 화면에 없음(승인/주문은 별도 영역).

---

## 8. 상태 전이

step `status`:
```text
needs_adjust=true
   ├─ (매수 & cycle_qty==0)  → blocked   (qty=0 보류후보, 주문 후보에서 제외)
   └─ (그 외)                 → candidate (이번 회차 조정 후보)
needs_adjust=false → step 생성 안 함 (유지)
```
- 회차 진행: 매 `compute()` 는 항상 `round_no=1` 로 새 plan 을 생성한다. "다음 회차"는 동기화→재계산으로 **새 plan 행**이 쌓이는 방식(자동 round_no 증가/이월은 미구현).
- 스키마 주석의 `hold` 상태는 코드가 실제로 기록하지 않음(UI 문구 "이번 회차 보류"는 안내 텍스트일 뿐 status 변경 아님).

---

## 9. 예외 / 실패 케이스

| 케이스 | 처리(코드) |
|---|---|
| 스냅샷 없음 | `decision.compute`: `{"ok":False,"error":"잔고 스냅샷이 없습니다…"}` 반환, plan 미생성 |
| `captured_at` 파싱 실패 | `age_h=None`, stale 판정 skip |
| 시세 없음(`last_price` None/0) | `cycle_qty=0`, 매수면 blocked, reason="시세 없음 — 차단" |
| 이번 회차 금액 < 1주 | blocked, reason="이번 회차 금액(…) 1주(…) 미만 — qty=0 차단" |
| 스냅샷 stale(>24h) | 리스크 게이트 violation `stale_snapshot` (차단성 경고) |
| selection: proposal/variant 없음 | `{"ok":False,"error":"해당 proposal/variant 없음"}` |
| selection: 스냅샷 없음 | `{"ok":False,"error":"잔고 스냅샷 없음 — 동기화 필요"}` |
| 내부 예외 | CLI `main()` 이 `{"ok":False,"error":"내부 오류: …"}` 로 감싸 출력 |
| python 미발견(web) | `POST` 가 python/python3/py 순회 후 `{"ok":False,"error":"python 미발견"}` |

---

## 10. Hard-block 조건

본 영역은 **"잘못된 포트폴리오 이동 방지"** 게이트를 `decision.compute` 안에서 적용(`violations[]`):
- `cash_min_pct`: 목표 현금 < 10%(방어현금 부족)
- `single_name_max_pct`: 단일 목표비중 > 20%
- `min_order_qty`: qty=0 후보 존재(최소주문 미달)
- `stale_snapshot`: 스냅샷 24h 초과
- `sector_max_pct`: 섹터 목표 집중 > 30%
- `cash_band_min` / `cash_band_max`: 대전제 현금밴드 위반

selection.precheck 의 block: 현금밴드 하한 미만, 테마>섹터한도, 투자비중 합>100%, stale.

상위 불변 규칙(본 영역이 의존하는 다른 영역 hard-block):
- **시장가 매수 금지 / 지정가만** — 본 영역은 항상 `limit_price` 만 산출.
- **목표비중 없이 주문 후보 금지** — universe target/allocation 없으면 후보 0.
- **사람 승인 없이 주문 금지**, **live 주문은 `KIS_LIVE_CONFIRM` 없이 하드차단** — 주문/승인 영역에서 강제(본 영역은 후보 산출까지).

> 본 영역 게이트는 candidate 산출을 막거나 표시할 뿐, 주문 자체를 차단하는 최종 hard-block(승인/주문/live 가드)은 인접 영역 책임.

---

## 11. 로그 / 감사 기록

- 모든 결정은 snapshot/version/provenance 로 추적: `decisions.payload.provenance`(`account_snapshot_id`, `universe_active_count`, `risk_policy` 전체) + `snapshot_at` + `computed_at`.
- `rebalance_plans.summary`(JSON): candidates/blocked/cycle_cap_pct. selection 경로는 `from`, `selection_id` 기록.
- selection 은 `allocation_selections`(append-only, 이전 active→superseded) + `precheck_reasons`(JSON) + `diff`(이전 대비 변경).
- **누락(위험)**: `audit_logs` 테이블에 본 영역(plan 생성)이 행을 적재하지 **않음** — 회차 계획 생성/차단이 audit_logs 감사 흐름에 안 들어감. (주문/승인 영역만 audit_logs 사용 추정.)

---

## 12. 테스트 기준

- 본 영역 전용 자동화 테스트 **없음**. `tests/` 에는 `test_risk_gate.py`, `test_order_safety.py` 만 존재하며 `rebalance|decision|selection|plan` 키워드 매칭 0건.
- 검증 가능 항목(수동/CLI): `python -m …decision --account N` 출력의 `today_candidate_count`, `blocked_count`, `lines[].this_cycle_pct ≤ cycle_cap`, `split_rounds == ceil(total/cap)`, qty0→blocked.
- 권장 추가(미작성): cycle_cap=min(one_order_cap, pace_cap) 단위테스트, qty0→blocked 분기, split_rounds 경계값, stale violation.

---

## 13. 현재 구현 상태

구현됨:
- pace→cycle_cap 매핑(slow=3%, normal/fast=5%), 1주문 한도와 min 결합.
- 종목별 drift(5/25 band), 전체/이번회차/남은조정/분할회차 산출.
- qty=0 매수 → blocked(보류후보) + 사유.
- 포트폴리오 리스크 게이트(현금/단일/qty0/stale/섹터/현금밴드).
- `decisions` + `rebalance_plans` + `rebalance_plan_steps` 저장(두 진입점: decision/selection).
- selection 구조적 plan(테마 전개, decision_id=NULL).
- 웹 portfolio 화면이 `decisions.payload` 의 분할 계획·차단을 DB 조회로 표시.
- provenance/snapshot/version 기록.

부분/주의:
- 웹은 plan 테이블이 아니라 `decisions.payload`(중복 저장본)를 읽음 — plan 테이블은 사실상 미소비.
- `round_no` 항상 1 — 회차 누적/이월 없음(매 계산이 새 plan).

---

## 14. 미구현 / placeholder

- 지정가 "무릎(예측 진입)" 정밀화: `limit_price = last_price` 그대로(코드 주석 "무릎 정밀화는 후속").
- `hold` status: 스키마엔 있으나 코드가 기록 안 함(보류는 안내 텍스트뿐, 미체결→다음 cycle 자동 이월 로직 없음).
- 미체결(주문 후 부분/미체결) 추적과 plan step 의 체결 연동: 없음(`orders`/`fills` 와 plan 연결 미구현).
- `round_no` 자동 증가/잔여 조정 이월: 없음.
- 섹터/현금밴드 외 인버스·레버리지 비중 검사: selection.precheck 에 "도입 시 검사" 주석만(현 3안엔 없음).
- plan 생성 audit_logs 적재: 없음.
- 본 영역 자동 테스트: 없음.
- Vector(근거검색)/Graph(관계설명) 연동: 계획 단계(스키마 v2 주석).

---

## 15. 다음 개선 항목

1. plan step ↔ `orders`/`fills` 연결 + 미체결/부분체결 시 `hold` 상태와 다음 cycle 이월(`round_no` 증가).
2. 지정가 예측진입(무릎) 산출 로직 도입(일/주 단위 가격흐름 + lessons 재사용).
3. plan 생성/차단을 `audit_logs` 에 적재(추적성 일관화).
4. 웹이 `decisions.payload` 대신 정규화된 `rebalance_plan_steps` 를 직접 조회(중복 제거).
5. cycle_cap·qty0·split_rounds 단위 테스트 추가(검증 없는 DONE 금지 충족).
6. `rebalance_plan_steps.status` CHECK 제약 추가(candidate/hold/blocked 정합).

---

## 16. 다른 Agent와의 의존성

| 의존 대상 | 방향 | 내용 |
|---|---|---|
| 동기화(Sync) 영역 | 입력 | `account_snapshots`/`holdings` 최신성 — stale 게이트가 여기에 의존 |
| 목표비중/유니버스(소전제) | 입력 | `universe_instruments.target_weight_pct` 없으면 후보 0 |
| Allocation Selection(중·소전제 확정) | 입력 | `allocation_selections`/`target_allocations` + `expected_rebalance_rounds` |
| 프로필/정책(대전제) | 입력 | `investor_profile.rebalance_pace`, `portfolio_policies.policy`(cash_band/limits) → cycle_cap·precheck |
| 리스크 게이트(`risk/gate.py`) | 입력 | `RiskLimits`(one_order/cash_min/single_name) 한도 상수 |
| 승인/주문 영역 | 출력(하류) | 본 영역 candidate step → 사람 승인 → 지정가 주문, live 는 `KIS_LIVE_CONFIRM` 가드 |
| lessons(메모리 성장) | 양방향(계획) | 회고/근거 재사용 — 현재 plan 과 직접 연결 미구현 |
