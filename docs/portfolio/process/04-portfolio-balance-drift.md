# Portfolio Balance / Drift Agent 시스템 프로세스 정리

> 본 문서는 추측이 아니라 실제 코드(`main_mission/portfolio_os/decision.py`, `store/schema.sql`, `web/app/api/accounts/[id]/decision/route.ts`, `web/app/accounts/[id]/portfolio/page.tsx`, `web/lib/server/portfolioDb.ts`)를 근거로 작성한다.
> 공통 원칙: 단기 trading 아님(포트폴리오 비중관리 + 분할 리밸런싱). 웹은 DB truth 조회만. KIS 호출은 백엔드 sync/job 만. 운영화면 mock/하드코딩 금지. 목표비중 없이 주문후보 금지. 사람 승인 없이 주문 금지. live 주문은 `KIS_LIVE_CONFIRM` 없이 하드차단. 모든 decision 은 snapshot/version/provenance 기록. 한글 문서 / 영문 코드.

---

## 1. 목적

계좌 전체 구조를 읽어 **현재비중 vs 목표비중의 drift** 를 계산하고, 5/25 band 트리거로 조정이 필요한 종목을 가려, **한 번에 다 맞추지 않고** 며칠·일주일에 걸쳐 **분할(1주문 한도 이내)** 로 목표에 접근하는 회차 단위 계획을 만든다. 이 영역은 *주문 실행*이 아니라 **"무엇을 얼마나 조정해야 하는가"의 측정·판정·분할 산정**까지를 담당한다.

핵심 책임(CEO 지정 범위):
- 현재비중 계산 (`current_pct`)
- 목표비중 비교 (`target_pct`)
- drift 계산 (`drift = current − target`)
- band 판정 (5/25 룰, `band`)
- 전체 조정필요금액 (`total_adjust_pct` / `total_adjust_krw`)
- 오늘(이번 회차) 조정금액 (`this_cycle_pct` / `this_cycle_krw` / `this_cycle_qty`)
- 남은 조정금액 (`remaining_pct`, `split_rounds`)
- portfolio balance 화면 표시기준 (`web/app/accounts/[id]/portfolio/page.tsx`)

근거: `decision.py:1-15`(모듈 docstring), `decision.py:37-232`(`compute`).

---

## 2. 전체 흐름

```text
[selected allocation 확정]  selection.py (allocation_selections.status='active')
        │  (확정 목표 = 주문후보 생성의 전제. 미확정 시 화면 경고)
        ▼
account_snapshots / holdings / universe_instruments / investor_profile  (DB truth, sync job 적재)
        │
        ▼  POST /api/accounts/[id]/decision
   execFile python -m main_mission.portfolio_os.decision --account <id>
        │
        ▼  decision.compute(account_index)
   ┌─ 현재비중   cur[ticker] = market_value / total * 100        (decision.py:81)
   ├─ 목표비중   tgt[ticker] = universe.target_weight_pct        (decision.py:82)
   ├─ drift     drift = current − target                        (decision.py:102)
   ├─ band      min(one_order_cap, target*0.25)  (5/25)         (decision.py:103)
   ├─ needs     |drift|>band and |drift|>0.1                    (decision.py:104)
   ├─ 전체조정   total_adjust_pct/krw = |drift|                  (decision.py:112-113)
   ├─ 이번회차   this_cycle = min(total, cycle_cap)              (decision.py:114-115)
   ├─ 남은조정   remaining_pct, split_rounds                     (decision.py:116-117)
   ├─ qty       cycle_qty = int(cycle_krw / last_price)         (decision.py:120)
   ├─ 섹터노출   sector_exposure (목표 기준 합산)                 (decision.py:90-93)
   ├─ 현금밴드   cash_band (대전제 cash_min/max 연결)            (decision.py:159-169)
   └─ 리스크게이트 violations[] (현금/단일/qty0/stale/섹터/밴드) (decision.py:139-169)
        │
        ▼  INSERT decisions + rebalance_plans + rebalance_plan_steps   (decision.py:201-228)
        ▼  stdout JSON 마지막 줄 반환 → route.ts 가 그대로 응답
        │
        ▼  GET /api/accounts/[id]/decision → getLatestDecision (DB 조회만)
        ▼  portfolio/page.tsx 렌더 (구조 우선 → drift 표 → 분할계획 → 리스크게이트)
```

