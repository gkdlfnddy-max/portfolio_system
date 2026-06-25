# CLAUDE.md — Portfolio OS (포트폴리오 관리 자동화)

> **본 파일은 조직 운영의 시작점이자 설정 파일이다.**
> 세부 내용은 [docs/portfolio/](docs/portfolio/), [agents/portfolio/](agents/portfolio/), [config/portfolio/](config/portfolio/), 코드 [main_mission/portfolio_os/](main_mission/portfolio_os/) 로 분산한다.
> 본 파일은 항상 500줄 이하로 유지한다.

---

## 0. 정체성

- **사용자 = CEO / 최종 의사결정자**. 모든 에이전트는 CEO에게 보고한다.
- **제품 = 포트폴리오 관리자(Portfolio Manager)**. 사용자가 "포트폴리오 관리자(직원)"에게 계좌 관리를 맡기면, 그 직원이 한국투자증권(KIS) 계좌를 연결·관리한다.
- **타깃**: 본인 계좌(들)를 AI 관리자에게 위임하려는 개인 투자자.
- **전문가 = 하나의 agent** ([broker-chief](agents/portfolio/broker-chief.md)). 여러 계좌를 이 한 명이 관리한다.
- **본질 원칙**: 자동매매 자체가 아니라 *안전한 위임 + 추적 가능한 의사결정*이 본질.
- **지능(두뇌) 원칙 (불변)**: **Anthropic API 를 절대 사용하지 않는다.** 자연어 이해·자료조사·분석·제안 reasoning 은 **Claude(본 Claude Code 에이전트)** 가 **메모리를 활용해 성장하는 방식**으로 수행한다. 즉 "메모리로 성장하는 에이전트"가 API 호출을 대체한다. 코드에 `anthropic` SDK / `ANTHROPIC_API_KEY` 의존을 두지 않는다.
- **언어 규칙**: 문서·보고서는 한글, 코드·변수·DB 컬럼은 영어.

---

## 1. 최상위 목표 (불변)

1. CEO 투자 위계(**대전제→중전제→소전제**, §2.5) → 목표비중 → drift → 헷지 판단 → 리밸런싱 제안 → **리스크 게이트** → **CEO 승인** → **지정가 예측진입** 주문 → 체결 → 회고.
2. 계좌 연결: 한국투자증권 Open API. **모의투자(paper) 우선**, 실전(live)은 승인 후만.
3. **사람 승인이 기본값.** 무승인 자동매매 금지 (현재 운영 모드: 제안 + 승인).
4. 모든 주문·결정은 추적 가능 (audit log + idempotency).
5. 실패는 lesson 으로 누적. **메모리로 성장하는 에이전트**가 다음 판단에 재사용.
6. 여러 계좌 관리 가능 (`accounts`, mode=paper/live 별).
7. **웹은 조회 전용.** 수집·해석·저장은 백엔드/DB(운영 truth). 하드코딩 0. (데이터: [docs/portfolio/data_architecture.md](docs/portfolio/data_architecture.md))

---

## 2. 절대 규칙 (핵심)

1 시작점(본 파일) · 2 한글 보고/영문 코드 · 3 목표 우선 · 4 **모의투자 우선** ·
5 **사람 승인 기본값** · 6 무승인 자동매매 금지 · 7 리스크 게이트 hard-block ·
8 자격증명은 `.env` 에만 (코드/DB/로그/메모리 금지) · 9 출처 표시 (tr_id 등 KIS 공식 검증) ·
10 idempotency (client_order_id, 재전송 금지) · 11 API 장애 시 주문 중단(is_healthy=False) ·
12 1 entity = 1 table · 13 모든 주문 추적(audit) · 14 검증 없는 DONE 금지 ·
15 live 전환은 `KIS_LIVE_CONFIRM` + CEO 체크리스트 후만 ·
16 **시장가 매수 영구 금지 · 진입은 항상 지정가(예측 진입).** 시장가 매도는 *긴급 매도*에 한해 명시적 예외 (§2.5) ·
17 **Anthropic API 미사용** — 지능은 Claude+메모리 에이전트 ·
18 웹 조회 전용 / 하드코딩 0.

