# Graph / Exposure Agent 시스템 프로세스 정리

> 이 문서는 **실제 코드 기반**으로 작성되었다. 추측 금지 — 함수명·테이블명·필드명·경로는 코드에 있는 그대로다.
> 미구현 항목은 정직하게 "미구현/계획"으로 표기한다.
> 공통 원칙: 단기 trading 아님(포트폴리오 비중관리 + 분할 리밸런싱) · 웹은 DB truth 조회만 · KIS 호출은 백엔드 sync/job만 · 운영화면 mock/하드코딩 금지 · 목표비중 없이 주문후보 금지 · 사람 승인 없이 주문 금지 · live 주문은 `KIS_LIVE_CONFIRM` 없이 하드차단 · 모든 decision 은 snapshot/version/provenance 기록 · **RDB=금액/잔고/주문 truth, Vector=근거검색, Graph=관계설명** · 한글 문서 / 영문 코드.

근거 코드:
- `main_mission/portfolio_os/decision.py` — **현재 유일하게 동작하는 노출 계산**: `sector_exposure`(목표비중 기준 섹터 쏠림 집계) + 섹터 집중 한도(`SECTOR_MAX_PCT`) 위반 산출.
- `main_mission/portfolio_os/policy.py` — 정책에 국가/통화/인버스/레버리지 한도값이 **선언만** 됨(검사 로직 없음).
- `main_mission/portfolio_os/store/schema.sql` — 정수 PK + `scope`/`ref` 패턴(`lessons`, `lesson_candidates`, `evidence_documents`, `target_allocations`) = **Graph 이식 전제**.
- `docs/portfolio/portfolio_os_design_v2.md` §3 — "Graph Index (관계)" 는 **PG 승격 시** 도입하는 계획. 현재 코드/DB에 Graph 엔진·노드/엣지 테이블 없음.

> ⚠️ **이 영역은 대부분 미구현/계획이다.** 현재 코드에는 그래프 DB, 노드/엣지 테이블, ETF→구성종목 매핑, 국가/통화 노출 집계, 테마→종목 관계, decision→evidence→risk→order 관계 그래프가 **존재하지 않는다.** 실제로 동작하는 것은 `decision.py` 의 **섹터 노출 1차원 집계**뿐이다.

---

## 1. 목적

Graph / Exposure Agent 는 **"계좌가 어떤 차원으로 얼마나 쏠려 있는가(노출)"** 와 **"왜 이 결정이 내려졌는가(관계 설명)"** 를 다루는 영역이다. CEO 가 지정한 범위:

- 계좌→종목→섹터→국가→통화 노출 관계
- ETF→구성종목→중복노출(중첩 보유)
- 테마→종목 관계
- decision→evidence→risk→order 관계(의사결정 추적 그래프)
- Graph Index 승격 계획
- 리스크 설명(violation 의 "왜"를 관계로 설명) 활용

설계 철학(`design_v2.md`): **RDB = 금액/잔고/주문 truth, Vector = 근거 의미검색, Graph = 관계 설명.** Graph 는 truth 를 보관하지 않고 *관계를 설명*하는 보조 인덱스다.

**현 단계의 실질 목적은 매우 좁다.** 동작하는 것은 `decision.compute` 안에서 목표비중을 섹터별로 합산해(`sector_exposure`) 한 차원(섹터)의 쏠림을 잡아내고, 30% 초과 시 리스크 위반을 만드는 것뿐이다. 나머지(국가/통화/ETF중복/테마/관계그래프)는 모두 **다음 증분**이다.

---

## 2. 전체 흐름