근거: 흐름은 `schema.sql:1-3`(KIS→sync→DB→Web 조회), `route.ts:23-41`(POST=계산), `route.ts:16-20`(GET=조회), `portfolioDb.ts:150-161`(`getLatestDecision`).

---

## 3. 입력

전부 DB(운영 truth)에서 읽음. 웹/화면에서 직접 KIS 호출하거나 하드코딩하지 않음.

| 입력 | 출처 테이블 / 컬럼 | 코드 위치 |
|---|---|---|
| 최신 잔고 스냅샷 | `account_snapshots`: `id, cash_krw, total_value_krw, captured_at` (account_index 별 최신 1행) | `decision.py:47-50` |
| 보유종목 시가 | `holdings`: `ticker, market_value` (해당 `snapshot_id`) | `decision.py:64-66` |
| 목표비중·시세·섹터 | `universe_instruments`: `ticker, name, asset_class, target_weight_pct, last_price` (`is_active=1`) | `decision.py:67-70` |
| 대전제 운용변수 | `investor_profile`: `cash_min_pct, cash_max_pct, risk_tolerance, rebalance_pace, individual_cap_pct` | `decision.py:71-75` |
| 리스크 한도 기본값 | `RiskLimits()` (코드 상수: 1주문 5%, 현금 10%, 단일 20%) | `decision.py:38-41` |

추가 입력 상수(코드 하드코딩 정책값, 추후 config 예정): `SECTOR_MAX_PCT=30.0`, `STALE_HOURS=24.0`, pace_cap `{slow:3, normal:5, fast:5}` (`decision.py:42-43, 78`).

전제 입력(연결): `allocation_selections.status='active'` (selection.py 가 확정한 목표 포트폴리오). 화면이 `GET /api/accounts/[id]/allocation` 으로 별도 조회해 "확정 목표 배너"로 표시(`page.tsx:69-75, 124-150`).

---

## 4. 출력

`compute()` 가 반환하는 단일 JSON(계좌별 1 decision). DB 저장 + stdout.

계좌 단위 필드: `total_value_krw, cash_current_pct, cash_target_pct, target_sum_pct, sector_exposure[], today_candidate_count, blocked_count, cash_band{min,max}, risk{passed,violations[]}, snapshot_at, snapshot_stale, note, provenance{...}, computed_at, decision_id, plan_id` (`decision.py:171-228`).

종목 단위 `lines[]` 필드(`decision.py:105-134`):
- `ticker, sector, current_pct, target_pct, drift, band, needs_adjust`
- 조정 필요 시: `direction(매도/매수), total_adjust_pct, total_adjust_krw, this_cycle_pct, this_cycle_krw, this_cycle_qty, remaining_pct, split_rounds, limit_price, blocked, block_reason, hold_note`

provenance(`decision.py:184-194`): `account_snapshot_id`, `universe_active_count`, `risk_policy{single_name_max_pct, sector_max_pct, cash_min_pct, one_order_cap_pct, cycle_cap_pct, rebalance_pace, individual_cap_pct, cash_band}`.

---

## 5. DB 테이블

| 테이블 | 이 영역에서의 역할 | R/W |
|---|---|---|
| `account_snapshots` | 금액 truth(총평가·현금·신선도) | R (`decision.py:47`) |
| `holdings` | 보유종목 시가 → 현재비중 분자 | R (`decision.py:64`) |
| `universe_instruments` | 목표비중·시세·섹터(소전제) | R (`decision.py:67`) |
| `investor_profile` | 대전제(현금밴드·pace·개별상한) | R (`decision.py:71`) |
| `decisions` | drift/비중/리스크 스냅샷(payload=JSON) | **W** (`decision.py:201-205`) |
| `rebalance_plans` | decision 1회 = plan 1개(회차 요약) | **W** (`decision.py:209-215`) |
| `rebalance_plan_steps` | 조정 종목별 회차 step(total/cycle/remaining/qty/limit/status/reason) | **W** (`decision.py:216-227`) |
| `allocation_selections` | 확정 목표(전제). 이 영역은 미수정·읽기만(화면 배너) | R (web, `portfolioDb.ts:129-133`) |

