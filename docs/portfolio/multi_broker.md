# 멀티 브로커 아키텍처 (키움 등 확장) — Portfolio OS

> CEO 원칙: 특정 증권사 1개에 종속 금지. **KIS 전용 코드에 키움 예외처리 추가 금지** —
> 증권사별 API는 **adapter(BrokerPort)** 에서 다르게 처리하고, 내부 DB/UI는 **동일 표준 snapshot** 구조를 쓴다.

---

## 1. 기존 구조 = 이미 멀티 브로커 토대
`broker/port.py`의 **`BrokerPort` Protocol**이 이미 broker-agnostic 어댑터 인터페이스다 (KIS 전용 아님):
`mode · is_healthy · ensure_token · get_balance · get_quote · get_fx_rate · get_open_orders · get_fills · place_order · cancel_order`.
구현체: `MockAdapter`, `KisPaperAdapter`, `KisLiveAdapter`. **여기에 `KiwoomRestAdapter`를 추가**하면 된다 (KIS 코드 수정 아님).

CEO `BrokerAdapter` ↔ 기존 `BrokerPort` 매핑:
| CEO | BrokerPort |
|---|---|
| get_accounts | (factory + accounts 테이블) |
| get_cash_balance / get_positions | `get_balance` |
| get_price | `get_quote` |
| get_orderable_cash | `get_balance`(예수금) |
| place_order / get_order_status / cancel_order | `place_order` / `get_fills`,`get_open_orders` / `cancel_order` |
| refresh_token / health_check | `ensure_token` / `is_healthy` |

표준 출력(AccountSnapshot/PositionSnapshot/PriceSnapshot/OrderIntent/OrderEvent)은 **DB가 동일** — KIS든 키움이든 `account_snapshots`/`holdings(position)`/`quotes(price)`/`orders` 같은 테이블에 저장.

## 2. DB (적용 완료)
- `accounts.broker` 컬럼 (`kis|kiwoom|manual|paper`, 없으면 kis 취급) — SQLite + PG.
- `broker_credentials(account_index, broker, key_ref, secret_ref, token_status, token_expires_at, UNIQUE(account_index,broker))` — **평문 키/시크릿 저장 금지**, `.env`/secret 참조(ref)만. KIS·키움 credential **혼용 금지**(broker별 행 분리).

## 3. factory 확장 (다음 단계, 코드)
`get_broker(account_index)`가 `accounts.broker`로 분기:
```
broker = accounts.broker (default 'kis')
if broker == 'kiwoom': return KiwoomRestAdapter(account_index)
else: (기존 KIS dispatch)
```
KIS 경로 무변경. 키움은 별도 adapter.

## 4. 키움 REST API 조사 결과 (1차 판단)
- **A. 키움 REST API 우선** — 키움이 REST 기반 API(시세·잔고·주문)를 제공. REST면 Ubuntu 서버 backend worker에서 직접 호출 가능 → **이 방식으로 `KiwoomRestAdapter` 구현 권장**. OAuth 토큰 발급 + 잔고/보유종목/현재가 조회를 REST로 확인 후 진행.
- **B. 키움 Open API+ (OCX)** — 전통적 Windows OCX 기반. **Ubuntu 서버 직접 운영 불가** → Windows bridge/gateway 필요(운영 복잡도 큼). 실시간 조건검색 등은 강하나 **1차 범위에서 제외**.
- **판단**: 잔고/보유종목/현재가가 REST로 되면 REST adapter만으로 1차 목표 달성. Windows bridge는 **불필요**(REST 가능 가정). REST 불가 기능(실시간 조건검색 등)이 필요해질 때만 별도 gateway 검토 → 그때 CEO 별도 보고.

## 5. 1차 목표 (주문 제외)
키움 계좌 연결 → 토큰 검증 → 잔고 조회 → 보유종목 조회 → **DB snapshot 저장**(KIS와 동일 테이블) → Portfolio OS 화면 표시(KIS 계좌와 동일 UI, adapter만 다름). **주문은 다음 단계** (risk gate·승인·account PIN 재인증·live 하드락 유지 후).

## 6. 보안 (KIS와 동일)
App Key/Secret/Token 로그 금지 · 프론트에서 키움 직접 호출 금지(서버 route/worker만) · 계좌번호 full 노출 금지 · account_id별 PIN 유지 · live 주문 기본 차단 · broker별 credential scope 분리 · KIS↔키움 혼용 금지.

## 7. 멀티 계좌 isolation (불변)
키움 계좌도 다른 계좌처럼 들어오되 **account_id 기준 분리**: 계좌별 policy/selected allocation/PIN/risk gate/snapshot 분리. 전문 Agent memory만 공통 성장, 최종 적용은 계좌별 policy 우선.

## 8. ⚠️ 외부 의존성 (실 구현 전제)
실제 키움 잔고 동기화는 **사용자의 키움 REST app key/secret 발급 + 모의투자 신청 + 공식 엔드포인트 spec**이 있어야 구현·검증 가능. 현재는 인터페이스·DB·factory 분기·UI 선택·`KiwoomRestAdapter` 스텁까지 스캐폴딩하고, 키 발급 후 endpoint/토큰/조회를 채운다.

## 8.1 키움 Track 상태 = **external key 대기** (실동기화 미완)
현재 = **구조만 완료**. 정확한 표현: "키움 멀티 브로커 구조 준비 완료. 실제 잔고/보유종목 동기화는 키움 REST app key/secret 발급 후 진행."
키 발급 후 확인/구현할 endpoint (대기 목록):
- [ ] 키움 REST API 키 발급 + **모의투자 가능 여부**
- [ ] OAuth **토큰 endpoint**(발급/갱신)
- [ ] **예수금/잔고 endpoint**
- [ ] **보유종목 endpoint**
- [ ] **현재가 endpoint**
- [ ] rate limit / error code
- 주문 endpoint = **2차 단계**(잔고/가격 검증·risk gate·승인·PIN·live 하드락 후). 현재 `place_order`는 NotImplemented 차단 유지.
- OpenAPI+ OCX 방식 = Ubuntu 운영 복잡도 큼 → **우선 제외**.
실동기화 순서: 토큰검증 → 계좌확인 → 예수금 → 보유종목 → 현재가 → DB snapshot → 화면표시 → paper 주문 → (별도 hard lock 후) 실전.

## 9. 다음 작업
1. `KiwoomRestAdapter`(BrokerPort 구현) — 키 발급 후 토큰/잔고/보유종목/현재가 REST 채우기.
2. factory broker 분기 + sync_job이 account.broker로 adapter 선택.
3. UI: 계좌 연결 화면 증권사 선택(한투/키움/수동/Paper) + 키움 계좌 카드(KIS와 동일 표시).
4. account_id isolation 테스트(키움·KIS 계좌 snapshot 안 섞임).