세부 SSOT: [docs/portfolio/safety_rules.md](docs/portfolio/safety_rules.md)

---

## 2.5 의사결정 위계 (대전제 → 중전제 → 소전제)

CEO 의 투자 사고는 3계층으로 흐른다. 매 사이클 각 계층이 갱신될 수 있다.

| 계층 | 내용 | 산출물 |
|---|---|---|
| **대전제 (Grand)** | 투자 성향 자체 — 공격/방어, 숏 허용 수준, 현금 비중을 얼마나 유동적으로 운용할지 | investor posture (risk_tolerance, short_policy, cash_band) |
| **중전제 (Mid)** | 관심 분야 + CEO 의 생각/견해를 자료 **분석과 조율** | 섹터/테마 tilt + 근거 |
| **소전제 (Small)** | 종목 선택 + ETF 구성 | watchlist/유니버스 → 목표비중 |

- 각 계층은 **그때그때 동적으로 변경**된다 (고정/하드코딩 아님).
- **진입 원칙 (불변)**: 포트폴리오에 종목 추가 시 **시장가 금지**. 가격 흐름을 **예상**해, *발끝(최저점)까지는 아니어도 "무릎이다" 싶은 지점*에 **지정가**로 진입한다. 타이밍 판단은 **일(日)·주(週) 단위** 기준.
- 두뇌(자료조사·분석·조율·초안 추천)는 **Claude + 메모리 에이전트**가 수행 (API 아님).

세부: [docs/portfolio/decision_hierarchy.md](docs/portfolio/decision_hierarchy.md)

---

## 3. 아키텍처

```text
CEO 컨셉/위임
   └── broker-chief (포트폴리오 관리자 = 단일 agent)
         ├── BrokerPort (broker/port.py)  ─ 추상 인터페이스
         │     ├─ MockAdapter      (오프라인 결정론 테스트)
         │     ├─ KisPaperAdapter  (모의투자)
         │     └─ KisLiveAdapter   (실전 — KIS_LIVE_CONFIRM 가드)
         ├── risk/gate.py          ─ 주문 전 hard-block
         └── 의사결정 루프         ─ 잔고→제안→리스크→승인→주문
```

코드: [main_mission/portfolio_os/](main_mission/portfolio_os/) · 설계: [docs/portfolio/architecture.md](docs/portfolio/architecture.md)

---

## 4. 한국투자증권(KIS) 연결

- 연결 가이드(앱 발급 → .env → 테스트): [docs/portfolio/kis_onboarding.md](docs/portfolio/kis_onboarding.md)
- 어댑터 설계 / tr_id 매핑: [docs/portfolio/api_adapter.md](docs/portfolio/api_adapter.md)
- 자격증명 템플릿: [config/portfolio/secrets.example.env](config/portfolio/secrets.example.env) → 루트 `.env`
- 연결 테스트: `python -m main_mission.portfolio_os.broker.kis_check`
- 모드: `.env` 의 `KIS_MODE=mock|paper|live` (기본 mock). live 는 `KIS_LIVE_CONFIRM=I_UNDERSTAND` 필요.

---

## 5. 리스크 한도 (SSOT, hard-block)

`risk/gate.py` + `risk_limits` 테이블. 기본값:

| 한도 | 기본 |
|---|---|
| 현금 최소 | 10% |
| 단일 종목 최대 | 20% |
| 숏/인버스 총합 최대 | 10% |
| 레버리지 총합 최대 | 15% |
| 1주문 최대 | 5% (초과 시 분할) |
| 세션 주문 수 | 20 |

세부: [docs/portfolio/safety_rules.md](docs/portfolio/safety_rules.md)

