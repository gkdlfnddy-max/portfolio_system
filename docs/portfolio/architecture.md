# Portfolio OS — 아키텍처

> 한국투자증권(KIS) 기반 **승인형 포트폴리오 운영 시스템**. 자동매매 봇이 아님.
> 참조 영향: PlanActCash(계획/행동/현금 분리), Everything-Claude-Code(에이전트·hook·memory loop),
> 로보어드바이저 리밸런싱 구조(target weight → drift → trade list → guard → execute).

---

## 1. 설계 철학 (참고 자료 분석)

| 출처 | 차용한 패턴 | 우리 적용 |
|---|---|---|
| PlanActCash | Plan / Act / Cash 3단 분리. 현금을 1급 자산으로 취급 | `target_weights.cash_pct` 를 별도 1급 비중. Plan(제안) ≠ Act(주문) 분리 |
| Everything Claude Code | agent × hook × memory/lesson loop, 승인 게이트 | chief 역할 + prehook/posthook + lesson 승격 |
| 로보어드바이저 (Betterment/Wealthfront 류) | target allocation → drift band → tax/cost-aware rebalance | drift band(±x%) 초과만 거래, 비용/체결 고려 |
| 전통 OMS/EMS | order lifecycle(생성→검증→승인→전송→체결→정산), audit log | `orders` 상태머신 + 전 단계 감사로그 |
| Risk parity / guardrail | 사전 리스크 한도 위반 시 주문 차단 | `risk-chief` 게이트가 승인 전 hard-block |

**핵심 차별점**: 신호가 아니라 **CEO 의 투자 컨셉(자연어)** 이 입력. 시스템은 컨셉 → 목표 비중 변환기 + 리스크 가드 + 감사 가능한 실행기.

---

## 2. 상위 컴포넌트

```text
┌─────────────────────────────────────────────────────────────┐
│  Web Admin (Next.js)  — CEO 가 컨셉 입력 / 제안 검토 / 승인     │
│   /portfolio  /portfolio/proposals  /portfolio/approve        │
└───────────────┬─────────────────────────────────────────────┘
                │ REST (adapter pattern, mock→postgres)
┌───────────────▼─────────────────────────────────────────────┐
│  Decision Loop (Python)                                       │
│   strategy → research → portfolio → risk → (approval) → broker│
│   각 단계: prehook(컨텍스트 인출) → 실행 → posthook(lesson)     │
└───────┬───────────────────────────────┬─────────────────────┘
        │                               │
┌───────▼─────────┐             ┌───────▼──────────────────────┐
│ KIS API Adapter │             │ PostgreSQL  portfolio_os_db   │
│  paper | live   │             │  SSOT · 스냅샷 · 주문 · 감사   │
│  domestic | us  │             │  + memory/lesson (graph/vec)  │
└─────────────────┘             └──────────────────────────────┘
```

---

## 3. 의사결정 루프 상세 (state machine)

```text
[concept]──parse──►[target_weights]
                        │ (current balance 조회)
                        ▼
                   [drift_report]──compute──►[rebalance_proposal]
                        │                          │ (근거 첨부)
                        ▼                          ▼
                   [risk_check]◄──────────── guard(현금/숏/단일종목/손실)
                        │ pass                     │ fail→차단+사유
                        ▼
                   [pending_approval]──CEO 승인──►[order_candidates]
                        │ reject→폐기+lesson        │
                        ▼                          ▼
                   [submitted(paper|live)]──►[fill_check]──►[snapshot/metrics]
                        │ API err→ABORT             │
                        ▼                          ▼
                   [audit_log + lesson_run]   [reflection→memory]
```

상태는 `orders.status` + `rebalance_proposals.status` 로 DB 에 영속(§40 추적).

---

## 4. 모듈 경계

