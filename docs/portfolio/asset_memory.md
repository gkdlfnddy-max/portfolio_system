# 자산별 메모리 + lesson run + pre-hook (성장형 시스템)

> CEO 지시 2+3 구현. "작업 전엔 과거를 읽고, 후엔 배운 것을 저장한다."
> 코드: `asset_memory.py` · `lesson_runs.py` · `lesson_outcome.py` · `memory_prehook.py` (모두 `main_mission/portfolio_os/`).
> 지능 = Claude + 메모리 (**Anthropic API 미사용** — SDK import 없음). 자동 주문/policy 변경 0.

---

## 1. asset_memory — 자산/시장별 누적 지식

종목/ETF/섹터/테마/거시/이벤트/정책에 대한 지식이 시간이 지날수록 축적된다.

- **공통 vs 사용자 관점 분리(불변)**
  - `account_index/user_id = NULL` → **공통 자산지식**(시장 공통 노하우).
  - `account_index = N` → **그 계좌 사용자 관점**(격리). `search()` 가 scope filter 로 격리해
    교차 덮어쓰기/혼입을 막는다. (`account_index="__shared__"` 기본 = 공통만 조회.)
- **출처 없는 강한 기억 차단**: `confidence >= STRONG_CONFIDENCE(0.6)` 인데 evidence/source(+date+freshness)
  가 없으면 `WEAK_CONFIDENCE_CAP(0.35)` 로 자동 강등(`downgraded=True`). 조회 시 `weak=True` 로
  표시되어 **강한 조언에 사용 금지**.
- **stale 표시**: `stale=1` 또는 `stale_at` 경과 시 stale. `include_stale=False` 로 제외 가능.
- **검색키**: ticker(exact) · sector · theme · bucket + freshness/confidence filter + scope filter.
- **growth_report(scope)**: 공통/사용자 카운트, memory_type 분포, stale·weak 수, evidence 연결 수,
  reliability 평균, 최신 항목, 사용자 view 변경 — 보고만(자동 적용 없음).

API: `record(...)` · `get(id)` · `search(...)` · `mark_used` · `mark_stale` · `growth_report(scope_type, scope_key)`.

## 2. lesson_runs — 판단 → 반응 → reliability

판단 시점 → 시장반응/사용자반응 → 신뢰도 갱신 → 다음 pre-hook 재사용 루프.

- `record_lesson(...)`: 분석 시점 판단(신호·suggested_action·근거 memory/evidence ids) 기록.
  `hit_or_miss=pending`. 주문/적용 없음.
- `record_outcome(lesson_id, window, actual)`: analysis 이후 N거래일 시장반응
  (`return_pct`/`drawdown_pct`)을 넣어 **hit/miss/false_alarm** 판정.
  - 방어계열(shift_conservative/reduce/sell/hedge/short): 낙폭 발생 → hit, 아니면 false_alarm.
  - 진입계열(buy/add/enter/long): 상승 → hit, 아니면 miss.
  - hold/미상: 큰 변동(>=8%) 있으면 miss, 아니면 hit.
- **reliability(베이지안)**: Beta(α,β), prior α0=β0=1(=0.5). hit→α+1, miss/false_alarm→β+1.
  `reliability = α/(α+β)`. scope 단위(자산/시장 공통)로 누적 — **계좌 교차적용 아님**.

API: `record_lesson(...)` · `record_outcome(...)` · `reliability(scope_type, scope_key)` · `recent_runs(...)`.

### 2.1 lesson_outcome — 시장반응 **자동** 기록(성장 루프 완성)

코드: `lesson_outcome.py`. `record_outcome` 를 손으로 부르지 않고, `price_history` 일봉으로
**분석 이후 시장 반응을 자동 계산**해 reliability 를 갱신한다. **평가·기록만 — 자동주문/policy 0.**