```text
[현재 구현 — 섹터 노출 1차원만]
universe_instruments(asset_class=섹터, target_weight_pct)
        │  decision.compute(account_index)
        ▼
sector_exposure[sec] += target_weight_pct      ← 종목→섹터 집계(딕셔너리)
        │
        ▼
for sec, sp in sector_exposure:
   if sp > SECTOR_MAX_PCT(30) → violations.append(sector_max_pct)  ← 리스크 설명
        │
        ▼
decisions.payload.sector_exposure = [{sector, target_pct}, ...]  (정렬)
        │
        ▼
web/app/accounts/[id]/portfolio/page.tsx  — 섹터 노출 조회 표시(DB truth)


[계획 — Graph Index 승격, 미구현]
RDB(정수 PK + scope/ref) ──승격──> Graph nodes/edges (PG)
  계좌-종목 · 종목-섹터 · ETF-구성종목 · 종목-뉴스/공시
  · decision-evidence-risk-order · 테마-종목-섹터 · 국가/통화
        │
        ▼
관계 질의(why 설명) — 리스크 위반의 인과/연결 설명
```

핵심: 현재는 **종목→섹터** 단일 매핑(`sector_of[ticker] = asset_class`)을 이용한 단일차원 집계가 전부다. "그래프"라 부를 자료구조(노드/엣지/경로질의)는 코드에 없다.

---

## 3. 입력

**현재 구현(섹터 노출):**

| 입력 | 출처 | 필드 |
|---|---|---|
| 관심종목 + 목표비중 | `universe_instruments` (SQLite) | `ticker`, `name`, `asset_class`(=섹터 대용), `target_weight_pct`, `last_price`, `is_active` |
| 섹터 한도 | `decision.py` 하드코딩 상수 | `SECTOR_MAX_PCT = 30.0` |
| (보유 비중) | `holdings`(최신 `account_snapshots`) | `ticker`, `market_value` — 현재 노출 표시는 **목표비중 기준**, 보유기준 노출은 미사용 |

> 주의: `asset_class` 컬럼이 사실상 "섹터"로 재사용된다(`sector_of = {ticker: asset_class or "기타"}`). 별도 `sector`/`country`/`currency`/`theme` 분류 테이블은 없다. `universe_instruments.currency` 컬럼은 존재하나(기본 'KRW') 노출 집계에 **사용되지 않는다.**

**계획 입력(미구현):** ETF→구성종목 매핑 소스, 종목별 국가/통화/테마 분류, evidence 문서(`evidence_documents`), decision-evidence 링크(`decision_evidence_links`).

---

## 4. 출력

**현재 구현:**

- `decision.compute` 결과의 `result["sector_exposure"]` = `[{"sector": s, "target_pct": p}, ...]` (비중 내림차순 정렬). `decisions.payload`(JSON) 안에 저장.
- 섹터 쏠림 위반: `result["risk"]["violations"]` 에 `{"limit": "sector_max_pct", "observed": sp, "threshold": 30.0, "detail": "섹터 '<sec>' 목표 집중 과도"}`.
- `provenance.risk_policy.sector_max_pct = 30.0` (어떤 한도로 판정했는지 추적).

**계획 출력(미구현):** 국가/통화/ETF중복 노출표, 테마별 노출, decision→evidence→risk→order 관계 경로, 리스크 위반의 그래프 기반 "왜" 설명.

---

## 5. DB 테이블

**그래프/노출 전용 테이블은 현재 없다.** 노출 결과는 `decisions.payload`(JSON) 안에 임베드된다.

| 테이블 | 이 영역 관련 |
|---|---|
| `universe_instruments` | `asset_class`(섹터 대용), `currency`, `is_leveraged`, `is_inverse`, `target_weight_pct` — 노출 분류의 원천 |
| `decisions` | `payload` JSON 에 `sector_exposure` + `sector_max_pct` violation 포함 |

**Graph 이식 전제(scope/ref 패턴) — 이미 스키마에 깔려 있음:**

| 테이블 | scope/ref 컬럼 | Graph 승격 시 의미 |
|---|---|---|
| `lessons` | `scope`(market/economy/sector/instrument/premise/decision), `ref`, `account_index` | 노드 라벨/키 후보 |
| `lesson_candidates` | `scope`(+risk), `ref`, `evidence_ref` | 관찰→근거 엣지 후보 |
| `evidence_documents` | `scope`(news/disclosure/report/...), `ref`, `affected_theme`, `affected_asset` | 근거→테마/종목 엣지 후보 |
| `decision_evidence_links` | `decision_id`, `evidence_id`, `weight_change` | **decision↔evidence 엣지(이미 테이블 존재, 데이터/쓰기 경로 미구현)** |
| `target_allocations` | `proposal_id`, `variant`, `kind`(cash/anchor/tilt), `ref`(테마/섹터/자산군) | 테마/섹터 tilt 노드 |

