# Order / Execution Safety Agent 시스템 프로세스 정리

> 영역: 주문후보 vs 실제주문 분리 · 승인대기 · paper 주문 · live 하드락 · 지정가 원칙 · 시장가 매수금지 · 긴급 시장가 매도 예외 · 체결/취소/오류 이력 · 실행 전 최종검증
> 코드 SSOT: `main_mission/portfolio_os/broker/order_service.py`, `broker/factory.py`, `broker/port.py`, `broker/kis_adapter.py`, `store/schema.sql`
> 원칙: 단기 trading 이 아니라 *포트폴리오 비중관리 + 분할 리밸런싱*. 웹은 DB truth 조회만. KIS 호출은 백엔드 sync/job 만. 사람 승인 없이 주문 금지, live 는 `KIS_LIVE_CONFIRM` 없이 하드차단.

---

## 1. 목적

CEO 가 승인한 **주문 후보 1건**을 broker(KIS)로 안전하게 제출하는 **마지막 게이트**다. 이 영역의 책임은 "무엇을 살지"(목표비중·드리프트·리밸런싱 계획)가 아니라, 이미 결정된 후보를 **실제 주문으로 전환하기 직전의 최종검증 + 멱등 전송 + 상태머신 추적**이다.

- 진입은 항상 **지정가**(예측 진입). 시장가 매수 **영구 금지**, 시장가 매도는 *긴급 매도*에 한해 명시적 예외 (CLAUDE.md §2 규칙16).
- 전송 응답이 불확실하면 **재전송하지 않고** `in_doubt` 로 기록 (이중주문 방지).
- `live` 주문은 `KIS_LIVE_CONFIRM=I_UNDERSTAND` 가드를 통과한 adapter 로만 가능.
- 모든 차단/거절/제출/불확실은 `audit_logs` 에 영속.

핵심 함수: `order_service.submit_order(...)` (`broker/order_service.py:40`).

---

## 2. 전체 흐름

`submit_order()` 의 실제 검증 체인(코드 순서 그대로):

```
승인된 주문 후보(OrderRequest) + risk_passed + available_cash_krw
   │
   1) payload_hash(req) 계산                              ← order_service.py:24
   2) idempotency 조회: orders WHERE client_order_id=?    ← :58
        ├ payload_hash 불일치 → rejected "different payload (A4)"  ← :63
        └ status ∈ (submitted/partial/filled) → idempotent 반환(재전송 안 함) ← :69
   3) 시장가 정책(§16):                                    ← :75
        ├ side=buy & market           → aborted "시장가 매수 금지"
        ├ side=sell & market & not urgent_sell → aborted "시장가 매도 금지"
        └ side=sell & market & urgent_sell     → CRITICAL 감사 후 통과
   4) 모드 일치: account.mode == broker.mode ?            ← :91  (불일치 → aborted)
   5) broker.is_healthy ?                                 ← :100 (False → aborted, A3)
   6) risk_passed is False ?                              ← :104 (→ aborted "risk gate failed")
   7) 매수여력: qty*limit_price <= available_cash_krw ?   ← :112 (초과 → aborted)
   │
   ── orders 원장에 status='submitting' INSERT OR IGNORE ── :123
   │
   8) broker.place_order(account, req)                    ← :133
        ├ 예외 발생          → status='in_doubt' (재조회 필요)   ← :135
        ├ ack.accepted=True  → status='submitted' + broker_order_id ← :144
        └ ack.accepted=False → reason 에 in_doubt 표현이면 in_doubt, 아니면 rejected ← :157
```

각 단계의 차단/통과는 `audit.record(...)` 로 기록되고 `conn.commit()` 으로 즉시 영속된다.

---

## 3. 입력

`submit_order()` 시그니처 (`order_service.py:40`):

| 인자 | 타입 | 의미 |
|---|---|---|
| `broker` | BrokerPort | MockAdapter / KisPaperAdapter / KisLiveAdapter (factory 가 주입) |
| `account` | `Account` | `id`, `mode`(paper/live), base_currency (`port.py:18`) |
| `req` | `OrderRequest` | `client_order_id`(멱등키, 필수), `instrument`, `side`, `qty`, `order_type`(limit 기본), `limit_price` (`port.py:55`) |
| `available_cash_krw` | float \| None | 매수여력 검증용 현금(예수금). None 이면 매수여력 검사 skip |
| `risk_passed` | bool \| None | **호출측이 `risk/gate.py` 로 계산해 전달**. False 면 hard-block. None 이면 검사 skip |
| `urgent_sell` | bool | 긴급 매도 시 시장가 매도 1건 허용하는 명시 예외 플래그 |
| `conn` | sqlite3.Connection \| None | 외부 트랜잭션 주입(없으면 자체 connect/close) |

