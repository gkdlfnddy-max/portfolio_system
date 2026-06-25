# Daily Portfolio Review — 운영 모델 (실시간 trading bot 아님)

> CEO 원칙: 이 시스템은 초단위 매매 봇이 아니다. **정기 점검 → 판단 보조 → 예약성 지정가 계획 → 승인 → 회고**.
> 기준 질문: "지금 사고팔까?"가 아니라 **"오늘 이 계좌가 목표 포트폴리오에 더 안전하게 가까워지려면 무엇을 / 또는 아무것도 안 해야 하는가?"**

---

## 1. 핵심: "관망"도 정상 결과
Daily Review 결과가 항상 주문으로 이어질 필요 없음. `action_decision ∈ {buy, sell, rebalance, hold, watch}`.
주문은 아래 4경우에만 후보 생성: **A 최초 구성 · B 리밸런싱 필요 · C 현금/방어 비중 조절 · D 위험 한도 초과**. 그 외엔 만들지 않는다.

## 2. 이미 강제되고 있는 금지사항(§10) — 기존 코드 매핑
| 금지 | 현재 강제 위치 |
|---|---|
| 시장가 매수 금지 | `order_service.submit_order` (test_market_buy_blocked) + CLAUDE.md §16 |
| 승인 없이 주문 금지 | 운영모드 = 제안+승인 (decision은 후보까지만) |
| selected allocation 없이 주문 금지 | `decision.compute` hard-block (`no_selection`) + `prehooks` decision 게이트 |
| stale snapshot 주문 금지 | `decision.compute` `stale_snapshot` 차단 (STALE_HOURS=24) |
| drift 작을 때 불필요 주문 금지 | `decision` `needs_adjust = abs(drift) > band` (밴드 내면 미생성) |
| 추격/강제 주문 금지 | 지정가(예측 진입) + `hold_note`(불리하면 회차 보류) |
| 관망을 실패로 보지 않음 | `hold`/`watch`가 정식 action_decision |
| live 주문 하드락 | `KIS_LIVE_CONFIRM` + LiveModeConfirm (PIN과 별개로 유지) |

→ "not a trading bot" 철학은 이미 아키텍처에 내장. 본 작업은 그 위에 **Daily Review 산출물 + 예약성 계획 + UI 문구**를 얹는다.

## 3. Daily Workflow
1. 지정 시각 `sync` → account_snapshot 저장
2. price_snapshot 저장
3. portfolio drift 계산 (`decision.compute` — selected allocation 기준)
4. 시장 상황 요약 (market_context: 금리/환율/채권/지수/뉴스 — evidence 기반, Claude+메모리)
5. 리스크 노출 점검 (risk gate: 현금밴드/섹터/국가/통화/인버스/레버리지/국채)
6. **Daily Portfolio Review 생성** (아래 §6 스키마)
7. 주문 필요 여부 판단 (§1 A~D만)
8. 필요시 예약성 지정가 주문 후보 (분할 3~5회, pace=slow면 일/주 단위)
9. 사용자 승인 대기 — 미승인 시 미실행
10. 다음 cycle에서 체결/미체결/보류 재평가

## 4. Rebalancing Workflow
전략/목표비중 변경 → selected allocation 확인 → 현재비중 비교 → 전체 조정량 → 분할 계획 → 지정가/예약 조건 → 리스크 게이트 → 승인 → paper/live 조건 → 체결/미체결/보류 이력 저장. (대부분 `selection`+`decision`에 존재; 신규는 `scheduled_order_plans` 영속화.)

## 5. Research Workflow
관심분야/질문 → evidence/lessons 조회(prehook 공통+계좌 memory) → 외부 조사 → 중전제 조언 → policy draft 반영 가능 판단 → 사람 저장 시 policy version → allocation 재계산. (consult/theme/view Agent + growth memory.)

## 6. 신규 DB (다음 단계 — store/schema.sql + migrations/pg)
- `daily_portfolio_reviews(account_index, review_date, account_snapshot_id, selected_allocation_id, drift_score, market_context_id, action_decision, action_reason, no_trade_reason, scheduled_order_plan_id, risk_check_id, approved_by_user, created_at)` — UNIQUE(account_index, review_date)
- `market_context_snapshots(id, captured_at, rates_json, fx_json, indices_json, news_json, summary)`
- `scheduled_order_plans(id, account_index, decision_id, status, valid_until, created_at)` + `scheduled_order_steps(plan_id, ticker, direction, total_pct, total_krw, cycle_pct, cycle_krw, remaining_pct, round_no, total_rounds, limit_price, valid_until, on_unfilled, hold_condition, risk_check_id, approved, status)`
- `order_intent_history`, `review_evidence_links`, `review_lesson_candidates`
PG: 동일 테이블을 `portfolio.` schema 아래 `300_daily_review.sql` 로 추가(JSONB market context, UNIQUE(account_id, review_date)).