> 모든 키가 정수 PK + scope/ref 패턴(스키마 주석 라인 208-211, design_v2 §3 "승격 전제 설계 원칙")이라 PG+Graph 로 **무손실 이관**하도록 설계되어 있다. 단, **노드/엣지 물리 테이블(graph_nodes/graph_edges), 그래프 질의 함수는 미생성.**

---

## 6. API / 함수

| 함수/식 | 위치 | 역할 | 상태 |
|---|---|---|---|
| `sector_exposure` 집계 루프 | `decision.py:90-93` | `for t,g in tgt: sector_exposure[sector_of[t]] += g` | **구현** |
| 섹터 한도 위반 | `decision.py:155-158` | `if sp > SECTOR_MAX_PCT: violations.append(...)` | **구현** |
| `sector_of` 매핑 | `decision.py:84` | `{ticker: asset_class or "기타"}` (종목→섹터 1:1) | **구현** |
| `compile_policy` limits | `policy.py:60-70` | `country_max_pct=70`, `currency_max_pct=80`, `inverse_max_pct`, `leverage_max_pct` 선언 | **선언만**(검사 로직 없음) |
| ETF→구성종목 / 중복노출 | — | 없음 | **미구현** |
| 국가/통화 노출 집계 | — | 없음 | **미구현** |
| 테마→종목 그래프 | — | 없음 | **미구현** |
| decision→evidence→risk→order 관계질의 | — | 없음(`decision_evidence_links` 테이블만 존재, 쓰기 경로 없음) | **미구현** |
| Graph 노드/엣지 CRUD·경로질의 | — | 없음 | **미구현** |

CLI: 별도 진입점 없음. 섹터 노출은 `python -m main_mission.portfolio_os.decision --account N` 실행 시 부수적으로 산출된다.

---

## 7. UI 화면

웹은 **DB truth 조회만** (노출/그래프 로직 미포함):

- `web/app/accounts/[id]/portfolio/page.tsx` — `decisions.payload.sector_exposure` 를 읽어 섹터별 목표비중을 표시, `sector_max_pct` violation 을 리스크 목록에 노출. `provenance.risk_policy.sector_max_pct` 근거 표시.
- 데이터 접근: `web/lib/server/portfolioDb.ts`(node:sqlite, 읽기 전용). mock/하드코딩 없음(원칙 준수).

**전용 "노출/그래프 화면"은 없다.** 국가/통화/ETF중복/관계 시각화 화면 미구현 → "해당 없음(미구현)".

---

## 8. 상태 전이

이 영역에는 자체 상태머신이 **없다.** 섹터 노출은 매 `decision.compute` 호출 시 새로 계산되어 `decisions`(append-only)에 적재될 뿐, 상태 전이를 갖지 않는다.

> 해당 없음(이유: 노출 계산은 무상태 집계, 그래프 엔진 미구현). 관련 상태는 상위 의사결정/주문 영역(`orders.status`, `allocation_selections.status`)에 있다.

---

## 9. 예외 / 실패 케이스

**현재 구현 범위:**
- **스냅샷 없음**: `decision.compute` → `{"ok": False, "error": "잔고 스냅샷이 없습니다..."}`. 노출도 계산 안 됨(목표비중/잔고 없이 후보 금지 원칙).
- **섹터 미분류**: `asset_class` 가 NULL/빈값이면 `"기타"` 섹터로 합산(쏠림이 "기타"에 뭉칠 수 있음 — §15 위험).
- **목표비중 합 비정상**: 섹터 합은 목표비중 그대로 더하므로, `target_weight_pct` 입력 오류가 그대로 노출에 반영(검증 없음).

