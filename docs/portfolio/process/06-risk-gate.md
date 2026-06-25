# Risk Gate Agent 시스템 프로세스 정리

> 이 문서는 **실제 코드 기반**으로 작성되었다. 추측 금지 — 함수명·테이블명·필드명·경로는 코드에 있는 그대로다.
> 미구현 항목은 정직하게 "미구현/계획"으로 표기한다.
> 공통 원칙: 단기 trading 아님(포트폴리오 비중관리 + 분할 리밸런싱) · 웹은 DB truth 조회만 · KIS 호출은 백엔드 sync/job만 · 운영화면 mock/하드코딩 금지 · 목표비중 없이 주문후보 금지 · 사람 승인 없이 주문 금지 · live 주문은 `KIS_LIVE_CONFIRM` 없이 하드차단 · 모든 decision 은 snapshot/version/provenance 기록.

근거 코드:
- `main_mission/portfolio_os/risk/gate.py` — 순수 함수 hard 게이트(`check_trades`)
- `main_mission/portfolio_os/decision.py` — 의사결정 시 포트폴리오 게이트(`violations`)
- `main_mission/portfolio_os/selection.py` — 목표비중 확정 전 사전검사(`precheck`: block/warn/info)
- `main_mission/portfolio_os/broker/order_service.py` — 주문 직전 리스크/시장가/모드/health 차단
- `main_mission/portfolio_os/broker/factory.py` — live 하드락(`_require_live_confirm`)
- `config/portfolio/risk_limits.yaml` — 한도 시드/사람용 참조
- `main_mission/portfolio_os/tests/test_risk_gate.py` — 회귀 테스트

---

## 1. 목적

Risk Gate Agent 는 **"잘못된 이동(매수/매도/목표선택)을 주문이 나가기 전에 차단"** 하는 영역이다. 한 곳이 아니라 의사결정 파이프라인의 **여러 지점에 분산된 게이트 집합**으로 구현되어 있다.

1. **목표비중 확정 게이트** (`selection.precheck`) — 3안 중 하나를 공식 target allocation 으로 확정하기 직전, 현금밴드/섹터/투자합/stale 등을 검사해 `block`/`warn`/`info` 로 분류.
2. **의사결정 게이트** (`decision.compute` 의 `violations`) — 회차 리밸런싱 후보를 만들 때 현금밴드/단일종목/섹터/qty0/stale 위반을 모아 `risk.passed` 로 산출.
3. **순수 hard 게이트** (`gate.check_trades`) — 거래 적용 후 예상 비중(`PostTradeWeights`)을 6개 hard 한도(`RiskLimits`)에 대조해 단 하나라도 위반이면 `passed=False`.
4. **주문 직전 게이트** (`order_service.submit_order`) — 시장가 매수 영구금지, 모드 일치, broker health, 리스크 통과, 매수여력을 순서대로 hard-block.
5. **live 하드락** (`factory._require_live_confirm`) — `KIS_LIVE_CONFIRM=I_UNDERSTAND` 없으면 live adapter 생성 자체를 `RuntimeError` 로 차단.

본질은 *자동매매*가 아니라 *안전한 위임 + 추적 가능한 차단 사유 제시*다.

---

## 2. 전체 흐름

```text
[대전제: investor_profile] → policy.compile_policy → limits/cash_band
        │
        ▼
[allocation.generate: 보수/기준/공격 3안] (target_allocations)
        │
        ▼
selection.precheck(rows, policy, stale)      ← 게이트 ①
   block / warn / info  →  allocation_selections.precheck_status 저장
        │ (사람 선택 = active)
        ▼
decision.compute(account_index)              ← 게이트 ②
   현금밴드·단일·섹터·qty0·stale → violations → risk.passed
   → decisions / rebalance_plans / rebalance_plan_steps 저장
        │ (승인된 후보 1건)
        ▼
gate.check_trades(PostTradeWeights, RiskLimits)  ← 게이트 ③ (순수 hard)
        │  passed=True 일 때만
        ▼
order_service.submit_order(..., risk_passed=...) ← 게이트 ④
   시장가매수금지 → 모드일치 → health → risk → 매수여력
        │
        ▼
broker (factory.get_broker) — live 면 _require_live_confirm ← 게이트 ⑤
```

