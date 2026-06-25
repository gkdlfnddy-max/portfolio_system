# Portfolio OS — Task Tree (T1–T16, 투자 운영)

> 모든 작업은 `tasks` 테이블의 task 단위. `parent_task_id` 로 트리 구성.
> 각 task 는 **Input / Output / Success / Fallback** 4요소 + **owner chief** 를 가진다.
> 루트: `investment_session` (1 회 의사결정 사이클).

```text
investment_session
├─ T1  market_data_collect      시장 데이터 수집 (국/미/글로벌·환율·금리·ETF·뉴스)
├─ T2  portfolio_snapshot       보유 스냅샷 (국내/미국/현금/환율 반영 평가금액)
├─ T3  asset_classify           자산군 분류 (반도체/배터리/바이오/지수/현금/숏/레버리지/글로벌)
├─ T4  concept_interpret        CEO 컨셉 해석 → theme/confidence/horizon/risk_intent
├─ T5  market_chief_analysis    korea/us/global chief 근거 의견 제출
├─ T6  target_weights_build     anchor + tilt → 보수/기준/공격 3안
├─ T7  drift_compute            현재 vs 목표 차이 (5/25 rule)
├─ T8  rebalance_propose        매수/매도 후보·금액·이유·예상 현금비중
├─ T9  risk_gate                현금/숏/레버리지/단일종목/drawdown/블랙아웃/이벤트
├─ T10 order_candidate_build    승인 대기 주문안 생성 (실주문 아님)
├─ T11 ceo_approval             CEO 승인 (승인 전 어떤 주문도 실행 금지)
├─ T12 execution_paper_live     paper 우선 · live 는 별도 CEO-GATE 이후
├─ T13 fill_track               체결/부분/실패/재시도/in_doubt 추적
├─ T14 performance_record       수익률·drawdown·drift·turnover·비용 기록
├─ T15 lesson_run               판단/실패원인/좋은 패턴 lesson 저장
└─ T16 memory_update            반복 검증된 lesson 만 장기 knowhow 승격
```

---

## Task 명세

| ID | owner | Input | Output | Success | Fallback |
|---|---|---|---|---|---|
| **T1** market_data_collect | korea/us/global-market + research | 시장·환율·금리·ETF구성·뉴스 | 정규화 시장 스냅샷 (provenance) | 3개 시장 + 환율 수신 | 소스 실패→캐시+stale flag, 누락 시장 해당 tilt 보류 |
| **T2** portfolio_snapshot | broker → data-ops | account_id, mode | 보유 라인 + KRW 통합 평가액 | 전 종목 수량·통화·환산 확보 | KIS 실패→직전 스냅샷+flag, 3회 재시도 |
| **T3** asset_classify | portfolio-chief | 잔고+현재가 | 자산군별 비중 (cash/long/short/lev/sector) | 합 100% ±0.5% | 미분류→unknown 버킷+경고 |
| **T4** concept_interpret | portfolio-strategy-chief | 컨셉(자연어) | {theme, confidence, horizon, risk_intent, cash_target, hedge_intent} | 파싱 성공·tilt 추출 | 모호→CEO 명확화 질문 |
| **T5** market_chief_analysis | korea/us/global-market-chief | 컨셉 + T1 | 시장별 의견 + tilt 제안 (source/as_of/confidence) | 각 chief 근거 1+ | 의견 미수→해당 시장 anchor 유지+flag |
| **T6** target_weights_build | portfolio-strategy → portfolio-chief | anchor + 통합 tilt | 3안(보수/기준/공격) 목표비중 | 각 안 합 100%·cash≥최소·tilt 상한 준수 | tilt 과다→clamp+축소 사유 |
| **T7** drift_compute | portfolio-chief | current + target(3안) | 종목별 drift + 5/25 band 판정 | 모든 축 산출 | current 누락 축→target 유지+flag |
| **T8** rebalance_propose | portfolio-chief | drift + band + 비용 | 매수/매도 후보·금액·이유·예상 현금 | band 초과만·근거 1+ | 후보 0→"리밸런싱 불필요" |
| **T9** risk_gate | risk-chief | 후보 + 잔고 + 한도 | pass/fail + 위반 enum | 모든 한도 통과 or 명확 위반 | **fail→세션 중단·사유 보고** |
| **T10** order_candidate_build | portfolio-chief + broker(시뮬) | 통과 후보 | 승인 대기 주문안 (client_order_id, 분할) | 후보→주문안 변환·idempotency key | 분할 초과→경고 |
| **T11** ceo_approval | strategy-executive → CEO | 제안 + 리스크 결과 | approved/rejected/partial | CEO 명시 결정 기록 | 무응답→자동 폐기(주문 안 함) |
| **T12** execution_paper_live | broker-chief | 승인 주문안 + mode | 주문 접수 결과 | paper 접수 확인 / live 는 별도 GATE | API err→ABORT·미전송 보장 |
| **T13** fill_track | broker-chief (execution-analyst) | order id | 체결/부분/미체결/in_doubt | 모든 주문 최종상태 확정 | 미체결→재조회, in_doubt→재전송 금지·재조회 |
| **T14** performance_record | data-ops-chief | 체결 + 스냅샷 | 수익률·drawdown·drift·turnover·비용 | 스냅샷+지표 저장 | DB 실패→로컬 큐 재시도 |
| **T15** lesson_run | memory-lesson-chief | 세션 전체 | lesson_candidate (reflection 7질문) | 모든 경로에서 1+ 회고 | 없음(항상 실행) |
| **T16** memory_update | memory-lesson-chief | lesson + 성과 | knowhow 승격 후보 | recurrence≥3 AND confidence≥0.6 만 승격 | 미충족→reflection 유지 |

---

## 게이트 & 안전 의존성

- **T6 는 T1~T5 완료 후** (시장 의견·현재 상태 없이는 목표 무의미).
- **T9(risk_gate) 통과 없이는 T10 주문안 생성 금지.** fail 시 T10~T13 스킵.
- **T11(CEO 승인) 없이는 T12 실행 절대 금지** (실주문 안전).
- **T12 live 는 paper 검증 + 별도 CEO-GATE 이후** (`KIS_MODE=live`).
- **T15 는 성공/실패/중단 모든 경로에서 실행** (lesson loop 보장).
- 각 task 전후 prehook/posthook 적용: [hook_design.md](hook_design.md).

연결: [roles.md](roles.md)(owner) · [db_schema.md](db_schema.md)(tasks) · [safety_rules.md](safety_rules.md)(T9 한도)
