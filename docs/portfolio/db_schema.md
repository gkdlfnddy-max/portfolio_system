# Portfolio OS — DB 스키마 초안

> DB: 로컬 SQLite (`data/portfolio.sqlite3`) — 추후 `portfolio_os_db` (PostgreSQL 15+) 승격 가능.
> SSOT(§13): 운영 상태는 전부 여기. 마이그레이션 [../../main_mission/portfolio_os/migrations/](../../main_mission/portfolio_os/migrations/).
> 모든 금액은 통화 명시 + KRW 환산 컬럼 분리. 모든 주문은 추적 가능(§40 안전).

---

## 1 entity = 1 table (§14)

| 테이블 | 역할 |
|---|---|
| `accounts` | KIS 계좌 (mode=paper/live, 통화, 별칭). **자격증명은 저장 안 함** |
| `instruments` | 종목 마스터 (티커, 시장 KRX/NASDAQ, 자산군, 레버리지/인버스 플래그) |
| `balances` | 잔고 스냅샷 라인 (계좌×종목×시점, 수량, 평가액, 통화, KRW 환산) |
| `quotes` | 현재가/환율 캐시 (종목, price, currency, ts, stale 여부) |
| `investment_concepts` | CEO 가 입력한 투자 컨셉(원문 + 파싱 결과) |
| `target_weights` | 컨셉 → 목표 비중 (자산군/종목/현금/숏 비중, 합 100%) |
| `risk_limits` | 리스크 한도 SSOT (현금 최소, 단일종목 최대, 숏 총합 등) |
| `rebalance_proposals` | 리밸런싱 제안 (drift + 거래 리스트 + 근거 + 상태) |
| `proposal_trades` | 제안 내 개별 거래 라인 (종목, side, 수량, 추정금액) |
| `risk_checks` | 리스크 검증 결과 (proposal별, pass/fail, 위반 enum) |
| `approvals` | CEO 승인 결정 (proposal별, decision, 사유, 시각) |
| `orders` | 주문 (client_order_id idempotency, 상태머신, mode) |
| `fills` | 체결 (order별 부분체결 라인) |
| `portfolio_snapshots` | 시점별 포트폴리오 비중/평가액 스냅샷 (성과 추적) |
| `audit_logs` | 모든 중요 행위 감사 로그 (who/what/when/payload) |
| `tasks` | task 트리 (parent_task_id, input/output/success/fallback) |
| `lessons` | 회고/lesson (raw→reflection→candidate→knowhow 승격) |
| `memory_docs` / `memory_links` / `memory_embeddings` | 메모리 인프라 (lesson/노하우 보존) |

---

## 핵심 테이블 컬럼 초안

### accounts
```
id BIGSERIAL PK
alias TEXT                       -- 사람이 읽는 별칭
mode TEXT NOT NULL               -- paper | live
broker TEXT NOT NULL DEFAULT 'KIS'
base_currency TEXT DEFAULT 'KRW'
account_ref TEXT                 -- 계좌 식별 해시(평문 계좌번호 저장 금지)
is_active BOOLEAN DEFAULT TRUE
created_at / updated_at
CHECK (mode IN ('paper','live'))
```

### instruments
```
id BIGSERIAL PK
ticker TEXT NOT NULL             -- '005930', 'AAPL', 'SOXL'
market TEXT NOT NULL             -- KRX | NASDAQ | NYSE | AMEX
name TEXT
asset_class TEXT                 -- stock | etf | cash
currency TEXT NOT NULL           -- KRW | USD
sector TEXT
is_leveraged BOOLEAN DEFAULT FALSE
is_inverse BOOLEAN DEFAULT FALSE
leverage_factor NUMERIC DEFAULT 1.0
UNIQUE (ticker, market)
```

### target_weights  (컨셉 → 비중)
```
id BIGSERIAL PK
concept_id BIGINT REFERENCES investment_concepts(id)
scope TEXT                       -- asset_class | sector | instrument | cash | short
key TEXT                         -- '반도체' | 'AAPL' | 'cash'
target_pct NUMERIC NOT NULL      -- 0~100
rationale TEXT
created_at
-- 동일 concept 내 합계 = 100% (애플리케이션 검증 + 뷰)
```

### risk_limits  (SSOT)
```
id BIGSERIAL PK
name TEXT UNIQUE                 -- cash_min_pct | single_name_max_pct | short_total_max_pct | leverage_total_max_pct | daily_loss_stop_pct
value NUMERIC NOT NULL
hard BOOLEAN DEFAULT TRUE        -- true=hard-block, false=advisory
updated_by TEXT
updated_at
```

### rebalance_proposals
```
id BIGSERIAL PK
session_task_id BIGINT REFERENCES tasks(id)
account_id BIGINT REFERENCES accounts(id)
concept_id BIGINT REFERENCES investment_concepts(id)
status TEXT NOT NULL DEFAULT 'draft'  -- draft|risk_pending|risk_failed|approval_pending|approved|rejected|executed|expired
drift JSONB                      -- 축별 drift
fx_rate NUMERIC                  -- 적용 환율(USDKRW)
rationale TEXT
created_at / updated_at
CHECK (status IN ('draft','risk_pending','risk_failed','approval_pending','approved','rejected','executed','expired'))
```

### orders  (상태머신 + idempotency)
```
id BIGSERIAL PK
proposal_id BIGINT REFERENCES rebalance_proposals(id)
instrument_id BIGINT REFERENCES instruments(id)
client_order_id TEXT NOT NULL UNIQUE   -- 중복 실행 방지 (안전 §9)
broker_order_id TEXT                    -- KIS 발급 id
mode TEXT NOT NULL                      -- paper | live
side TEXT NOT NULL                      -- buy | sell
qty NUMERIC NOT NULL
order_type TEXT                         -- market | limit
limit_price NUMERIC
currency TEXT NOT NULL
status TEXT NOT NULL DEFAULT 'created'  -- created|risk_passed|approved|submitted|partial|filled|rejected|canceled|aborted
filled_qty NUMERIC DEFAULT 0
avg_fill_price NUMERIC
submitted_at / created_at / updated_at
CHECK (mode IN ('paper','live'))
CHECK (side IN ('buy','sell'))
CHECK (status IN ('created','risk_passed','approved','submitted','partial','filled','rejected','canceled','aborted'))
```

### portfolio_snapshots
```
id BIGSERIAL PK
account_id BIGINT
captured_at TIMESTAMP
total_value_krw NUMERIC
cash_pct / long_pct / short_pct NUMERIC
weights JSONB                    -- 자산군/섹터/종목별 비중
fx_rate NUMERIC
source TEXT                      -- live_fetch | post_fill | scheduled
```

### audit_logs
```
id BIGSERIAL PK
actor TEXT                       -- CEO | strategy-chief | broker-chief | system
action TEXT                      -- propose | risk_block | approve | order_submit | abort ...
entity_type TEXT / entity_id BIGINT
mode TEXT
payload JSONB
created_at
```

---

## 안전 관련 제약 (DB 레벨)

- `orders.client_order_id` **UNIQUE** → 같은 주문 중복 전송 차단.
- `orders.mode` / `accounts.mode` 불일치 금지(애플리케이션 + 트리거 검증).
- 부분 인덱스: `CREATE UNIQUE INDEX ... ON orders(client_order_id)`.
- 자격증명(API key/secret/계좌번호 평문)은 **어떤 테이블에도 저장 안 함** → `.env` 전용.

마이그레이션 초안: [001_init.sql](../../main_mission/portfolio_os/migrations/001_init.sql)