- 입력의 truth 원천: `risk_passed` ← Risk Gate Agent, `available_cash_krw` ← 계좌 sync(`account_snapshots.cash_krw`), `req` ← 리밸런싱 계획(`rebalance_plan_steps`)에서 승인된 후보.
- **주의(미연결)**: 현재 코드에는 "목표비중/계획에서 후보를 가져와 `OrderRequest` 를 만들고 사람 승인을 받아 `submit_order` 를 호출하는" 오케스트레이션이 없다. `submit_order` 는 단위 진입점으로만 존재한다(§13/§14).

---

## 4. 출력

`_result(...)` dict (`order_service.py:36`):

```python
{"ok": bool, "status": str, "reason": str|None, "broker_order_id": str|None, **extra}
```

| status | ok | 의미 |
|---|---|---|
| `submitted` | True | broker 가 수락, `broker_order_id` 채워짐 |
| `rejected` | False | 멱등 payload 충돌(A4) 또는 broker 거절 |
| `aborted` | False | 사전검증 차단(시장가/모드/health/risk/매수여력) |
| `in_doubt` | False | 전송 응답 불확실 — 재조회 필요, 재전송 금지 |
| `submitted`+`duplicate=True` | True | 이미 제출됨(멱등) |

부수 출력: `orders` 원장 1행(상태 갱신), `audit_logs` 다건. `list_orders(status=...)` 로 조회(`order_service.py:172`).

---

## 5. DB 테이블

### `orders` — 주문 원장 + idempotency + 상태머신 (`schema.sql:104`)

| 컬럼 | 비고 |
|---|---|
| `client_order_id` TEXT **UNIQUE NOT NULL** | 멱등키 (A4) |
| `payload_hash` TEXT NOT NULL | sha256(ticker/market/side/qty/order_type/limit_price), `payload_hash()` 생성 |
| `account_id`, `mode` | mode NOT NULL |
| `ticker`, `side`, `qty`, `order_type`, `limit_price` | 주문 내용 |
| `broker_order_id` | KIS ODNO (수락 후) |
| `status` | CHECK IN (`created`,`submitting`,`submitted`,`in_doubt`,`partial`,`filled`,`rejected`,`canceled`,`aborted`) |
| `reason`, `created_at`, `updated_at` | |

인덱스: `idx_orders_status`, `idx_orders_account`.

### `audit_logs` — 감사 (`schema.sql:85`)

`actor`, `action`, `entity_type`, `entity_id`, `mode`, `level`(CHECK CRITICAL/WARNING/INFO), `payload`(JSON, 비밀값 스캔 후), `created_at`(UTC ISO8601). 인덱스: created/entity/action.

해당 없음(이 영역 밖): `fills`(체결), `proposal_trades`, `approvals` 테이블은 **스키마에 미존재**(CLAUDE.md §6 의 계획 목록에는 있으나 `store/schema.sql` 에 미정의). 체결/승인 영속화는 미구현(§14).

---

## 6. API / 함수

| 함수 | 위치 | 역할 |
|---|---|---|
| `submit_order(...)` | order_service.py:40 | 검증체인 + 전송 + 상태머신 (메인) |
| `payload_hash(req)` | order_service.py:24 | 멱등 payload 지문 |
| `list_orders(status=None)` | order_service.py:172 | 원장 조회 |
| `audit.record(...)` | audit/logger.py:17 | 감사 1건(비밀값 차단 포함) |
| `get_broker(mode, account_index)` | factory.py:36 | mode→adapter 주입 |
| `_require_live_confirm()` | factory.py:17 | live 하드락(`KIS_LIVE_CONFIRM`) |
| `place_order(account, req)` | kis_adapter.py:145 | KIS 주문 POST(ORD_DVSN 01/00), adapter 자체 멱등셋 |
| `cancel_order(...)` | kis_adapter.py:175 | **NotImplementedError** (미구현) |
| `get_fills(...)` | kis_adapter.py:140 | `[]` 반환 (미구현 stub) |
| `get_open_orders(...)` | kis_adapter.py:136 | `[]` 반환 (미구현 stub) |

