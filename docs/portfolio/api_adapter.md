# Portfolio OS — KIS API Adapter 구조

> 목적: 한국투자증권 Open API 를 **하나의 인터페이스 뒤로 격리**하여
> paper(모의)/live(실전) · 국내/미국 시장 차이를 흡수하고, 나머지 모듈이 broker 세부에 의존하지 않게 한다(§ adapter 분리, §37 경계).
> 자격증명은 전부 `.env`. 코드 하드코딩 금지(§26).

---

## 1. 계층

```text
모듈(strategy/portfolio/risk) ──► BrokerPort (추상 인터페이스)
                                      ├─ KisPaperAdapter   (모의투자 도메인)
                                      ├─ KisLiveAdapter    (실전 — CEO 승인 후만)
                                      └─ MockAdapter       (오프라인 테스트, 결정론적)
                                            │
                                            └─ KisHttpClient (토큰·서명·rate limit·재시도)
```

`KIS_MODE` 환경변수 1개로 어떤 adapter 를 주입할지 결정(DI). 기본값 `paper`.

---

## 2. BrokerPort 인터페이스 (의사 시그니처)

```python
class BrokerPort(Protocol):
    # 인증
    def ensure_token(self) -> Token: ...            # 만료 시 자동 갱신, 캐시

    # 조회 (read)
    def get_balance(self, account: Account) -> list[BalanceLine]: ...
    def get_quote(self, instrument: Instrument) -> Quote: ...
    def get_fx_rate(self, pair: str = "USDKRW") -> Decimal: ...
    def get_open_orders(self, account: Account) -> list[Order]: ...
    def get_fills(self, account: Account, since: datetime) -> list[Fill]: ...

    # 주문 (write) — CEO 승인된 후보만 호출됨
    def place_order(self, req: OrderRequest) -> OrderAck: ...   # client_order_id 필수
    def cancel_order(self, broker_order_id: str) -> CancelAck: ...

    @property
    def mode(self) -> Literal["paper", "live"]: ...
    @property
    def is_healthy(self) -> bool: ...               # 장애 시 False → 루프 ABORT
```

**국내 vs 미국**: 같은 인터페이스, adapter 내부에서 KIS 의 `tr_id`(국내/미국 주문·조회 코드)와 시장 구분을 매핑. 호출측은 `instrument.market` 만 넘긴다.

---

## 3. KIS 매핑 (구현 시 채울 표)

| 기능 | KIS 엔드포인트(개념) | tr_id 예 | 비고 |
|---|---|---|---|
| 토큰 발급 | `/oauth2/tokenP` | — | app_key/secret → access_token, 만료 캐시 |
| 국내 잔고 | `/uapi/domestic-stock/v1/trading/inquire-balance` | TTTC8434R(실)/VTTC8434R(모) | mode별 tr_id 분기 |
| 미국 잔고 | `/uapi/overseas-stock/v1/trading/inquire-balance` | TTTS3012R/VTTS3012R | 통화 USD |
| 국내 현재가 | `/uapi/domestic-stock/v1/quotations/inquire-price` | FHKST01010100 | |
| 국내 투자자 매매동향(분산축) | `/uapi/domestic-stock/v1/quotations/inquire-investor` | FHKST01010900 | ✅공식 다수 소스 교차검증(mode 무관). read-only. 응답 output[]: `stck_bsop_date`/`frgn_ntby_qty`(외국인)/`orgn_ntby_qty`(기관계)/`prsn_ntby_qty`(개인)+매수거래량. **외국인/기관/개인 3주체만**(연기금/프로그램 등 세부 주체는 본 TR 미제공). `broker/kis_investor.py` → `investor_flows` 적재 |
| 미국 현재가 | `/uapi/overseas-price/v1/quotations/price` | HHDFS00000300 | 거래소 코드 필요 |
| 환율 | (해외주문 응답 내 환율 or 별도) | — | 없으면 외부 환율 소스 + flag |
| 국내 주문 | `/uapi/domestic-stock/v1/trading/order-cash` | 매수 TTTC0802U / 매도 TTTC0801U(실), V-prefix(모의) | ✅실전 코드 검증 / 모의 코드 미검증 · hashkey 서명 |
| 미국 주문 | `/uapi/overseas-stock/v1/trading/order` | ⚠️ TTTT1002U vs **JTTT1002U** 출처 엇갈림 | **미검증** — 거래소(NASD/NYSE/AMEX)·매수매도·정정취소별 코드 전수 확인 필요 |
| 체결 조회 | `inquire-ccnl` 류 + **WebSocket 실시간 체결통보 H0STCNI0(실)/H0STCNI9(모)** | | 미체결/부분 추적은 push 권장 (Wave1 §7) |