`decisions` 스키마: `id, account_index, payload(JSON), created_at` (`schema.sql:154-160`). `rebalance_plans` / `rebalance_plan_steps` (`schema.sql:239-261`).

이 영역은 `orders` / `fills` / `audit_logs` 를 직접 쓰지 않음 — 주문·감사는 하류(주문 실행) Agent 담당.

---

## 6. API / 함수

백엔드(Python):
- `decision.compute(account_index: int) -> dict` — 핵심 계산·DB 저장(`decision.py:37`).
- `decision.main()` — CLI 진입점, `--account` 인자, 결과 JSON 을 stdout 마지막 줄에 출력(`decision.py:235-244`). 실행: `python -m main_mission.portfolio_os.decision --account 1`.
- 내부 헬퍼: `_now()`, `_r1(x)`(소수2자리 반올림) (`decision.py:29-34`).

웹 API(`route.ts`):
- `POST /api/accounts/[id]/decision` — `execFile` 로 python 모듈 실행(`python|python3|py` 순차 시도, `cwd=루트`, timeout 25s), stdout 마지막 줄 JSON 파싱 후 `out.ok`에 따라 200/400 반환(`route.ts:23-41`).
- `GET /api/accounts/[id]/decision` — `getLatestDecision(id)` 로 DB 최신 1행 조회만(`route.ts:16-20`).

웹 DB 조회(`portfolioDb.ts`):
- `getLatestDecision(index)` — `decisions` 최신 payload 파싱 + `saved_at` 부착(`portfolioDb.ts:150-161`).
- `getCurrentSelection(index)` — 확정 목표 배너용(`portfolioDb.ts:129-133`).

---

## 7. UI 화면

`web/app/accounts/[id]/portfolio/page.tsx` — "포트폴리오 비중 관리" (구조 우선 레이아웃, 주문은 마지막).