`MockAdapter` 는 오프라인 결정론 경로(키 없이 전체 검증 테스트). live 가드: `factory._dispatch` 가 `mode=="live"` 일 때 `_require_live_confirm()` 호출, 없으면 RuntimeError 로 차단(`factory.py:30`).

---

## 7. UI 화면

**해당 없음 — 미구현.** `web/app` 에 주문 후보 검토/승인대기/제출/체결 화면 및 API route 가 존재하지 않는다. 현존 라우트는 accounts/strategy/allocation/universe/portfolio/decision/advice 뿐(`web/app/api/accounts/[id]/*`). 승인 워크플로 UI 는 미구현이며, 현재 운영 수준은 **제안 + 수동 승인**(CLAUDE.md §7).

설계 원칙(향후): 웹은 `orders`/`audit_logs` DB truth 조회만, KIS 호출은 백엔드 sync/job 만, mock/하드코딩 화면 금지.

---

## 8. 상태 전이

`orders.status` (CHECK 제약 = 허용집합):

```
            ┌─(사전검증 차단)──────────────► aborted
            │   (시장가/모드/health/risk/매수여력)
created ─► submitting ─► place_order
                          ├─ ack.accepted ───────► submitted ─► (partial) ─► filled   ※ partial/filled 갱신 코드 없음
                          ├─ 예외/불확실 표현 ───► in_doubt   (재전송 금지, 재조회 필요)
                          └─ ack 거절 ───────────► rejected
멱등 충돌(다른 payload) ──────────────────────► rejected (원장 무변경)
```

- 실제로 코드가 **기록하는** 상태: `submitting`, `submitted`, `in_doubt`, `rejected`, `aborted`.
- **갱신 경로 없음**(스키마엔 있으나 미사용): `partial`, `filled`, `canceled` — 체결/취소 폴링 미구현 때문(§14).
- `submitting` 은 INSERT OR IGNORE 로 잠깐 기록 후 즉시 전송 결과로 덮어쓴다(`order_service.py:123`).

---

## 9. 예외 / 실패 케이스

| 케이스 | 코드 처리 | 결과 |
|---|---|---|
| 같은 client_order_id, 다른 payload | `payload_hash` 비교(:63) | rejected, `order_block_dup_payload` WARNING |
| 이미 submitted/partial/filled 재호출 | :69 | idempotent 반환(`duplicate=True`), 재전송 안 함 |
| 시장가 매수 | :75 | aborted, `order_block_market` WARNING |
| 시장가 매도(urgent_sell 미지정) | :76 | aborted, `order_block_market` WARNING |
| 모드 불일치(account≠broker) | :91 | aborted, `order_block_mode_mismatch` WARNING |
| broker unhealthy (A3) | :100 | aborted "broker unhealthy" (감사기록 없음) |
| risk_passed=False | :104 | aborted, `risk_block` WARNING |
| 매수여력 초과 | :112 | aborted, `order_block_buying_power` WARNING |
| 전송 중 예외(소켓 타임아웃 등) | :134 | in_doubt, `order_in_doubt` WARNING, **재전송 금지** |
| ack 거절 + reason 에 "in_doubt/전송 오류/재조회" | :157 | in_doubt |
| ack 거절 그 외 | :157 | rejected |
| 감사 payload 에 비밀값(app_key 등) | logger.py:31 | `AuditError` — 감사기록 자체 차단 |

KIS adapter `place_order` 도 자체적으로 unhealthy/중복/전송예외 시 `OrderAck(accepted=False, reason=...)` 로 안전 반환(`kis_adapter.py:146`).

---

## 10. Hard-block 조건

이 영역의 hard-block(통과 불가, 주문 미전송):

1. **시장가 매수 영구 금지** — `side=buy & order_type=market` → aborted (§16, 예외 없음).
2. **시장가 매도** — `urgent_sell=True` 명시 없으면 aborted (§16).
3. **live 어댑터 생성 차단** — `KIS_LIVE_CONFIRM != "I_UNDERSTAND"` 면 broker 자체가 생성 안 됨(RuntimeError, `factory.py:18`). 즉 submit 이전 단계에서 하드락.
4. **모드 불일치** — paper 의도에 live adapter 등 사고 방지(:91).
5. **broker unhealthy** — `is_healthy=False` 면 주문 중단(A3, 규칙11).
6. **risk gate 실패** — 호출측 `risk_passed=False`(규칙7 hard-block 결과를 전달받아 차단).
7. **매수여력 초과** — notional > cash 차단.
8. **멱등 payload 충돌** — 같은 id 다른 내용은 rejected(이중주문 방지).