게이트 ②와 ③의 관계: `decision.compute` 는 **딕셔너리 기반 인라인 violations** 를 직접 계산해 저장한다. `gate.check_trades`(순수 dataclass 버전)는 **현재 decision.py 에서 호출되지 않으며**, `RiskLimits` 기본값만 import 해서 쓴다(`from .risk.gate import RiskLimits`). 즉 게이트 ③은 테스트(`test_risk_gate.py`)로만 검증되는 독립 순수 함수다(§13 참조).

---

## 3. 입력

| 게이트 | 입력 | 출처 |
|---|---|---|
| ① precheck | `rows`(kind/ref/weight_pct), `policy`(limits/cash_band/pace), `stale` | `target_allocations`, `policy.latest`, `account_snapshots.captured_at` |
| ② decision | `account_index`, 최신 `account_snapshots`, `holdings`, `universe_instruments`(target_weight_pct/last_price/asset_class), `investor_profile`(cash_min/max_pct, rebalance_pace, individual_cap_pct) | SQLite (`store/db.py`) |
| ③ check_trades | `PostTradeWeights`(cash_pct, single_name_max_pct, short_total_pct, leverage_total_pct, largest_order_pct, order_count), `RiskLimits` | 호출측이 계산해 전달(현재 테스트만) |
| ④ submit_order | `OrderRequest`, `Account`, `available_cash_krw`, `risk_passed`, `urgent_sell` | 호출측(승인 흐름) |
| ⑤ live락 | env `KIS_LIVE_CONFIRM`, `KIS_MODE`/`KIS_ACCOUNT_{n}_MODE` | `.env` (코드/DB 금지) |

핵심 정책 상수(코드에 하드코딩된 정책 기본값 — 추후 DB/config 승격 예정):
- `decision.py`: `SECTOR_MAX_PCT = 30.0`, `STALE_HOURS = 24.0`
- `selection.py`: `STALE_HOURS = 24.0`, `PACE_CAP = {"slow":3.0,"normal":5.0,"fast":5.0}`
- `gate.py RiskLimits` 기본값: 현금 10 / 단일 20 / 숏 10 / 레버 15 / 1주문 5 / 세션 20

---

## 4. 출력

| 게이트 | 출력 형태 |
|---|---|
| ① precheck | `{"status": "pass\|warn\|block", "reasons": [{"level","msg"}], "one_order_cap_pct"}` |
| ② decision | `result["risk"] = {"passed": bool, "violations": [{"limit","observed","threshold","detail"}]}` + `provenance.risk_policy` |
| ③ check_trades | `RiskResult(passed: bool, violations: list[Violation(limit, observed, threshold, detail)])` |
| ④ submit_order | `{"ok": bool, "status": "submitted\|aborted\|rejected\|in_doubt", "reason": ...}` |
| ⑤ live락 | 통과 시 adapter, 실패 시 `RuntimeError`(예외로 중단) |

violations `limit` 코드값(실제 사용 문자열):
- decision.py: `cash_min_pct`, `single_name_max_pct`, `min_order_qty`, `stale_snapshot`, `sector_max_pct`, `cash_band_min`, `cash_band_max`
- gate.py: `cash_min_pct`, `single_name_max_pct`, `short_total_max_pct`, `leverage_total_max_pct`, `single_order_max_pct`, `max_orders_per_session`

---

## 5. DB 테이블

게이트 **전용 테이블은 없다.** 결과는 다른 영역 테이블에 임베드되어 저장된다(`store/schema.sql`):

| 테이블 | 게이트 관련 컬럼 |
|---|---|
| `decisions` | `payload`(JSON 안에 `risk.passed`/`violations`/`provenance.risk_policy` 포함) |
| `allocation_selections` | `precheck_status`(pass/warn/block), `precheck_reasons`(JSON), `user_override`(block 무시 선택 여부) |
| `rebalance_plan_steps` | `status`(candidate/blocked), `reason`(차단 사유) |
| `audit_logs` | 주문 차단 이벤트(`risk_block`, `order_block_market`, `order_block_mode_mismatch`, `order_block_buying_power`, `order_block_dup_payload`, `order_in_doubt`) |
| `orders` | `status`(submitting/submitted/aborted/rejected/in_doubt), `reason` |