- `evaluate_pending(window_days=[5,20,60], scope_key=None)`: `hit_or_miss=pending` 인
  price-scope(stock/etf) lesson_run 을 모아, 각 종목의 분석일 이후 일봉으로 시장반응을
  계산 → `record_outcome` 호출. CLI: `python -m main_mission.portfolio_os.lesson_outcome --evaluate`.
- `evaluate_lesson(lesson_id, windows=...)`: 단일 평가. `--lesson-id N`.

**lookahead bias 차단 (불변, 단일 관문 `future_bars`)**
- 결과 평가는 **분석일(`created_at`) 이후 거래일(`trade_date > analysis_date`)** 일봉만 사용.
  분석일 당일/이전 일봉은 **절대 사용 금지** (baseline 으로도 안 씀).
- baseline = 분석일 이후 **첫** 거래일 종가(= 분석 직후 진입 기준가).
- `return_pct` = (window 번째 종가 / baseline − 1)×100, `drawdown_pct` = baseline 대비 구간 최저가 낙폭(≤0).
- 미래 일봉이 window 개 미만 → **pending 유지**(가짜 성장 금지, 정직). 더 적재된 뒤 재평가.
- 테스트로 못박음: 분석일 이전에 극단 노이즈 일봉을 넣어도 결과 불변(`test_lesson_outcome.py`).

**시장반응 기록**: `actual_outcome`(JSON)에 `return_5d/20d/60d`·`max_drawdown`·`baseline_close`·
`analysis_date`·각 window dict 저장. 판정은 **확정 가능한 가장 긴 window** 의 수익률/낙폭으로.
`reliability_before/after` 도 행에 기록.

**예시(005930, 분석일 2026-05-01, 제안=방어/진입속도 조절)**: baseline 232,500 →
5d +22.8% / 20d +55.0% (상승) → 방어 제안 틀림 → **false_alarm** → reliability 0.5 → 0.3333.
(분석일 2026-06-22 처럼 이후 일봉이 아직 없으면 → pending 유지.)

## 3. memory_prehook — 판단 전 컨텍스트

`prehook_context(account, scope_type, scope_key, ...)` — 최신/장기/시장반응/사용자반응/stale 를
**분리**해 retrieval + 판단용 요약을 만든다. 우선순위:

1. `selected_allocation` (계좌 선택 배분)
2. `user_views` (사용자 견해 — 1급 입력, 계좌 격리)
3. `asset_memory` (공통 `asset_memory_shared` + 그 계좌 `asset_memory_user`)
4. 최신 `evidence`
5. 최신 가격/수급/거시 (`latest_price`/`latest_flows`/`latest_macro`)
6. 과거 `lesson_runs` + `reliability`
7. 장기 thesis (`long_thesis` / `long_views`)
8. `stale` (표시만)

- **상충 정보도 포함**: 장기 긍정 thesis ↔ 단기 수급/가격 악화를 `conflicts` 로 노출(숨기지 않음).
  요약 `cautions` 에 stale·출처없음·상충 주의문구.
- **출처 없는 강한 기억**은 `weak_unsourced` 로 분리.
- **자동 적용 금지**: 출력은 후보·confidence·주의문구·질문까지만(`advisory_only=True, applied=False`).
  실제 결정/주문은 사람 승인 경로.

## 4. 005930(삼성전자) 실증

prehook 이 수급(외국인/기관 순매도) + 가격(하락) + 거시 + 공통 자산지식 + **사용자 반도체 장기 긍정
관점**(계좌 격리)을 결합하고, 장기 긍정 ↔ 단기 악화를 `conflicts` 로 노출. lesson outcome(buy +5%)
→ reliability 0.5 → 0.667 갱신. (CLI 재현은 각 모듈 `--help` 참조.)

## 5. 테스트

`tests/test_asset_memory.py` · `tests/test_memory_prehook.py` — 공통↔사용자 분리·계좌 격리·stale 표시·
출처없는 강한기억 차단·freshness/confidence filter·growth_report·reliability(hit/miss/false_alarm)·
005930 실증·상충 저장·Anthropic 미사용·자동주문/policy 0. (전체 스위트 green.)