## 7. 주문 후보 표시 필수 항목
전체 조정 필요액 · 이번 회차액 · 남은액 · 분할 회차 · 예약/지정가 · 유효기간 · 미체결 처리 · 보류 조건 · 리스크 게이트 결과 · 승인 상태. (대부분 `decision.lines`에 이미 존재: total_adjust_pct/krw, this_cycle, remaining_pct, split_rounds, limit_price, hold_note.)

## 8. UI 문구 (trading → portfolio review)
"주문 실행"→"오늘 조정 후보" · "매수 추천"→"목표비중 접근 조정안" · "실시간 매매"→"예약성 지정가 계획" · "수익률 예측"→"비중 조정 근거". 화면: 오늘의 Review / 주문 or 보류 / 보류 이유 / 예약 후보 / 분할 계획 / 미체결 상태 / 다음 점검일 / 시장 요약 / 참고 근거.

## 9. DONE 기준
Daily Review 없이 주문 후보 생성 금지 · 후보는 selected allocation+drift에서 출발 · 승인 전 미실행 · 시장가 매수 금지 유지 · 미체결/보류는 다음 cycle 이월 · "관망" 정상 결과 · live 하드락 유지.

## 10. 종합 블록 (synthesis) — 영향 분석 + 조정 후보 + ETF 겹침
> 코드: `portfolio_impact.py`(영향+후보), `etf_analysis.py`(ETF 구성·겹침), `daily_review._synthesis_block`(종합).
> **전부 읽기 전용·broker-neutral. 자동주문 0 · 자동 policy 변경 0 · 승인 전 allocation 반영 0.** `auto_order_created:false`.

**입력(DB 읽기 전용):** `user_views`(사용자 견해) · `evidence_items`(자료) · `decline_scan`(하락 6축·confidence) · `holdings`/`universe_instruments` · `etf_constituents`.

**산식(portfolio_impact):**
- 종목별로 (하락신호 + evidence 편향 + user_views 부호)를 묶어 **데이터 신뢰도**(하락 6축 overall_confidence ∧ evidence confidence 평균) 산출. `< 0.3` = 단정 금지(약한 후보/관망), `≥ 0.6` = 비교적 강한 후보(여전히 사람 승인).
- **위험/기회 구분**: 하락 강함·부정 자료·단기 부정 견해 → 위험; 긍정 자료·장기 긍정 견해·하락 약함 → 기회. (한 종목이 둘 다 가질 수 있음.)
- **견해 vs 데이터 일치/충돌 명시**(`alignment ∈ aligned|conflict|mixed|none`). 장기 긍정 ↔ 단기 과열/하락 = `conflict`.
- **mixed_swing**(충돌, 보유 중): 단타 아님 — 노출관리(net/gross). 후보 = `long 유지 + 분할매수(staged_buy) + hedge 검토`. 매도/매수 단정 금지.

**조정 후보 종류(전부 후보, 주문 아님):** 관망(observe) · 현금밴드 상향(cash_band_raise) · 위험자산 축소(reduce_risk_assets) · 헤지 검토(consider_hedge, 인버스 한도 내) · 신규매수 보류/속도 완화(slow_new_buy/staged_buy) · 리밸런싱 속도 완화(slow_rebalance) · 테마 노출 축소 · mixed_swing 구조.

**ETF 분석(etf_analysis):** 구성·상위비중·섹터/국가 노출 + **보유 ETF 간 겹침**(공통 종목·min_overlap_weight 합산, 예 반도체ETF+AI ETF → NVIDIA/TSMC/Samsung). 겹침 ≥ 20% = 집중 위험 표기. 데이터 없으면 정직하게 `data_connected:false`(가짜 구성 생성 X).

**종합 블록 구성:** 견해 vs 데이터(일치/충돌) · 포트폴리오 영향 요약 · 종목/테마 영향 · 오늘의 조정 후보 · ETF 겹침/집중 플래그 · **오늘 하지 않을 이유**(신뢰도 낮음/근거 없음/충돌 → 단정 금지) · **추가 확인 필요**(자료 부족·일봉 부족·ETF 미연동).

## 10b. 통합 강화 — 매일 보는 핵심 화면 (Track D)
> 코드: `daily_review._user_views_block`·`_macro_block`·`_perspective_block`·`_evidence_summary_block`·`_conservative_candidates_block`·`_today_questions_block`·`_integration_payload`/`_integration_confidence`.
> **전부 읽기 전용·broker-neutral. 자동주문 0 · 자동 policy 0 · 사용자 승인 흐름 유지.** `auto_order_created:false`, `requires_user_approval:true`.