> ⚠️ **`risk_checks` / `risk_limits` 테이블은 현재 SQLite 스키마(`store/schema.sql`)에 존재하지 않는다.** CLAUDE.md §5/§6 와 `migrations/001_init.sql` 의 PostgreSQL 초안·`docs/portfolio/db_schema.md` 에는 등장하지만, 운영 SQLite 에는 미생성. 현재 한도 SSOT 는 **코드 `RiskLimits` dataclass 기본값**과 `config/portfolio/risk_limits.yaml`(사람용 시드) 두 곳이다 — DB 단일 SSOT 는 미구현.

---

## 6. API / 함수

| 함수 | 위치 | 역할 |
|---|---|---|
| `check_trades(weights, limits) -> RiskResult` | `risk/gate.py:49` | 6개 hard 한도 순수 검사 |
| `RiskLimits` (dataclass, frozen) | `risk/gate.py:13` | 한도 기본값 컨테이너 |
| `PostTradeWeights` / `Violation` / `RiskResult` | `risk/gate.py` | 입출력 dataclass |
| `precheck(rows, policy, stale) -> dict` | `selection.py:49` | block/warn/info 분류, 내부 `block()`/`warn()` 클로저 |
| `compute(account_index) -> dict` | `decision.py:37` | violations 계산 + decisions/plan 저장 |
| `submit_order(...)` | `broker/order_service.py:40` | 주문 직전 다단계 차단 |
| `_require_live_confirm()` | `broker/factory.py:17` | live 하드락 |
| `audit.record(...)` | `audit/logger.py:17` | 차단 이벤트 감사 기록 |

CLI 진입점: `python -m main_mission.portfolio_os.decision --account N`, `python -m main_mission.portfolio_os.selection --account N --options|--select P V`.

---

## 7. UI 화면

웹은 **DB truth 조회만** (게이트 로직 미포함, 결과만 표시):

- `web/app/accounts/[id]/allocation/page.tsx` — `PreBadge`(block→"한도 위반", warn→"주의"), `precheck.reasons` 를 level 별 색으로 렌더, block 일 때 버튼 문구 "한도 위반 — 무시하고 선택"(`user_override=1` 로 select).
- `web/app/accounts/[id]/portfolio/page.tsx` — `d.risk.passed` 로 통과/차단 배너, `d.risk.violations` 목록, 라인별 `blocked`/`block_reason`, `snapshot_stale` 경고, `provenance.risk_policy.sector_max_pct` 등 근거 표시.
- 데이터 접근: `web/lib/server/portfolioDb.ts`(node:sqlite, 읽기 전용). 운영화면에 mock/하드코딩 없음(원칙 준수).

---

## 8. 상태 전이

**precheck status** (`selection.py`): `pass` → `warn`(block 아닐 때만 승격) → `block`(최종, 강등 불가). block 이 한 번이라도 발생하면 warn 으로 내려가지 않음(`if status != "block"` 가드).

**allocation_selections.status**: `active` → `superseded`(재선택 시) / `cancelled`(취소) / `chosen`·`archived`(target_allocations 측). 이력 삭제 금지(append-only, status 만 변경).

**orders.status** (게이트가 끊는 지점): `submitting` → `submitted` | `aborted`(게이트 차단) | `rejected` | `in_doubt`(응답 불확실, 재전송 금지).

---

## 9. 예외 / 실패 케이스

- **스냅샷 없음**: `decision.compute` / `selection.select` → `{"ok": False, "error": "잔고 스냅샷이 없습니다..."}`. 목표비중 없이 / 잔고 없이 주문후보 생성 안 됨(원칙 준수).
- **captured_at 파싱 실패**: `_is_stale`/decision 의 try/except → `stale=False`, `age_h=None`(보수적으로 통과시킴 — §15 위험).
- **price 없음/0**: 매수 후보 `cycle_qty=0` → `blocked=True`, `block_reason="시세 없음 — 차단"`.
- **broker unhealthy**: `submit_order` → `aborted` "broker unhealthy (A3)".
- **in_doubt**: 전송 중 예외 → 재전송 금지, `orders.status=in_doubt` + audit.
- **live 미확인**: `_require_live_confirm` → `RuntimeError`(adapter 생성 중단).
- **decision/selection 내부 오류**: `main()` 에서 `{"ok": False, "error": "내부 오류: ..."}` 로 감싸 반환.