목표비중 없이 후보 생성 금지·사람 승인 없는 주문 금지는 **상류 영역**(allocation/rebalance/approval)의 책임이며, 이 영역은 그 결과(`risk_passed`, 승인된 `OrderRequest`)를 신뢰하고 최종 게이트만 수행한다.

---

## 11. 로그 / 감사 기록

모든 결정은 `audit.record()` 로 `audit_logs` 에 영속(UTC, 비밀값 스캔 후). 이 영역이 남기는 action:

| action | level | 시점 |
|---|---|---|
| `order_block_dup_payload` | WARNING | 멱등 payload 충돌 |
| `order_block_market` | WARNING | 시장가 매수/매도 차단 |
| `order_urgent_market_sell` | **CRITICAL** | 긴급 시장가 매도 허용(강조) |
| `order_block_mode_mismatch` | WARNING | 모드 불일치 |
| `risk_block` | WARNING | 리스크 게이트 실패 |
| `order_block_buying_power` | WARNING | 매수여력 부족 |
| `order_in_doubt` | WARNING | 전송 불확실 |
| `order_submit` | INFO | 정상 제출 |
| `order_rejected` / `order_in_doubt` | WARNING | ack 거절 분기(:163) |

provenance: actor 는 `broker-chief`(전송) 또는 `risk-chief`(차단). `mode` 로 paper/live 구분. payload 에 자격증명 들어가면 `secrets_detector.scan` 이 `AuditError` 로 기록 자체를 막는다(logger.py:31). `orders` 원장 자체도 audit(상태/이유 추적)이며 append/update 로 이력화.

미구현: `partial/filled/canceled` 체결·취소 audit(폴링 없음), 주문 ↔ 결정/계획 link 기록.

---

## 12. 테스트 기준

`main_mission/portfolio_os/tests/test_order_safety.py` — 키 없이 MockAdapter + 임시 SQLite 로 전 경로 검증. 13개 테스트:

| 테스트 | 검증 |
|---|---|
| `test_normal_submit` | 정상 submitted + 원장 기록 |
| `test_idempotent_same_payload` | 동일 payload 재호출 → duplicate, 재전송 안 함 |
| `test_dup_id_different_payload_rejected` | 다른 payload → rejected |
| `test_mode_mismatch_aborted` | account=live/broker=paper → aborted |
| `test_risk_block_aborted` | risk_passed=False → aborted |
| `test_insufficient_buying_power` | notional>cash → aborted |
| `test_market_buy_blocked` | 시장가 매수 → aborted "시장가 매수" |
| `test_market_sell_blocked_without_urgent` | 시장가 매도(비긴급) → aborted |
| `test_urgent_market_sell_allowed` | urgent_sell=True → submitted |
| `test_in_doubt_on_exception` | place_order 예외 → in_doubt + 원장 기록 |
| `test_audit_blocks_secret` | app_key payload → AuditError |
| `test_audit_records_order` | order_submit 감사로그 존재 |

실행: `python -m main_mission.portfolio_os.tests.test_order_safety` (또는 pytest). 메모리에 "16 테스트 통과" 기록 있음 — 현재 파일은 12 test 함수(+`setup`). 미커버: 체결/취소/live 가드 경로, 동시성.

---

## 13. 현재 구현 상태

**구현됨 (검증 완료):**
- `submit_order` 전체 사전검증 체인: 멱등(payload_hash) → 시장가 정책 → 모드일치 → health → risk → 매수여력 (`order_service.py`).
- `orders` 원장 상태머신(submitting/submitted/in_doubt/rejected/aborted) + `audit_logs` 영속.
- 시장가 매수 영구 금지 / 긴급 매도만 예외(§16) — 테스트로 보장.
- in_doubt 처리(전송 불확실 시 재전송 금지).
- live 하드락: `factory._require_live_confirm()`(`KIS_LIVE_CONFIRM=I_UNDERSTAND`).
- KIS paper/live adapter `place_order`(국내주식, ORD_DVSN 01/00, hashkey, rt_cd 판정) — paper 우선.
- MockAdapter 오프라인 결정론 경로 + 12개 테스트.

**부분 구현:**
- adapter 멱등은 인메모리 `_seen_client_ids`(프로세스 재시작 시 소실) — DB 원장 멱등이 진짜 SSOT.

---

## 14. 미구현 / placeholder

