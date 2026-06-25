# Portfolio OS — 핵심 안전 원칙 (SSOT)

> 본 문서는 투자 도메인의 **절대 규칙**. 모든 코드/hook/리스크 게이트가 여기에 종속.
> 위반은 silent 하게 통과될 수 없으며, 대부분 risk-chief 의 **hard-block** 대상.

---

## A. 자동화 안전 (구조)

| # | 규칙 | 강제 위치 |
|---|---|---|
| A1 | **완전 자동매매 금지**. 기본 = 추천/승인형 | 루프 구조: T8 승인 없이는 T9 호출 불가 |
| A2 | 실전(`live`) 진입은 CEO 승인 + 체크리스트 | `KIS_MODE=live` 게이트 + 별도 확인 |
| A3 | API 장애 시 주문 중단 | broker.is_healthy=False → ABORT |
| A4 | 같은 주문 중복 실행 방지 | `client_order_id` UNIQUE + 전송 전 확인 |
| A5 | 모든 주문 DB+로그 추적 | `orders` + `audit_logs` 필수 |
| A6 | 자격증명 코드/DB/로그 노출 금지 | `.env` 전용 + 로그 마스킹 |

## B. 리스크 한도 (수치 — `risk_limits` 테이블 SSOT)

| name | 기본값(초안) | hard | 의미 |
|---|---|---|---|
| `cash_min_pct` | 10% | ✅ | 현금 최소 비중. 그 아래로 떨어지는 매수 차단 |
| `single_name_max_pct` | 20% | ✅ | 단일 종목 최대 비중 |
| `short_total_max_pct` | 10% | ✅ | 인버스/숏 ETF 총합 (보험 수준) |
| `leverage_total_max_pct` | 15% | ✅ | 레버리지 ETF 총합 |
| `daily_loss_stop_pct` | 5% | ✅ | 일일 손실 한도 → 추가 매수 중단 |
| `single_order_max_pct` | 5% | ✅ | 1주문 최대 (총자산 대비) → 슬리피지 방지 |
| `max_orders_per_session` | 20 | ⚠️ | 1세션 주문 수 상한 |

> 기본값은 **초안**. CEO 가 컨셉 입력 시 조정 가능(예: "숏은 보험 수준" → short_total_max_pct 낮게).

## C. 시간/체결 안전

| # | 규칙 |
|---|---|
| C1 | 장 시작 직후 **15분**(KRX 개장 변동성 최고 구간) / 장 마감 직전 10분 주문 제한. 마감 단일가(15:20~) 구간은 **지정가 강제** |
| C2 | 미체결 → 추적 + 재조회. 일정 시간 후 자동 취소 옵션(CEO 정책) |
| C3 | 부분체결 → 잔량 추적, 비중 재계산은 실제 체결분 기준 |
| C4 | 시장가 vs 지정가 정책: 변동성 큰 종목/개장 직후는 지정가 권장 |

## D. 환율 안전

| # | 규칙 |
|---|---|
| D1 | 미국 자산 평가·주문은 **환율 반영 필수**. fx_rate 를 proposal/order 에 기록 |
| D2 | 환율 stale(오래된 값) → flag, 큰 환변동 시 재조회 |

---

## 리스크 게이트 실행 순서 (T7)

```text
거래 리스트 입력
 → 각 거래 적용 후 예상 비중 계산
 → cash_min_pct 위반?           → fail
 → single_name_max_pct 위반?    → fail
 → short_total / leverage 위반?  → fail
 → single_order / per_session?  → fail (or 분할 제안)
 → 시간대 제한(C1)?             → fail / 지연 제안
 → 전부 통과 → pass + 요약
```

**fail 이면 주문 후보를 만들지 않고**, 위반 사유 + 대안(예: 일부만 매수)을 CEO 에 제시한다.

---

## 완료 기준과의 연결 (§9 CEO 완료 기준)

- "주문 전 리스크 가드가 작동하는가" → B/C/D + T7 게이트로 충족.
- "API 장애나 실수 주문이 조용히 발생하지 않는가" → A3/A4/A6 + audit_logs 로 충족.