---

## 10. Hard-block 조건

**게이트 ② decision.compute (violations → risk.passed=False):**
- `cash_target_pct < cash_min_pct`(기본 10)
- 최대 목표비중 `max_tgt > single_name_max_pct`(기본 20)
- `qty0_blocked > 0`(최소주문 미달 후보 존재)
- `stale`(snapshot age > 24h)
- 섹터 노출 `sp > SECTOR_MAX_PCT`(30)
- `cash_band_min` / `cash_band_max`(대전제 cash_min/max_pct 위반)

**게이트 ③ check_trades (6개 hard):** cash_min_pct, single_name_max_pct, short_total_max_pct, leverage_total_max_pct, single_order_max_pct, max_orders_per_session.

**게이트 ① precheck 의 block(hard):** 현금 < cash_band.min, 테마 weight > sector_max, 투자합 > 100, stale. (cash > band.max, 테마 > single_max 는 **warn** = soft.)

**게이트 ④ submit_order (hard, aborted/rejected):** 시장가 매수(영구금지)·비긴급 시장가 매도, 모드 불일치, broker unhealthy, `risk_passed=False`, 매수여력 초과, 중복 payload.

**게이트 ⑤:** `KIS_LIVE_CONFIRM != "I_UNDERSTAND"` → live adapter 생성 하드차단.

### Hard-block vs Warning 구분
- **Hard**: 위 §10 전부 + risk_limits.yaml 에서 `hard: true`(cash_min, single_name, short_total, leverage_total, daily_loss_stop, single_order). 후보 생성/주문 차단.
- **Soft/Warning**: precheck 의 `warn`(현금 상한 초과·단일한도 초과 advisory), `max_orders_per_session`(yaml `hard: false`), yaml time_guards 의 `vi_triggered_hold`/advisory 항목.

---

## 11. 로그 / 감사 기록

- **audit_logs**(`audit/logger.py record`): 주문 차단 시 액션명으로 기록 — `risk_block`, `order_block_market`, `order_block_mode_mismatch`, `order_block_buying_power`, `order_block_dup_payload`, `order_in_doubt`, `order_urgent_market_sell`(CRITICAL). actor 는 `risk-chief`/`broker-chief`, level WARNING/CRITICAL.
- **provenance**: decision payload 에 `account_snapshot_id`, `universe_active_count`, `risk_policy`(적용 한도 스냅샷), `cash_band` 기록 → 어떤 한도로 차단/통과했는지 추적 가능.
- **precheck_reasons**: `allocation_selections.precheck_reasons` 에 JSON 으로 사유 영구 저장(append-only). `user_override` 로 block 무시 선택도 기록.

---

## 12. 테스트 기준

`main_mission/portfolio_os/tests/test_risk_gate.py` (pytest 없이도 `__main__` 러너 동작):
- `test_clean_passes` — 정상 통과
- `test_cash_below_min_blocks` — cash_min_pct 위반 차단
- `test_single_name_over_blocks` — 단일종목 초과 차단
- `test_short_over_blocks` / `test_leverage_over_blocks` / `test_big_order_blocks`
- `test_multiple_violations_collected` — 다중 위반 동시 수집(≥2)

**커버 대상은 게이트 ③(`check_trades`)뿐.** 게이트 ①(precheck) / ②(decision.violations) / ④(submit_order) 에 대한 전용 단위 테스트는 미작성(§14). order_service 는 `tests/test_order_safety.py`(별도 영역, 16테스트)에서 일부 커버.

---

## 13. 현재 구현 상태

**구현됨:**
- 게이트 ③ 순수 hard 게이트 `check_trades` + 6한도 + 회귀 테스트 7건.
- 게이트 ② `decision.compute` 의 인라인 violations(현금밴드/단일/섹터/qty0/stale/cash_band) — decisions/rebalance_plan_steps 에 저장, 웹 표시.
- 게이트 ① `selection.precheck` block/warn/info 3단계 + allocation_selections 저장.
- 게이트 ④ `submit_order` 다단계 차단(시장가/모드/health/risk/매수여력/idempotency) + audit.
- 게이트 ⑤ live 하드락 `_require_live_confirm`.
- 웹 조회 전용 표시(allocation/portfolio page), provenance 기록.