**계획 범위(미구현)이라 현재 발생 안 함:** ETF 구성종목 데이터 누락, 국가/통화 매핑 누락, 그래프 사이클/끊긴 엣지, evidence 링크 무결성 오류.

---

## 10. Hard-block 조건

**이 영역에서 실제로 hard-block 을 만드는 것은 섹터 집중 한도 하나뿐이다:**

- `decision.compute`: 섹터 노출 `sp > SECTOR_MAX_PCT(30.0)` → `violations` 에 `sector_max_pct` 추가 → `risk.passed=False`(주문 후보 차단에 기여).

**선언만 되고 hard-block 미적용(미구현):**
- 국가 한도(`country_max_pct=70`), 통화 한도(`currency_max_pct=80`): `policy.py` 에 값만 존재, **노출 집계·위반 검사 없음.**
- ETF 중복(중첩 보유) 한도: 정의·검사 모두 없음.
- 인버스/레버리지 총합: `gate.check_trades`(순수 함수)에는 한도가 있으나 `decision` 노출 경로에서는 **미집계·미적용**(§14, 06-risk-gate.md 참조).

---

## 11. 로그 / 감사 기록

- **provenance**: `decisions.payload.provenance.risk_policy.sector_max_pct` 에 적용된 섹터 한도값이 스냅샷으로 기록 → 어떤 한도로 쏠림을 판정했는지 추적 가능.
- **섹터 노출 자체**: `decisions.payload.sector_exposure`(append-only)로 매 결정마다 보존 → 노출 추이 이력이 됨.
- **전용 audit_logs 액션 없음**: 섹터 위반은 decision 의 violation 으로만 남고, 별도 `audit_logs` 액션(예: `exposure_block`)은 없다.
- **계획**: `lessons`/`lesson_candidates`/`evidence_documents` 의 scope/ref 가 관계 근거 기록의 substrate(현재는 노출 영역에서 쓰지 않음).

---

## 12. 테스트 기준

**이 영역(노출/그래프) 전용 테스트는 없다.**

- `tests/test_risk_gate.py` 는 `gate.check_trades` 의 6 hard 한도(현금/단일/숏/레버/1주문/세션)만 커버하며, **섹터 노출(`sector_exposure`)·`sector_max_pct` 위반은 테스트되지 않는다.**
- `tests/test_order_safety.py` 는 주문 안전(별도 영역).
- 섹터 노출 집계 정확성, 국가/통화/ETF중복(미구현)에 대한 회귀 테스트 미작성.

> 테스트 기준(향후): 종목→섹터 합산 정확성, 30% 경계, "기타" 폴백, 노출표 정렬.

---

## 13. 현재 구현 상태

**구현됨:**
- **섹터 노출 1차원 집계**: `decision.py` 가 `universe_instruments.asset_class` 를 섹터로 보고 목표비중을 섹터별 합산(`sector_exposure`).
- **섹터 집중 hard-block**: `sp > 30%` 위반 산출 → `risk.passed` 에 반영.
- **노출 결과 저장/표시**: `decisions.payload.sector_exposure` → 웹 portfolio 화면 조회 표시(DB truth, mock 없음).
- **Graph 이식 전제 스키마**: 모든 핵심 테이블이 정수 PK + scope/ref 패턴(`lessons`/`lesson_candidates`/`evidence_documents`/`target_allocations`/`decision_evidence_links`). PG+Graph 무손실 이관 설계 완료.
- **정책 한도 선언**: `policy.py` 에 국가/통화/인버스/레버리지 한도값이 정책 객체에 포함(값만).

**부분/주의:**
- 노출은 **목표비중 기준**만 계산. **보유(현재) 기준 노출**은 표시·검사에 미사용(`holdings.market_value` 는 종목 drift 에만 쓰임).
- 섹터=`asset_class` 재사용 → 진짜 섹터 분류 체계 없음.
- `decision_evidence_links` 테이블은 존재하나 쓰는 코드 경로가 없어 **빈 테이블**(decision→evidence 관계 미기록).

---

## 14. 미구현 / placeholder

