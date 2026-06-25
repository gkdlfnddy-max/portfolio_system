# 국채 ETF — 실 지표 연동 + 후보 비교 (govbond_etf)

> CEO 목적: 국채 ETF 후보(운용 수단)에 **실 지표를 연동한 비교표**를 제공해, 사용자가 외워서
> 고르는 게 아니라 시스템이 **거시·계좌·확정안 기준으로 설명**하게 한다.
> 코드: [main_mission/portfolio_os/govbond_etf.py](../../main_mission/portfolio_os/govbond_etf.py)
> 관련: [bond_policy.md](bond_policy.md) · [data_architecture.md](data_architecture.md) · [safety_rules.md](safety_rules.md)

---

## 0. 불변 원칙 (정직성)

- **가짜 지표 0.** 미연동 항목은 `unknown` 으로 정직 표기. 임의 수치 금지.
- 국채 ETF 는 **방어자산(현금+국채)을 담는 운용 수단(상품)**일 뿐 — **상품 추천이 아니라 추천(설명)**.
- 비교는 **제시**일 뿐. 특정 ETF(C안) 바로 확정 안 함.
- **자동 주문 0 · policy 변경 0 · KIS read-only · secret 0.**

---

## 1. ETF universe (government_only, 8종)

| ticker | 이름 | 지역 | duration_bucket | 가격/거래량 연동 |
|---|---|---|---|---|
| SHY | iShares 1-3Y Treasury | 미국 | short | **미연동(unknown)** |
| IEF | iShares 7-10Y Treasury | 미국 | intermediate | **미연동(unknown)** |
| TLT | iShares 20Y+ Treasury | 미국 | long | **미연동(unknown)** |
| 153130 | KODEX 단기채권 | 한국 | short | **KIS 국내 일봉 실연동** |
| 114260 | KODEX 국고채3년 | 한국 | short | **KIS 국내 일봉 실연동** |
| 471230 | KODEX 국고채10년액티브 | 한국 | intermediate | **KIS 국내 일봉 실연동** |
| 439870 | KODEX 국고채30년액티브 | 한국 | long | **KIS 국내 일봉 실연동** |
| 451530 | TIGER 국고채30년스트립액티브 | 한국 | long | **KIS 국내 일봉 실연동** |

정성 사실(항상 표기): `region` · `duration_bucket` · `tracking_index`(추적지수) · `hedged_or_unhedged`(환헤지 여부, KR=원화·환노출 없음 / US=달러·환노출).

---

## 2. 연동 / 미연동 표 (정직)

| 항목 | KR 5종 | 미국 3종 |
|---|---|---|
| price / volume | **실연동**(KIS 국내 일봉, price_history 재사용) | unknown (KIS 해외 미연동) |
| recent_volatility | 가격 있으면 계산(일간수익률 표준편차%) | unknown |
| expense_ratio / duration_years / yield | **unknown**(무료 KR API 제한) | **unknown** |

`data_available` = **가격 실연동 여부**(정성 사실과는 별개로 항상 존재).

---

## 3. 사용

```bash
# KR 국채 ETF 5종 가격/거래량 KIS 국내 일봉 실적재 (read-only, 주문 0)
python -m main_mission.portfolio_os.govbond_etf --fetch --account 1

# 단일 ETF 프로필
python -m main_mission.portfolio_os.govbond_etf --profile 153130

# 후보 비교표 (거시·계좌 적합성 반영)
python -m main_mission.portfolio_os.govbond_etf --account 1 --duration short --region 한국
```

내부적으로 `price_history.KisDailyBarFetcher` 를 재사용한다(별도 KIS 호출 코드 없음).

---

## 4. 비교표(compare_govbond_candidates) 항목

후보별로:

- **분류**: 단기/중기/장기 · 한국/미국
- **역할**: 방어(단기) / 완충(중기) / 금리대응·베팅(장기)
- **장점 / 리스크**: duration_bucket 기준(장기 = 금리상승 평가손·변동성 큼)
- **현 거시 적합성(macro_fit)**: `rate_regime` (macro_connect 실데이터 → elevated/uncertain/unknown).
  인상·고금리(elevated) → 단기 적합 / 장기 주의. 미연동이면 **판단보류(정직)**.
- **계좌 목적 적합성(purpose_fit)**: 확정안(있으면 단일 진실) → 없으면 프로필. 선호 듀레이션·성향 반영.
- **추천 강도**: 거시+계좌 적합성 + 데이터 품질 종합(미연동은 강도 ↓).
- **데이터 품질**: price/volume/변동성/보수율/듀레이션/수익률 + confidence + last_verified_at + source.
- **대안**: 같은 지역 내 다른 만기대 후보.
- **제외 사유**: 필터로 빠진 후보를 사유와 함께 정직 기록.

**장기국채 변동성 경고**(`long_bond_volatility_warning`)는 항상 동봉되며, 장기 후보의 `risks`에도 반영.

---

## 5. rate_regime 도출

`macro_connect.macro_to_portfolio()` 의 실데이터 신호 사용:

- 고금리 신호(`high_rate_*`) 또는 인플레(`high_inflation`) 또는 `tilts.bond_duration < 0` → **elevated**.
- 거시 연동·신호 없음 → **uncertain**.
- 거시 미연동 → **unknown**(적합성 = 판단보류, 가짜 점수 금지).

---

## 6. 테스트

[tests/test_govbond_etf.py](../../main_mission/portfolio_os/tests/test_govbond_etf.py) — KR 가격 실적재(fake fetcher 주입),
미국/보수율/듀레이션 unknown 정직, 거시·계좌 적합성, 장기채 경고, 자동주문/policy 0.