| 모듈 | 입력 | 출력 | 부작용 |
|---|---|---|---|
| `strategy` | 컨셉(자연어) | 자산배분 원칙 + 리스크 한도 | 없음(순수) |
| `research` | 컨셉/원칙 | 섹터·ETF·종목 후보 + 근거 출처 | 외부 조회 |
| `portfolio` | 원칙 + 후보 + 현재 잔고 | 목표 비중 + drift + 거래 리스트 | 없음(순수) |
| `risk` | 거래 리스트 + 잔고 | pass/fail + 위반 사유 | 없음(순수, hard-gate) |
| `broker` | 승인된 주문 후보 | 체결 결과 | KIS API 호출 |
| `data_ops` | 모든 단계 결과 | 스냅샷·로그·성과 | DB 쓰기 |

**순수 모듈(strategy/portfolio/risk)** 은 API 없이 단위 테스트 가능 → CI 에서 리스크 가드 회귀 테스트 필수.

---

## 5. 신뢰 경계 & 안전

- `broker` 만 KIS 자격증명 접근. 다른 모듈은 broker 인터페이스만 의존(§ adapter 분리).
- `paper` ↔ `live` 는 **환경변수 1개**(`KIS_MODE`)로 전환하되, `live` 진입은 별도 CEO 승인 + 체크리스트.
- 모든 주문에 `client_order_id`(idempotency key) → 중복 방지.
- API 장애/타임아웃/인증실패 → 루프 즉시 `ABORT`, 진행중 주문 없음 보장.

세부 연결: [roles.md](roles.md) · [task_tree.md](task_tree.md) · [db_schema.md](db_schema.md) · [api_adapter.md](api_adapter.md) · [hook_design.md](hook_design.md) · [safety_rules.md](safety_rules.md) · [mvp_order.md](mvp_order.md)

---

## 6. Wave 1 개선 (strategy + code-architect 자료조사, 2026-06-19)

### 6.1 컨셉 → 비중 = Anchor + Tilt 2단 변환 (plan_required)
출처: LLM-Enhanced Black-Litterman, Risk Parity vs MVO(기대수익 추정오차 22~56배 민감). 현재는 컨셉→비중을 한 번에 "초안"으로 산출(블랙박스, zero-base 단정 위험).
- **anchor_weights**: 컨셉 무관 중립 baseline(동일가중 or 역변동성/risk-parity or CEO 지정 기본배분).
- **tilt**: CEO 컨셉을 `{asset, direction, magnitude, confidence, source_quote}` 튜플로 파싱 → anchor에서 편향만. `final = anchor ⊕ tilt`. source_quote로 어느 문장에서 나온 tilt인지 추적(§9).
- **tilt 강도 상한**(CEO-GATE): `max_single_tilt_pct`(±15%p), `max_total_tilt_l1_pct`, confidence(low/med/high)→배율(0.3/0.6/1.0). 자연어 한 줄이 포트폴리오를 통째로 뒤집는 것 방지(MVO 과민성 방어).

### 6.2 보수/중립/공격 3안 동시 제안 (plan_required, 옵션)
출처: TradingAgents(risk-seeking/neutral/conservative 토론 후 단일 승인 게이트). 동일 컨셉에 tilt 강도 3단계 변형 + 각 안의 예상 현금/단일종목/리스크게이트 통과여부 라벨 → CEO 1개 선택. 선택안만 hard-gate 진입. 초기엔 CEO 명시 요청 시에만(토큰 3배).

### 6.3 결정 루프 = 명시적 전이표 + 순수 RiskDecision (plan_required)
출처: OMS FSM(허용 전이는 가능집합의 부분집합·가드 삽입), Functional Core/Imperative Shell, Hexagonal. §3 상태머신이 ASCII로만 존재 → 불법 전이(risk 미통과→submitted) 차단 단일지점 부재.
- `ALLOWED_TRANSITIONS: dict[State, set[State]]` 명시, `transition()`은 허용집합 밖이면 예외.
- risk 게이트 = **순수 함수** `evaluate(trades, balance, limits) -> RiskDecision{passed, violations, snapshot_inputs}`(broker/DB 미접근).
- 전이 가드: `risk_checked→pending_approval`는 RiskDecision.passed=True만, `approved→submitted`는 ceo_approval_id 존재만.
- **의존방향 가드 테스트**: 순수 모듈(strategy/portfolio/risk)이 broker/KIS/DB 드라이버 import 시 **테스트 실패**(코어가 adapter 모름 강제).