- **Graph Index(그래프 엔진/노드/엣지/경로질의)**: 전면 미구현. `design_v2.md` §3 의 "PG 승격 시" 계획. SQLite 운영 DB 에 graph 테이블 없음.
- **계좌→종목→섹터→국가→통화 다차원 노출**: 섹터만 구현. **국가/통화 노출 집계 미구현**(policy 에 한도값만 선언, `universe_instruments.currency` 미사용).
- **ETF→구성종목→중복노출(중첩 보유)**: 데이터 소스·매핑·중복 계산 전부 미구현.
- **테마→종목 관계**: 미구현(`target_allocations.ref` 에 테마 슬롯만 있음).
- **decision→evidence→risk→order 관계 그래프**: `decision_evidence_links`/`evidence_documents` 테이블만 존재, **데이터 적재·관계 질의 미구현**(Vector/근거 영역과 함께 다음 증분).
- **리스크 설명(관계 기반 "왜")**: 현재 violation 은 평면 텍스트(`detail`)뿐. 인과/연결 그래프 설명 미구현.
- **보유 기준 노출 / 노출 추이 차트 UI**: 미구현.
- **전용 노출/그래프 테스트**: 미작성.

---

## 15. 다음 개선 항목

1. **국가/통화 노출 집계 구현**: `universe_instruments` 에 country/currency 분류 → 목표비중 합산 → `policy.limits.country_max_pct`/`currency_max_pct` 와 대조해 hard-block(섹터와 동형 확장). `advice.py:42` 가 이미 "지역 비중 없으면 환율 리스크가 숨는다"고 경고 중 → 실제 검사로 승격.
2. **보유 기준 노출 추가**: 목표 노출과 현재(보유) 노출을 함께 산출해 drift 의 노출 영향까지 설명.
3. **ETF→구성종목 매핑 도입**: 중첩 보유(중복 노출) 계산 → 단일종목 한도 우회 방지.
4. **섹터 분류 분리**: `asset_class` 재사용 대신 별도 섹터/테마 분류 체계.
5. **decision_evidence_links 쓰기 경로**: 목표비중 변경마다 evidence_id 링크 적재(현재 빈 테이블 활성화) → 관계 추적의 첫 실데이터.
6. **Graph 승격(PG)**: scope/ref 패턴을 노드/엣지로 투영, 관계 질의(리스크 "왜" 설명, ETF 중복 경로). `design_v2.md` §3 계획 실행.
7. **노출/그래프 전용 회귀 테스트** + 노출 시각화 UI.

---

## 16. 다른 Agent와의 의존성

- **목표비중(allocation) 영역**(`allocation.generate`, `target_allocations`, `universe_instruments`): 노출의 입력(목표비중·섹터/통화 분류)을 제공. 목표비중 없으면 노출도 없음(원칙).
- **프로필/정책 영역**(`policy.compile_policy`): 국가/통화/섹터/인버스/레버리지 한도값을 정책 객체로 제공 → 노출 위반 판정의 임계값 공급원(섹터만 실제 사용, 나머지는 선언 상태).
- **의사결정(decision) 영역**: 섹터 노출 집계·위반이 `decision.compute` 안에 **내장**되어 있다(독립 모듈 아님). 노출 결과는 `decisions.payload` 에 임베드.
- **리스크 게이트 영역**(`06-risk-gate.md`): 섹터 노출 위반(`sector_max_pct`)은 리스크 게이트 ②의 한 항목. 국가/통화/ETF중복/인버스 노출은 게이트가 받기를 기대하나 **미공급**(이 영역의 미구현이 리스크 게이트의 미구현과 짝).
- **근거/Vector 영역**(`evidence_documents`, `decision_evidence_links`): decision→evidence 관계의 데이터 substrate. 현재 미연결 — Graph 관계 설명의 핵심 전제(다음 증분에서 함께 구현).
- **lessons(성장) 영역**(`lessons`, `lesson_candidates`): scope/ref 패턴 공유 → Graph 노드/엣지 이식 시 함께 투영.
- **웹(DB 조회)**: `portfolio/page.tsx` 가 섹터 노출을 읽어 표시만, 로직 미보유.