**부분/주의:**
- 게이트 ③ `check_trades` 는 **decision 파이프라인에서 호출되지 않음** — decision.py 는 동일 한도값을 인라인으로 재구현. 즉 순수 게이트와 운영 게이트가 **이중 구현**(중복·드리프트 위험).
- 한도 SSOT 가 코드(`RiskLimits`)·yaml 두 곳 — DB `risk_limits` 테이블 미생성.

---

## 14. 미구현 / placeholder

- **국가/통화 한도**: 미구현(다음 증분). policy.policy 컬럼 주석에 "국가/통화" 언급, `risk_limits.yaml` 에 `fx_effective_exposure.usd_asset_multiplier=1.1` 초기값만 존재 — 실제 검사 로직 없음.
- **ETF 중복(중첩 보유) 검사**: 미구현(다음 증분). 코드/스키마에 해당 로직 없음.
- **인버스/레버리지 검사**: `gate.check_trades` 에 한도는 있으나 decision/precheck 운영 경로에서는 **미적용**. precheck 주석: "본 3안엔 인버스/레버리지 테마 없음 → 0(pass). 도입 시 검사."
- **가격 이상치(price outlier) 검사**: 미구현. qty=0(시세 없음/금액 미달)만 차단, "비정상 시세 스파이크" 검출 없음.
- **`risk_checks` / `risk_limits` DB 테이블**: 미생성(PostgreSQL 초안·문서에만 존재).
- **1일 주문 수(daily) / daily_loss_stop / max_drawdown_stop**: yaml 에 정의되어 있으나 코드 미적용. `max_orders_per_session` 만 gate.py 에 존재(decision 경로 미사용).
- **time_guards(개장 블랙아웃·VI·서킷브레이커)**: yaml 정의만, 코드 게이트 미구현.
- **stale 파싱 실패 시 fail-open**: `except → stale=False` 는 보수적이지 않음(placeholder 성격).
- **precheck/decision/submit_order 전용 단위 테스트**: 미작성.

---

## 15. 다음 개선 항목

1. **한도 SSOT 일원화**: `risk_limits` 테이블 생성 → `RiskLimits`/yaml/decision 인라인값을 DB 로드로 통합(이중 구현 제거).
2. **게이트 ③ 운영 연결**: `decision.compute` 가 인라인 대신 `check_trades(PostTradeWeights, RiskLimits)` 를 호출하도록 리팩터 → 단일 진실.
3. **국가/통화·ETF 중복·인버스/레버리지·가격 이상치** 검사 증분 구현 + 테스트.
4. **stale 파싱 실패를 fail-closed** 로 전환(애매하면 차단).
5. **daily 한도·drawdown·time_guards** 를 코드 게이트로 승격(yaml→실행).
6. precheck/decision/submit_order 전용 회귀 테스트 추가.
7. `risk_checks` 감사 테이블로 위반 이력을 정형화(현재 JSON 임베드 → 질의 가능 RDB).

---

## 16. 다른 Agent와의 의존성

- **프로필/정책 영역**(`investor_profile`, `policy.compile_policy`): 대전제 cash_band·limits·pace 를 제공 → 게이트 ①②의 cash_band/sector 한도 입력원.
- **목표비중(allocation) 영역**(`allocation.generate`, `target_allocations`): 3안 rows → precheck 입력. 목표비중 없으면 게이트는 후보를 만들지 않음(원칙).
- **의사결정(decision) 영역**: 게이트 ②를 내장. 게이트는 decision 산출물(`decisions`/`rebalance_plan_steps`)에 결과를 임베드.
- **주문/브로커 영역**(`order_service`, `factory`, KIS adapter): 게이트 ④⑤ 보유. 게이트 통과 결과(`risk_passed`)를 `submit_order` 에 전달받아 최종 차단. KIS 호출은 백엔드 sync/job·order_service 만(웹 직접 호출 없음).
- **감사 영역**(`audit/logger`): 차단 이벤트 기록 대상.
- **웹(DB 조회)**: 게이트 결과를 읽어 표시만, 로직 미보유.
- **broker-chief / risk-chief role**(`agents/portfolio/`): gate.py docstring 상 "risk-chief 만 게이트 통과/차단 권한". 현재 단일 agent(broker-chief) 운영이나 audit actor 로 `risk-chief` 표기 유지.