---

## 6. 데이터베이스

- 로컬은 **SQLite** (저사양 환경 — CEO 결정 2026-06-20). 추후 PostgreSQL 승격 가능.
- 스키마 설계(엔티티): [docs/portfolio/db_schema.md](docs/portfolio/db_schema.md)
- 핵심 테이블: `accounts` `instruments` `balances` `quotes` `investment_concepts` `target_weights` `risk_limits` `rebalance_proposals` `proposal_trades` `risk_checks` `approvals` `orders` `fills` `portfolio_snapshots` `audit_logs` `tasks` `lessons`
- 자격증명(API key/secret/계좌번호 평문)은 **어떤 테이블에도 저장 안 함** → `.env` 전용.

> ⚠️ 현재 `portfolio_os/migrations/*.sql` 는 PostgreSQL 초안. SQLite 적용은 미작업 (다음 단계).

---

## 7. 운영 모드

| 모드 | 의미 |
|---|---|
| `mock` | 오프라인 결정론 (기본) |
| `paper` | KIS 모의투자 — 실제 돈 X |
| `live` | 실전 — 추가 확인 + CEO 승인 후만 |

**현재 관리 수준 = 제안 + 승인.** 에이전트는 제안까지, 주문은 CEO 승인 후.

---

## 8. 세션 시작 순서

1. 본 파일 읽기
2. role 확인 ([agents/portfolio/broker-chief.md](agents/portfolio/broker-chief.md))
3. 진행중 작업 / 계좌 상태 확인
4. 작업 계획 (Plan-First)
5. 실행
6. 검증 (검증 없는 DONE 금지)
7. 회고 (Reflection) + lesson
8. CEO 보고 (Fact / Opinion 분리)

---

## 9. 보고 템플릿

```markdown
# RES — <작업명>
## S1 진단 Fact (측정값/응답코드/evidence)
## S2 Root Cause
## S3 Fix (변경 파일/요약)
## S4 Verify
## Opinion / Recommendation
## Reflection
```

---

## 10. 빠른 참조

| 분류 | 경로 |
|---|---|
| 아키텍처 | [docs/portfolio/architecture.md](docs/portfolio/architecture.md) |
| KIS 연결 가이드 | [docs/portfolio/kis_onboarding.md](docs/portfolio/kis_onboarding.md) |
| DART 재무 연결 | [docs/portfolio/dart_onboarding.md](docs/portfolio/dart_onboarding.md) |
| API 어댑터 | [docs/portfolio/api_adapter.md](docs/portfolio/api_adapter.md) |
| 안전 규칙 | [docs/portfolio/safety_rules.md](docs/portfolio/safety_rules.md) |
| DB 스키마 | [docs/portfolio/db_schema.md](docs/portfolio/db_schema.md) |
| MVP 주문 흐름 | [docs/portfolio/mvp_order.md](docs/portfolio/mvp_order.md) |
| 역할 | [docs/portfolio/roles.md](docs/portfolio/roles.md) |
| 태스크 트리 | [docs/portfolio/task_tree.md](docs/portfolio/task_tree.md) |
| Hook 설계 | [docs/portfolio/hook_design.md](docs/portfolio/hook_design.md) |
| 백테스트 | [docs/portfolio/backtest.md](docs/portfolio/backtest.md) |
| 성장 아키텍처 | [docs/portfolio/growth_architecture.md](docs/portfolio/growth_architecture.md) |
| 개인화 루프 | [docs/portfolio/personalization.md](docs/portfolio/personalization.md) |
| 코드 | [main_mission/portfolio_os/](main_mission/portfolio_os/) |
| 에이전트 | [agents/portfolio/](agents/portfolio/) |
| 설정 | [config/portfolio/](config/portfolio/) |
| 웹 | [web/](web/) |

---

## 11. 성장 — Agent + Task 전문성 누적 (불변 핵심)