- 헤더: "며칠·일주일에 걸쳐 분할로 좁혀갑니다. 한 번에 다 맞추지 않습니다." + [목표비중](universe), [다시 계산](POST) 버튼(`page.tsx:104-119`).
- **확정 목표 배너**: `getCurrentSelection` 결과. variant(보수/기준/공격)·`expected_drift_pct`·`expected_rebalance_rounds`·`precheck_status`·확정 비중 칩. 미확정이면 경고 + "목표 포트폴리오 확정" 유도(`page.tsx:123-150`).
- **① 포트폴리오 구조 카드 4개**: 총 평가액 / 현금 현재→목표(밴드 병기) / 투자 목표비중 합 / 오늘 조정 후보(`page.tsx:164-169`).
- **섹터 노출(목표 기준)** 막대 + 현금(`page.tsx:171-190`).
- **② 현재 vs 목표 + drift 표**: 종목/업종/현재/목표/drift(±색)/band(±)/판정(조정 필요·유지) (`page.tsx:192-223`).
- **③ 분할 리밸런싱 계획(이번 회차)**: 종목별 전체조정 / 이번 회차(%·원·주) / 남은 조정 / 분할·지정가, 차단 사유 표시(`page.tsx:225-257`).
- **④ 리스크 게이트**: passed → "승인 단계로 진행 가능", 아니면 violation 나열(`page.tsx:259-274`).
- 푸터: stale 경고 + "전부 DB 저장값 · 주문은 승인 후 · 실전 차단" + provenance(snapshot#/유니버스/섹터한도) (`page.tsx:276-280`).

표시기준: 전부 DB 저장값만 렌더(`cache:"no-store"`, `getLatestDecision`). 화면에서 계산·하드코딩 없음.

---

## 8. 상태 전이

이 영역 자체는 가벼운 상태만 가진다(주문 상태머신은 하류 `orders` 담당).

`rebalance_plan_steps.status` (이 영역이 부여):
- `candidate` — 밴드 초과·차단 아님(이번 회차 조정 후보)
- `blocked` — qty=0 등으로 차단 (`decision.py:219`)
- (`hold` 는 스키마 enum 에 존재하나 `decision.py` 는 candidate/blocked 만 기록; hold_note 는 텍스트로만 안내)

종목 line 판정 흐름: `유지(needs_adjust=false)` → `조정 필요(needs)` → (`매수`이고 qty=0 이면) `차단(blocked)` / 아니면 `후보(candidate)`.

decision 전체 게이트: `risk.passed=true` → 화면상 "승인 단계로 진행 가능" / `false` → "차단 — 이번 회차 보류". 단 이는 **표시 상태**이며, 실제 주문 차단·승인 전이는 하류 Agent 가 수행.

---

## 9. 예외 / 실패 케이스

- **스냅샷 없음**: `{"ok": false, "error": "잔고 스냅샷이 없습니다 — 계좌 화면에서 먼저 동기화하세요."}` (`decision.py:51-52`). route.ts 가 400 반환.
- **스냅샷 stale**(>24h): `snapshot_stale=true`, violation `stale_snapshot` 추가(`decision.py:151-153`), 화면 푸터 경고.
- **시세 없음/0**: `last_price` 없으면 `cycle_qty=0`, 매수 후보면 차단 "시세 없음 — 차단"(`decision.py:120-127`).
- **qty=0(최소주문 미달)**: 이번 회차 금액이 1주 가격 미만이면 차단 + `qty0_blocked` 카운트 → violation `min_order_qty`(`decision.py:123-127, 148-150`).
- **total=0**: 비중 계산 시 0 나눗셈 방어(`if total else 0`) (`decision.py:81, 86`).
- **captured_at 파싱 실패**: `age_h=None`, stale 판정 생략(`decision.py:58-62`).
- **python 미발견**: route.ts 가 `python|python3|py` 순차 시도, 전부 ENOENT 면 `{ok:false,"python 미발견"}` 500(`route.ts:27-40`).
- **계산 예외**: `main()` 이 `{"ok": false, "error": "내부 오류: ..."}` 로 감싸 반환(`decision.py:241-242`).
- **확정 목표 미존재**: 화면 경고 배너(`page.tsx:145-150`). 단 현재 `compute()` 는 `allocation_selections` 미확정이어도 universe 목표비중만으로 계산을 수행함 → §14 위험 참조.

---

## 10. Hard-block 조건

이 영역의 게이트는 **"잘못된 포트폴리오 이동 방지"** 용 violation 수집이며 `risk.passed` 로 표면화한다(`decision.py:139-169`, `risk` 출력 `decision.py:195`). 위반 항목:

| limit | 조건 | 근거 |
|---|---|---|
| `cash_min_pct` | 목표 현금 < 10%(RiskLimits) | `decision.py:141-143` |
| `single_name_max_pct` | 최대 목표비중 > 20% | `decision.py:144-147` |
| `min_order_qty` | qty=0 후보 존재 | `decision.py:148-150` |
| `stale_snapshot` | 스냅샷 age > 24h | `decision.py:151-153` |
| `sector_max_pct` | 섹터 목표합 > 30% | `decision.py:155-158` |
| `cash_band_min` / `cash_band_max` | 목표 현금이 대전제 밴드 밖 | `decision.py:159-169` |

종목 수준 hard-block: **매수 진입 qty=0 차단**(`blocked=true`) — 시장가 금지·지정가 원칙과 연결(`decision.py:119-127`, `limit_price=last_price`).

주의: 본 영역은 **주문을 발행하지 않으므로** live `KIS_LIVE_CONFIRM` 하드차단·시장가 매수 영구금지 등의 *실주문* hard-block 은 하류(주문 실행/어댑터) Agent 책임. 본 영역은 진입가를 항상 `limit_price`(지정가)로만 산출해 그 원칙의 상류를 지킨다.

---

## 11. 로그 / 감사 기록

- **decision 스냅샷 자체가 감사 기록**: `decisions.payload` 에 입력·계산·provenance·risk 전체를 JSON 으로 append(`decision.py:201-205`). `provenance.account_snapshot_id` 로 어떤 잔고에서 계산했는지 추적, `computed_at`/`snapshot_at` 시각 보존.
- **회차 계획 원장**: `rebalance_plans` + `rebalance_plan_steps`(차단 사유 `reason` 포함)로 "무엇을 얼마나 왜 조정 후보로 올렸나" 보존(`decision.py:209-228`).
- **append-only**: 매 계산이 새 `decisions` 행(최신 id 조회), 과거 미삭제 → 시계열 비교 가능.
- 단, 본 영역은 `audit_logs` 테이블(`schema.sql:85-99`)에는 **기록하지 않음**(주문·승인·차단 감사는 하류). 이는 의도된 분리이자 §14 보완 후보.

---

## 12. 테스트 기준

**현재 decision/drift 전용 자동 테스트 없음**(미구현). `main_mission/portfolio_os/tests/` 에는 `test_risk_gate.py`, `test_order_safety.py` 만 존재하며 `decision|drift|rebalance|compute` 매칭 없음(검색 결과 0건).

검증해야 할 기준(작성 권장):
- drift = current − target, band = min(one_order_cap, target*0.25), needs 판정 경계(`|drift|>band and >0.1`).
- 분할: total_adjust=|drift|, this_cycle=min(total, cycle_cap), remaining=total−cycle, split_rounds=ceil(total/cycle_cap).
- qty0 매수 차단·`min_order_qty` violation, stale·현금밴드·섹터·단일 violation.
- pace(slow=3%)에 따른 cycle_cap 축소, total/price 0 방어.

---

## 13. 현재 구현 상태

구현됨(코드 확인):
- 현재비중/목표비중/drift/5·25 band/needs 판정 (`decision.py:81-104`).
- 전체 조정금액·이번 회차·남은 조정·split_rounds·지정가·qty 산정 (`decision.py:112-134`).
- 섹터 노출 합산, 현금 현재→목표, 목표비중 합 (`decision.py:86-93`).
- 리스크 게이트 6종(현금/단일/qty0/stale/섹터/현금밴드) (`decision.py:139-169`).
- provenance(snapshot_id, universe_count, risk_policy) (`decision.py:184-194`).
- pace 기반 cycle_cap (slow 더 잘게) (`decision.py:76-79`).
- `decisions` + `rebalance_plans/steps` DB 저장 (`decision.py:201-228`).
- POST(계산)/GET(조회) API, python 다중 폴백 (`route.ts`).
- DB-only 조회 화면(구조 우선 + 확정목표 배너 + drift 표 + 분할계획 + 리스크게이트) (`page.tsx`).
- selection.py 와의 연결: 화면이 `allocation_selections.status='active'` 를 배너로 표시, 미확정 시 경고 (`page.tsx:124-150`).

---

## 14. 미구현 / placeholder

- **drift/decision 자동 테스트 없음** (§12). 회귀 안전망 부재.
- **selected allocation 강제 미연동**: `compute()` 는 `allocation_selections` 를 읽지 않고 `universe_instruments.target_weight_pct` 만 사용(`decision.py:67-70, 82`). 화면은 확정 목표를 배너로 보여주나, *계산 자체가 확정 목표를 게이트로 삼지 않음* → "목표비중 없이 주문후보 금지" 원칙이 universe 입력에만 의존. 확정 목표(섹터/테마)와 universe(종목)의 매핑/정합 검증은 미구현(주석상 "종목 단위 실행은 소전제 매핑으로 연결" placeholder, `page.tsx:142`).
- **`limit_price` = 단순 `last_price`**: "무릎(지정가 정밀화)"은 후속(`decision.py:119` 주석 "지정가 기준 (무릎 정밀화는 후속)").
- **`hold` 상태 미사용**: 스키마 enum 에 `hold` 있으나 `compute` 는 candidate/blocked 만 기록(hold 는 텍스트 안내 `hold_note` 로만).
- **`SECTOR_MAX_PCT=30`, `STALE_HOURS=24`, pace_cap 하드코딩**: config/policy 로 이관 예정(`decision.py:42-43` 주석 "추후 config").
- **audit_logs 미기록**: 이 영역 계산은 `decisions` 에만 남고 `audit_logs` 에는 미기록.
- **현재비중 섹터 노출 없음**: `sector_exposure` 는 *목표 기준* 만(`decision.py:89-93`). 현재 보유의 섹터 쏠림은 미산출.
- **multi-round 추적 없음**: 항상 `round_no=1` 로만 기록(`decision.py:226`). 누적 진행률(이미 몇 회 집행했는지)을 실제 체결과 대조하는 로직 미구현.

---

## 15. 다음 개선 항목

1. `decision.compute` 단위 테스트 작성(MockAdapter/고정 스냅샷으로 결정론 검증) — §12 기준.
2. `compute()` 가 `allocation_selections.status='active'` 를 직접 읽어 **확정 목표가 없으면 주문후보(needs/candidate) 생성을 막거나** 확정 목표 ↔ universe 정합을 검증(원칙 "목표비중 없이 주문후보 금지" 강제화).
3. `limit_price` 무릎 산정(일·주 단위 가격 흐름 반영, quotes 활용)으로 placeholder 대체.
4. 정책 상수(SECTOR_MAX_PCT/STALE_HOURS/pace_cap)를 `portfolio_policies` / `risk_limits` 로 이관해 하드코딩 제거.
5. 현재 보유 기준 섹터 노출 추가(목표 대비 현재 쏠림 비교).
6. 회차 진행 추적: 실제 fills 와 대조해 `remaining_pct` 를 누적 갱신, `round_no` 증가.
7. decision 계산 이벤트를 `audit_logs` 에 INFO 로 남겨 감사 일원화.

---

## 16. 다른 Agent 와의 의존성

| 상대 Agent / 모듈 | 관계 | 방향 |
|---|---|---|
| **Selection / Target Allocation** (`selection.py`, `allocation_selections`) | 확정 목표(보수/기준/공격)를 전제로 제공. precheck/estimate(`expected_drift_pct`, `expected_rebalance_rounds`)가 본 영역 표시의 상류. 본 영역은 화면 배너로만 소비(계산 강제연동은 §14 미구현). | 상류 → 본 영역 |
| **Profile / Policy** (`investor_profile`, `portfolio_policies`) | 대전제(현금밴드·pace·개별상한)를 입력으로 제공(`decision.py:71-75`). pace→cycle_cap. | 상류 → 본 영역 |
| **Universe** (`universe_instruments`) | 소전제 목표비중·시세·섹터 입력(`decision.py:67-70`). 이게 없으면 drift 산출 불가. | 상류 → 본 영역 |
| **Sync / Balance Job** (`account_snapshots`, `holdings`, KIS 어댑터) | 금액 truth 적재. KIS 호출은 전적으로 이 백엔드 job. 본 영역은 DB 만 읽음. | 상류 → 본 영역 |
| **Risk Gate** (`risk/gate.py` `RiskLimits`) | 한도 기본값 소비(`decision.py:38-41`). 실주문 hard-block 의 SSOT 는 gate 측. | 공유 의존 |
| **주문 실행 / 승인 Agent** (`orders`, `fills`, `audit_logs`, 어댑터) | 본 영역의 `rebalance_plan_steps`(candidate/지정가/qty) 를 입력으로 받아 승인→지정가 주문→체결. live `KIS_LIVE_CONFIRM`·시장가 금지·idempotency 는 하류 책임. | 본 영역 → 하류 |