연결: [hook_design.md](hook_design.md) · [db_schema.md](db_schema.md)(risk_limits) · [task_tree.md](task_tree.md)(T7)

---

## Wave 1 개선 — 리스크 한도 재검토 (risk-chief 자료조사, 2026-06-19)

> CEO 지시: 기본값을 그대로 확정하지 말 것. 아래는 근거 기반 재검토. **수치 하향/확정은 CEO 승인 필요(CEO-GATE)**. risk-chief는 검증·제안만.

### 한도 재검토표
| 한도 | 현재 | 제안 | 근거 | 분류 |
|---|---|---|---|---|
| cash_min_pct | 10% | 유지 10% (변동성장 동적 15% advisory) | 일반 유동성 완충 타당 | advisory 룰만 CEO |
| single_name_max_pct | 20% | **15%(보수 10%)** | 기관 기준 단일종목 5~10%, 10%↑는 집중리스크. 20%는 과도 | **CEO-GATE** |
| short_total_max_pct | 10% | **"보험 수준"이면 5%** | 인버스 ETF는 decay로 장기 헤지효과 침식. 보험이면 5%로 충분 | **CEO-GATE (보험 정의)** |
| leverage_total_max_pct | 15% | **10%** | 레버리지 decay. 15%는 단기전술로도 높음 | **CEO-GATE** |
| single_order_max_pct | 5% | 유지 5% | 포트폴리오 heat 5% 캡과 정합 | 유지 |
| daily_loss_stop_pct | 5% | 유지 + **max_drawdown_stop_pct 10~12% 신설** | 일일 단일한도는 trending(매일 4%×3일=12%)에 취약 | 누적한도 수치만 CEO |
| drift_band_pct | 3% | **min(절대5%p, 상대25%) 결합** | 3%는 큰 자산엔 과민·작은 자산엔 둔감 (5/25 rule) | plan_required |
| open_blackout | 10분 | **15분(반영함)** | KRX 개장 15분 변동성 최고 | ✅즉시반영 |

### 신규 한도 제안 (CEO 승인 후 risk_limits 추가)
- `max_drawdown_stop_pct`(고점 대비 누적 10~12%, hard) — 누적 드로다운 서킷브레이커.
- `leveraged_inverse_combined_max_pct`(숏+레버리지 합산 15%, advisory).
- `leveraged_hold_days_max`(레버리지/인버스 보유일 상한, 예 10거래일 — decay 방어). **데이터 없으면 fail-closed**.
- `fx_stale_max_min`(환율 stale 15분 초과 시 주문 보류, advisory).

### 한국장/미국장/환율/미체결 반영 (즉시반영 — 정책 명문화)
- **KRX**: 정규장 09:00~15:30 외 blackout. 개별종목 **VI 발동 종목 주문 보류**(advisory). KOSPI 시장 서킷브레이커(±8%↑) 발동 시 **세션 전체 ABORT**(hard, A3 연장).
- **미국장**: 별도 야간 세션. **미국장 종가→한국장 시가 갭 전이** 고려, 미국 보유분 큰 날 한국장 개장 직후 추가매수 보류.
- **환율**: 미국 자산은 **유효노출 = 시장가치 × 환변동 버퍼**(초기 ×1.1 보수)로 한도 체크. USD/KRW는 위기 시 주가와 양의 상관 → 미국 집중은 보수적 한도.
- **미체결**: 한도는 **실제 체결분 기준** 재계산(C3). 미체결 잔량은 "잠재 노출"로 별도 추적, pending 합산이 한도 초과 시 신규 주문 차단. stale 미체결 자동취소(CEO 정책).

### 레버리지/인버스 volatility decay (즉시반영 — 명문화)
레버리지/인버스 ETF는 일일 리밸런싱으로 **경로 의존**, 횡보장에서 기초지수 제자리여도 손실(decay) 누적. → **단기 전술용, 장기보유 금지** 원칙. 보유일 상한 게이트는 데이터 없으면 fail-closed.