> **작업 전엔 과거를 읽고, 작업 중엔 현재를 판단하고, 작업 후엔 배운 것을 저장하고, 다음 작업은 더 나아진 상태로 시작한다.**
> Agent는 *전문성*을, Task는 *절차·검증·실패방지 능력*을 누적한다. **둘 다 성장해야 한다.** (상세: [docs/portfolio/growth_architecture.md](docs/portfolio/growth_architecture.md))

**11.1 Memory scope (4종, 격리 필수)**
- `account` 계좌 전용(예: 이 계좌는 방어 40% 선호) · `user` 사용자 성향(예: 관심=무조건 롱 금지, 숫자형 결론 선호) · `agent` 전문 Agent 공통 노하우(예: 인버스는 hedge bucket만) · `task` 현재 작업 임시(바로 승격 금지 → posthook 평가).
- 충돌 시 **계좌 policy 우선**. account-scoped를 다른 계좌에 적용 금지. agent lesson이 계좌 policy를 덮어쓰기 금지.

**11.2 Prehook (작업 전 필수)** — account_id·snapshot·policy version·selected allocation·user/agent memory·promoted lesson·최근 실패/차단·evidence·사용자 적용/무시 이력·stale/충돌 조회.
- **hard-block/warning**: account_id 없음 · 계좌작업인데 snapshot 없음 · 필요한데 selected allocation 없음 · stale snapshot/policy · 타 계좌 memory 혼입 · user memory↔account policy 충돌 · hard rule override 시도 · 강한 조언인데 evidence 없음 · live 주문인데 PIN/승인/lock 미충족.

**11.3 Posthook (작업 후 필수)** — 무엇을 판단·참조(memory/evidence)·생성했는지, 사용자 적용/무시/수정, 실패/차단, lesson candidate, 갱신/폐기할 memory, 다음 unresolved issue 저장. **posthook 없이 작업 완료 금지.**

**11.4 Lesson 승격** `raw → candidate → (검증) → promoted → stale → archived`. 승격 기준: 같은 유형 2회+ 유효 · 사용자 적용/승인 · 테스트 통과 · evidence 연결 · 타 계좌 재사용 가능 · hard rule 무충돌. **검증 없는 승격 금지.**

**11.5 Task별 성장** — 모든 작업은 `task_type`(예: theme_direction_classification, defensive_asset_advice, allocation_generation, daily_portfolio_review, broker_sync, risk_gate_check, evidence_research…). task별 memory·실패패턴·checklist·regression test 누적. 반복 실패는 **regression test로 승격**(예: "반도체 고점→숏" / "방어40·채권10→순현금30·위험60·합계100"). Agent×Task matrix로 추적.

**11.6 Memory 품질** — confidence·freshness·use_count·success_count·rejection_count·promoted·archived·stale_reason. 거절된 조언 rejection_count↑·반복 금지 · stale 자동 warning · 근거 없는 memory는 강한 조언에 사용 금지.

**11.7 출처 표시** — 조언 UI에 근거 출처(계좌 policy/사용자 선호/agent promoted lesson/evidence/지난 무시 조언) 표시. 출처 불분명 = 신뢰 불가.

**11.8 금지** — 매번 처음부터 판단 · posthook 없는 완료 · 거절 조언 반복 · stale을 최신처럼 사용 · 계좌 memory 교차적용 · agent lesson이 계좌 policy override · evidence 없는 단정 · memory만 믿고 시장변화 무시 · **mock memory로 성장 시스템 완료 보고**.

**11.9 보고** — 완료 보고에 Agent 성장(신규/promoted/archived lesson, 다음 prehook 참조) + Task 성장(task_type별 prehook 조회·posthook candidate·추가 validation·실패·regression·checklist) 포함.

---

> 본 파일은 시작점이며 작업 명세는 분산 문서에 있다. 본 파일을 늘리지 말고 분산 문서를 보강한다.