관점별 최선·하락 징후·거시를 한 화면에 녹인다. Daily Review return/payload 에 아래 섹션이 **모든 분기(watch/hold/rebalance) 공통**으로 들어간다(없는 데이터는 정직하게 "미연동/데이터 없음").

| 섹션 | 키 | 소스(읽기 전용) | 없을 때(graceful) |
|---|---|---|---|
| 오늘의 6축 상태 | `six_axis` | `_decline_block`(종목 composite) + `_macro_block` + `_supply_demand_block` 재사용(재스캔 0) | 데이터 없는 축은 `data_available:false`로 제외(가짜 점수 0) |
| 오늘의 사용자 관점 요약 | `user_views` | `user_views.list_views` + `investor_objective.get` | 관점/목적 미입력 정직 표기(가정 금지) |
| 오늘의 주요 거시 변화 | `macro` | `macro_connect.macro_snapshot` + `macro_to_portfolio`(병렬 B) | `connected:false` "거시 미연동"(거짓 수치 0) |
| 오늘의 관점별 A/B/C 후보 | `perspective_variants` | `perspective_variants.generate(save_draft=False)` | `connected:false`·후보 미생성 |
| 오늘의 자료 요약 | `evidence_summary` | `evidence_summary.evidence_for_account`(병렬 E) | `connected:false` "자료 요약 미연동" |
| 오늘의 보수적 전환 후보 | `conservative_candidates` | `decline` proposal + `synthesis` 방어성 후보 | 빈 목록 "보수 전환 후보 없음" |
| 오늘의 하락 징후 | `decline` | `decline_scan`(6축, §10) | 일봉 부족 시 `not_enough_data` |
| 오늘 사용자에게 물어볼 질문 | `today_questions` | 위 섹션 종합 | 특이 신호 없으면 "유지/검토" 선택지 질문 |

**graceful import:** 통합 소스(`user_views`·`investor_objective`·`perspective_variants`·`macro_connect`·`evidence_summary`)는 `daily_review` 상단에서 `try/except` 로 import 한다 — 병렬 B/E 모듈이 늦게 도착해도 daily_review 가 깨지지 않는다(부재 시 해당 섹션만 "미연동" 정직 표기).

**오늘 물어볼 질문(불변):** 단정이 아니라 **선택지 질문**이다. 각 질문 = `{topic, question, options[]}`.
예) "반도체 단기 과열 — 신규매수를 늦출까요, 헤지를 확대할까요, 현 노출을 유지할까요?" → options=["신규매수 보류","헤지 확대 검토","현 노출 유지"]. 질문 트리거: 하락 보수전환 후보 · 견해 vs 데이터 충돌 · mixed_swing 과열(expand) · 거시 변화 · 관점/목적 미입력 · 관점별 후보 선택. 질문 자체는 주문/정책 변경이 아니며 사람의 선택·승인을 구한다.

**confidence(정직 — 단정 회피):** `integration_confidence ∈ {low, medium, high}`. 스냅샷/선택안 없음·거시 미연동·관점 미입력·자료 없음·일봉 부족이 겹칠수록 penalty↑ → level↓. 낮을수록 "단정 금지·관망/추가확인" 톤.

**no-trade(불변):** review-level `no_trade_reason` 에 더해 섹션 차원 `no_trade_reasons[]`(포트폴리오 기준 미확정 · synthesis 의 not_doing_today · "모든 조정은 후보, 승인 전 변경 0")를 함께 싣는다. `auto_order_created:false`, `broker_neutral:true`.

테스트: `tests/test_daily_review_integration.py`(신규 SQLITE_PATH 핀 + per-test `setup_function` re-pin). 모든 섹션 존재·macro graceful·관점 draft 미저장·질문 선택지·confidence 낮춤·자동주문 0 검증.

## 11. policy draft 승인 흐름 (자동 적용 차단)
영향 조정 후보 → `decline_policy_draft.build_impact_draft/save_impact_draft`(source=`portfolio_impact`)로 **미승인 advice_items(status=open)** 저장. `requires_user_approval:true, auto_applied:false`.
흐름: **영향분석 → 조정 후보 → draft(open) → (사람 승인=accepted) → policy version → allocation 재계산.**
`policy.compile_policy` 는 `status='accepted'` 만 읽으므로 **draft 저장해도 정책/비중 불변**(자동 적용 차단 — `test_impact_draft_does_not_change_compile_policy` 가 증거). 거절(rejected) draft 는 재저장 강요 안 함(`test_impact_draft_respects_rejected`).