> 위 코드값은 **구현 단계에서 KIS 공식 문서로 검증 후 selector_registry 처럼 한 곳에 고정**한다. 임의 추측 금지(§9 출처). Wave 1 검증 결과는 아래 §7.

---

## 4. KisHttpClient 책임 (안전 핵심)

- **토큰 캐시 + 자동 갱신** (만료 전 재발급, 동시성 안전).
- **hashkey 서명** (주문 위변조 방지, KIS 요구).
- **Rate limit** (초당 호출 제한 준수 → 토큰 버킷).
- **재시도** (조회는 멱등 → 3회 백오프 / **주문은 재시도 금지**, 대신 상태 재조회).
- **타임아웃 → is_healthy=False** → 의사결정 루프가 ABORT(§ API 장애 시 주문 중단).
- **로그 마스킹**: app_key/secret/token/계좌번호 절대 로그에 남기지 않음(§26).

---

## 5. Idempotency (중복 주문 방지)

- 모든 `place_order` 는 `client_order_id` 필수(우리가 생성, ULID).
- DB `orders.client_order_id UNIQUE` + adapter 전송 전 "이미 submitted?" 확인.
- 네트워크 불확실(전송됐는지 모름) → **재전송 금지**, `get_open_orders`/`get_fills` 로 실제 상태 확인 후 진행.

---

## 6. Paper / Mock 우선

- 초기 전 기능은 `MockAdapter`(고정 잔고/가격) + `KisPaperAdapter`(모의투자 계좌)로만 동작.
- `live` adapter 는 코드 존재해도 **`KIS_MODE=live` + CEO 승인 체크리스트** 없이는 주입 안 됨.

스켈레톤: [../../main_mission/portfolio_os/broker/](../../main_mission/portfolio_os/broker/)

---

## 7. Wave 1 검증·개선 (broker-chief 자료조사 반영, 2026-06-19)

출처: [KIS 공식 GitHub](https://github.com/koreainvestment/open-trading-api), KIS Throttling 사례, WebSocket 체결통보 예제. 검증등급 구분 표기.

### 7.1 환경 도메인 (✅ 다수 일치 — §3 표 보강)
| | 실전(prod) | 모의(vps/paper) |
|---|---|---|
| REST | `https://openapi.koreainvestment.com:9443` | `https://openapivts.koreainvestment.com:29443` |
| WebSocket | `ops.koreainvestment.com:21000` | `ops.koreainvestment.com:31000` |
| 체결통보 tr_id | H0STCNI0 | H0STCNI9 |

→ 구현 시 `broker/kis_endpoints.py` 상수로 고정, `KIS_MODE`로 분기. (개선안: 도메인 상수화)

### 7.2 Rate limit (✅ — paper/live 분리 필수)
- **실전 초당 20건 / 모의 초당 5건**. 초과 시 에러코드 **EGW00201**.
- 토큰 재발급 **1분당 1회** 제한. → 토큰버킷을 mode별(paper=5, live=20)로, 25% 헤드룸 운영. 만료 5분 전 선제 갱신. EGW00201 수신 시 즉시 재시도 금지·백오프.

### 7.3 부분체결 추적 = WebSocket push 일원화 (개선안, plan_required)
- 현재 `get_open_orders`/`get_fills` **폴링**만 → 모의 5/s rate limit 잠식.
- `BrokerPort`에 선택적 `subscribe_order_updates(account) -> Iterator[OrderUpdate]` 추가. `OrderUpdate{broker_order_id, orig_broker_order_id(원주문), exec_state(주문/정정/취소/거부 vs 체결), cum_filled_qty, last_fill_qty/price, rejected}`. 폴링은 reconciliation 백업으로만.

### 7.4 Idempotency 강화 (즉시반영 §5 + plan_required 상태머신)
- `client_order_id → request_payload_hash` 함께 저장 → **같은 id + 다른 payload = reject**(클라이언트 버그 탐지).
- DB `orders`에 **`in_doubt` 상태** 추가: place_order 응답 미수신 시 자동 진입, 해소는 오직 `get_open_orders`/체결통보 재조회로만(**재전송 금지** 코드 강제).
- idempotency key **TTL**(거래일 단위) 명시.

### 7.5 환율
- KIS 단독 FX 엔드포인트 존재 여부 **미확인** → 외부 환율 소스 + `stale_fx_flag` 의존 설계 유지가 안전.