- **승인 워크플로 UI 전체** — 주문 후보 검토/승인대기/제출/취소 화면·API route 없음(`web/app` 미존재). 현재 제안+수동 승인 (CLAUDE.md §7, §13).
- **후보→주문 오케스트레이션** — `rebalance_plan_steps`(계획)에서 승인된 후보를 `OrderRequest` 로 만들고 `submit_order` 를 호출하는 연결 로직 없음. `submit_order` 는 단위 진입점만 존재.
- **체결(fills) 추적** — `kis_adapter.get_fills()` 는 `[]` stub, `fills` 테이블 스키마 자체 미존재. `partial/filled` 상태 갱신 코드 없음.
- **취소(cancel)** — `kis_adapter.cancel_order()` = `NotImplementedError`(order-rvsecncl 미연결). `canceled` 상태 미사용.
- **미체결조회** — `get_open_orders()` = `[]` stub. in_doubt 자동 재조회 루프 없음(수동 재조회 전제).
- **승인 영속** — `approvals`/`proposal_trades` 테이블 스키마 미정의(§6 계획 목록엔 있으나 `schema.sql` 에 없음). 누가 언제 승인했는지 DB 기록 경로 없음.
- **미국주식/FX/WebSocket 체결통보** — adapter TODO(api_adapter.md §3/§7), `get_fx_rate` = NotImplementedError.
- **세션 주문 수 한도(20)** — 이 영역에서 미체크(risk gate 측 책임, 연결 미확인).

---

## 15. 다음 개선 항목

1. 체결 폴링/통보 연결 → `fills` 테이블 신설 + `partial/filled` 상태 갱신 + 체결 audit.
2. `cancel_order`(order-rvsecncl) 구현 + `canceled` 상태 경로 + 취소 audit.
3. in_doubt 자동 재조회 잡(`get_open_orders`로 broker truth 대조) — 재전송 대신 동기화.
4. 승인 영속화: `approvals` 테이블 + 승인 actor/시각/사유 → `submit_order` 가 승인 토큰 확인 후만 실행(사람 승인 없는 주문 금지를 코드 강제).
5. 후보→주문 오케스트레이터: `rebalance_plan_steps`(candidate) → 승인 → `OrderRequest` → `submit_order`, 결정/계획 ↔ 주문 link 기록(provenance).
6. 승인대기/제출/체결 조회 UI(DB truth 전용, KIS 직접호출 금지).
7. live 전환 체크리스트를 코드 가드와 연동(KIS_LIVE_CONFIRM + CEO 체크리스트, 규칙15).
8. 세션 주문 수/레이트리밋을 submit 직전 재확인(상류 risk gate 와 이중방어).

---

## 16. 다른 Agent와의 의존성

| 의존 대상 | 방향 | 내용 |
|---|---|---|
| **Risk Gate Agent** (`risk/gate.py`, `06-risk-gate.md`) | 입력 | `risk_passed` 를 계산해 전달. False 면 이 영역이 aborted. 시장가/멱등은 이 영역 자체 책임 |
| **Rebalance Plan Agent** (`rebalance_plan_steps`, `05-rebalance-plan.md`) | 입력 | 분할 리밸런싱 후보(ticker/qty/limit_price/round) 제공 → `OrderRequest` 원천 (연결 미구현) |
| **Account/KIS Sync Agent** (`account_snapshots`, `01-account-kis-sync.md`) | 입력 | `available_cash_krw`(예수금) + broker `is_healthy` 신선도 제공 |
| **Allocation Agent** (`allocation_selections`, `03-allocation.md`) | 상류 | 목표비중 없이 후보 금지 — 후보의 정당성 원천 |
| **Audit / Lessons** (`audit_logs`, `lessons`, `09-lessons-memory.md`) | 출력 | 모든 차단/제출/불확실을 audit 로 남기고, 실패는 lesson 으로 누적(메모리 성장) |
| **broker factory/adapters** (`factory.py`, `kis_adapter.py`) | 도구 | mode→adapter 주입, live 하드락, 실제 KIS 전송 |
| **승인 UI Agent** (`10-ui-ux.md`) | 출력(미구현) | 승인대기/제출/체결 조회 화면 — 현재 부재 |

RDB(`orders`,`audit_logs`)=주문/금액 truth, Vector=근거검색(evidence), Graph=관계설명(decision↔evidence)으로 분리. 이 영역은 **RDB truth** 만 쓰고 쓰며, 두뇌(reasoning)는 Claude+메모리(Anthropic API 미사용).
