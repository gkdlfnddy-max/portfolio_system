# 하락 징후 분석 엔진 (Pre-Decline Signal Engine)

> 섹터/지수/종목의 **하락 전 특징을 과거 데이터로 분석**하고, 그런 징후가 보이기 시작하면
> **포트폴리오를 보수적으로(현금/방어 비중↑) 전환하도록 제안**한다.
> 제안만 — 자동매매 없음(사람 승인). Anthropic API 미사용(규칙 신호 + Claude+메모리 성장).

> **v2 확장(6축 + 메타인지 + 성장):** 하락은 여러 원인에서 온다. 단일 기술지표가 아니라
> **6축으로 입체 분석**하고, 엔진이 **스스로 어느 축이 신뢰 가능한지(데이터 유무 + 과거 적중
> 이력)를 가늠해 가중**하며 **쓸수록 성장**한다. 상세는 §9~§12.

---

## 1. 비전 (CEO)

- 종목/섹터/지수의 하락 **직전 특징**을 과거 일봉으로 찾아낸다.
- 그 징후가 다시 보이면 **보수적 전환(현금/방어 band↑, 위험자산↓, 헤지)** 을 *제안*한다.
- 관심 종목별 **노하우를 메모리에 누적**(성장)해 다음 판단에 재사용.
- 타이밍 판단은 **일/주 단위**. 발끝(최저점) 아니어도 "무릎" 회피 목적.

지능 = 결정론 규칙 신호(`decline_signals`) + Claude+메모리(노하우 누적). **API 호출 0.**

---

## 2. 신호 정의 (`decline_signals.py`, 순수 함수)

입력: 가격이력 시계열 `list of {date, close, high, low, volume}` (오래된→최신).
각 신호 → `{name, fired: bool, value, severity(0~1), detail}`.

| 신호 | 의미(하락 전 특징) | 발화 조건(기본 임계, config 의미) |
|---|---|---|
| `overextended_ma200` | 장기선 대비 과열(이격) | 200일선 대비 +20% 이상 (없으면 120/60 폴백) |
| `rsi_overbought` | 과매수 | RSI(14) ≥ 70 (80+ severity 만점) |
| `volatility_spike` | 변동성 급증(추세 불안정) | 단기 ATR%/장기 ATR% ≥ 1.5x (ATR 불가 시 표준편차 폴백) |
| `ma_trend_weakening` | 상승추세 둔화/전환 | 20일선 5일 기울기 ≤ 0% |
| `deadcross_proximity` | 데드크로스 근접/역전 | 20일선−60일선 격차가 종가의 1% 이내 또는 역배열 |
| `drawdown_from_high` | 이미 하락 시작 | 최근(≤60일) 고점대비 ≤ −7% |
| `volume_divergence` | 상승 동력 약화 | 최근 10일 가격↑ + 거래량이 40일 평균의 80% 미만 |

- 임계값은 `decline_signals.THRESHOLDS` 상수 + 설명 — 하드코딩이 아니라 **조정 가능한 config 의미**.
- 데이터 부족 신호는 발화 안 함(거짓 경보 금지). 전체 데이터 < 20거래일이면 `NotEnoughData`.

### 위험점수 산식 (0~100)

```
risk_score = clamp( Σ_{발화신호} severity × weight ) × 100
```

가중치(`SIGNAL_WEIGHTS`, 합 1.0): 과열 0.18 · RSI 0.16 · 변동성 0.16 · 추세둔화 0.16 ·
데드크로스 0.14 · 낙폭 0.12 · 거래량 0.08. **발화한 신호만** 가중합.

수준: `low(<15) · elevated(15–35) · high(35–60) · severe(≥60)`.

---

## 3. 보수적 전환 로직 (`decline_scan.py`)

흐름: 관심/보유 종목 집합 → 각 종목 `compute_signals` → 종목별 위험점수 +
**섹터/지수 집계**(평균 위험) → 트리거 시 **보수적 전환 권고**.

트리거(`SHIFT_THRESHOLDS`): 집합 평균 위험 ≥ 25 **또는** 고위험(high+severe) 종목 비율 ≥ 1/3.
강함: 평균 ≥ 45 또는 고위험 비율 ≥ 50%.

권고(제안 객체 — **읽기 전용**, 주문 0):
```json
{
  "action": "shift_conservative",
  "strength": "moderate|strong",
  "rationale": "스캔 종목 평균 위험점수 .. / 고위험 종목 .. / 주요 선행신호 ..",
  "suggested_cash_band": {"min": .., "max": .., "from": {현재밴드}},
  "reduce_risk_assets": true,
  "consider_hedge": <강한 경우 true>,
  "auto_order_created": false,
  "apply_via": "사람 승인 — profile/policy 저장 경로로만 반영(자동 적용 금지)"
}
```

- 현재 대전제 `cash_band`(policy.compile_policy)를 **읽기만** 해서 그 위로 +5%p(약)/+10%p(강) 권고.
- 적용은 사람이 `profile`/`policy` 저장 경로로만. 엔진은 정책을 바꾸지 않는다.
- `scan_account_universe(account_index)` 가 universe+holdings+cash_band 를 DB 에서 모아 스캔.

---

## 4. 데이터 소스 — 정직 안내

`price_history` 테이블(`store/schema.sql`, additive): PK `(instrument_code, trade_date)`.

| 소스 | 상태 | 비고 |
|---|---|---|
| KIS 일봉 (`KisDailyBarFetcher`) | **구현 완료(read-only)** | endpoint `inquire-daily-itemchartprice`, tr_id `FHKST03010100`(mode 무관) — KIS Developers 공식 + wikidocs/kis-client/zerohertzLib 다수 독립 소스 교차확인(2026-06-21). 요청 FID: `FID_COND_MRKT_DIV_CODE=J`, `FID_INPUT_ISCD`, `FID_INPUT_DATE_1/2`(YYYYMMDD), `FID_PERIOD_DIV_CODE=D`, `FID_ORG_ADJ_PRC=0`(수정주가). 응답 `output2[]`: `stck_bsop_date/stck_oprc/stck_hgpr/stck_lwpr/stck_clpr/acml_vol`(거래량 포함). 1회 ≤100건 → 날짜 윈도우 페이징. **read-only(주문 0): adapter `get_daily_bars` 만 호출, `place_order` 미사용.** live 키여도 일봉 조회는 read-only 라 주문 하드락과 무관(조회 허용). 키 없으면 `KisConfigError` 안전 실패(가짜 성공 금지). |
| 키움 일봉 (`KiwoomDailyBarFetcher`) | **stub (미구현)** | opt10081 류 — 사용자 endpoint 확인 필요(범위 외). |
| quotes seed (`seed_from_quotes`) | 동작(근사) | 기존 누적 `quotes`(단일시점가)에서 seed. **OHLC=close 근사, 거래량 없음** → 실 일봉보다 정확도 낮음(특히 ATR/거래량 다이버전스). |

> **정직**: fetcher 는 구현 완료. **실 일봉 적재는 사장님 KIS 키로 1회 실행이 필요**하다
> (`--fetch-daily --account 1`). 적재 전 backtest 는 `not_enough_data` 로 정직 보고한다.
> quotes_seed 근사로는 표본/정확도가 낮으므로 "분석 완료/예측 가능"으로 과장하지 않는다.

---

## 5. 노하우 성장 구조 (`decline_backtest.accumulate_knowhow`)

- backtest 결과 → 기존 growth 시스템(`lessons.add_candidate`)에 **후보로** 누적(새 API 호출 없음).
- `scope='instrument'`, `ref=instrument_code` (종목 단위 공통 노하우 — **계좌 교차적용 아님**).
- 예: "종목 X — 과거 N거래일 −10% 이상 낙폭 K회, 사건 직전 빈출 선행 신호: [..]".
- 즉시 promoted 아님 — `candidate`. 승격은 기존 기준(반복 ≥2 + 근거/결과 + confidence ≥0.6).
- confidence 는 사건 수가 적으면 보수적(2회 미만 0.4, 이상 0.5~0.7).

---

## 6. 백테스트 헬퍼 (`decline_backtest.py`, 결정론)

- `label_declines(history, decline_pct)` — peak→trough ≤ −N% 구간 라벨링(비겹침).
- `signals_before(history, peak_idx, window)` — 사건(peak) 직전 window 일 중 발화 신호(look-ahead 없음).
- `backtest(code)` — 사건 목록 + 신호별 **선행 빈도**(`signal_lead_rate` = 사건 직전 발화 / 전체 사건).

> **정직**: `signal_lead_rate` 는 precision/recall 이 아니라 **연관성**(사건 직전 동반 빈도)이다.
> 표본이 작으면 신뢰 낮음. 실 백테스트는 일봉 fetch 전제.

---

## 7. 진입점 (CLI)

```bash
# 단일/다수 종목 스캔
python -m main_mission.portfolio_os.decline_scan --code 005930 --code 000660
# 계좌 유니버스+보유 스캔(+cash_band 권고)
python -m main_mission.portfolio_os.decline_scan --account 1
# quotes 에서 seed
python -m main_mission.portfolio_os.price_history --seed-from-quotes 005930
# KIS 일봉 실적재(read-only, 주문 0) — 단일 종목
python -m main_mission.portfolio_os.price_history --fetch-daily --account 1 --code 005930 --count 300
# KIS 일봉 실적재 — 계좌 관심+보유 종목 전체
python -m main_mission.portfolio_os.price_history --fetch-daily --account 1 --count 300
# 적재된 일봉으로 백테스트 + 노하우 누적
python -m main_mission.portfolio_os.decline_backtest --code 005930 --accumulate
```

---

## 8. 다음 단계

1. ✅ **KIS 일봉 fetcher 구현 완료**(read-only). 남은 것: 사장님 KIS 키로 **1회 실적재 실행**(`--fetch-daily --account 1`) → 실데이터 누적 확인. (키움 fetcher 는 범위 외 stub.)
2. **실 백테스트** — 일봉 적재 후 종목별 예측력 측정 → 임계값(THRESHOLDS) 튜닝.
3. **웹 UI (조회 전용, 후속)** — 종목별 위험점수/신호/보수적 전환 권고 대시보드. 웹은 조회만(하드코딩 0).
4. **daily_review 연결** — 일일 점검에 스캔 결과를 읽기 전용 입력으로 통합(제안 흐름).
5. **regression test 승격** — 반복 검증된 "하락 전 신호 패턴"을 task_regression_tests 로.

---

## 9. 6축 입체 분석 (`decline/axes/`)

하락/조정은 여러 원인에서 온다 — 이를 **6축**으로 나눠 각각 독립 scorer 로 분석한다.
각 축 scorer 는 **공통 인터페이스** `score(context) -> AxisResult` 를 따른다.

```python
AxisResult = {
  "axis":           str,    # technical|distribution|macro|event|sentiment|policy
  "risk_0_100":     float,  # 이 축이 본 하락 위험 (data 없으면 0.0)
  "signals":        [ {name, fired, value, severity, detail}, ... ],
  "data_available": bool,   # 정직 — 실데이터로 계산했는가? False면 가짜 점수 아님
  "confidence":     float,  # 0~1 — 이 축 데이터 양/질 (data 없으면 0.0)
  "detail":         str,    # 한글 한 줄 요약
}
```

| 축 | 모듈 | 보는 것 (하락 전 특징) | 데이터 소스 | **정직 상태** |
|---|---|---|---|---|
| **기술** technical | `axes/technical.py` | 이격·RSI·변동성·MA둔화·데드크로스·낙폭·거래량 다이버전스 | `price_history` | **동작** — 기존 `decline_signals` 래핑(재사용, 읽기만). quotes_seed 근사는 정확도 낮음 |
| **분산** distribution | `axes/distribution.py` | 외국인·기관 동반 순매도 + 개인 순매수 + 거래량 급증(스마트머니 분산·세력 이탈) / **기관 방어 매수는 완충**(위험 감쇄) | KR 종목별 투자자 매매동향 KIS `inquire-investor`(tr_id `FHKST01010900`, mode 무관, read-only) → `broker/kis_investor.py` → `investor_flows` | **연동(ingestion 구현)** — 데이터 있으면 점수+한글 설명, 없으면 `data_available=False`(가짜 0 금지) |
| **거시** macro | `axes/macro.py` | 금리 인상·장단기 역전·신용 팽창·고인플레·환율 충격 | ECOS(한은)/FRED → `macro_indicators` | **미연동** — 일부 지표만 있어도 가용 지표로 계산(confidence↓) |
| **이벤트** event | `axes/event.py` | FOMC·금통위·CPI·고용 발표 임박 → **변동성 위험 알림(예측 아님)** | 경제 캘린더 → `market_events` (적재: `event_calendar.py`) | **적재 경로 구현** — 공식 일정 **수동 입력**(자동 캘린더 API 미연동), 데이터 있으면 `data_available=True`·없으면 `False`(가짜 0 금지) |
| **심리** sentiment | `axes/sentiment.py` | VIX·**VKOSPI**·풋콜·신용잔고·**거래대금** 급등(공포/과열) | VIX/VKOSPI/풋콜/신용/거래대금 → `sentiment_index` (적재: `event_calendar.py`) | **적재 경로 구현** — **거시와 분리**(거시=금리/환율/유가, 심리=변동성/공포). VIX 하나로 '완성' 과장 금지(지표 수↑ → confidence↑) |
| **정책/규제** policy | `axes/policy.py` | 정부 정책 불리·규제 발표 → 섹터 조정 | 뉴스/DART → `policy_events` (stance/severity 는 사람·메모리 판단 저장, **API 자동분류 아님**) | **미연동** |

> **정직 원칙**: 기술축 외 5축은 **실데이터 있으면 계산, 없으면 `data_available=False`**.
> 데이터 없는 축은 위험점수 0·confidence 0 — **가짜 점수 절대 금지**(거짓 경보 방지).
> 프레임·메타인지·성장 루프는 **완성**했으나, *실 예측력은 데이터 연결 후*에 의미가 있다.

---

## 10. 메타인지 종합 (`decline/composite.py`)

`composite(context)` 가 6축을 실행해 **가용 축만** 합성한다(미연동 축 제외).

### 가중치 = 데이터 가용성 × 과거 예측 적중 신뢰도

```
weight_i  = axis_confidence_i × reliability_i              # 가용 축만
holistic_risk = Σ_i (risk_i × weight_i) / Σ_i weight_i     # 가중평균 0~100
overall_confidence = mean(axis_confidence_i) × coverage    # coverage = 가용축/전체축
```

- `axis_confidence_i` = 그 축의 데이터 양/질(축 scorer 가 산출).
- `reliability_i` = **track record**(§11) — 그 축의 과거 예측 적중률. lessons 에서 읽어
  **쓸수록 정교화(성장)**. 이력 없으면 중립 0.5(단정 회피).

### 메타인지 출력 (`metacognition`)

```json
{
  "reliable_axes":       ["technical", ...],
  "data_missing_axes":   ["distribution","macro","event","sentiment","policy"],
  "conflicting_signals": false,
  "conflict_detail":     "거시축 위험 100 vs 기술축 16 — 신호 상충",
  "coverage":            0.17,
  "note":                "가용 축 1/6 (coverage 17%). 데이터 얇음 — confidence 낮음, 단정 회피."
}
```

- **신뢰 가능한 축 / 데이터 부족 축 / 상충 신호** 를 명시. 데이터 얇으면 `overall_confidence`↓.
- 한 축 계산이 실패해도 그 축만 제외(`data_available=False`), 전체는 계속(robust).

---

## 11. 성장 학습 루프 (`decline/track_record.py`)

```
징후 발화(예측) → 이후 실제 하락 여부 → 적중/미스를 lessons 에 기록 → 다음 가중에 반영
```

- `record_outcome(axis, predicted_decline, actual_decline)` — 예측=하락이었을 때만 기록
  (true negative 제외 — 이 엔진은 **발화 신뢰도**를 본다).
  - 적중 = `lesson_candidates` title `"축 적중 — {axis}"` 관찰수++ (scope='axis', ref=axis).
  - 미스 = title `"축 미스 — {axis}"` 관찰수++.
- `reliability(axis)` — 적중/미스 관찰수 합 → 베이지안 평활 적중률(prior=1/1, 약한 표본 보정).
- 기존 `growth/lessons` 시스템 재사용 — **새 API 호출 0**. 익명화/scope 규칙 준수.
  scope='axis'(시장 공통 노하우) — **계좌 교차적용 아님**.

> ⚠️ `actual_decline` 은 **실현 결과**(백테스트/사후관찰)여야 한다.
> mock 으로 가짜 이력을 쌓아 "성장 완료"로 보고 금지(CLAUDE.md §11.8).

---

## 12. 통합 + 데이터 테이블 (`decline_scan` / `store/schema.sql`)

- `decline_scan.scan_instrument(code, multi_axis=True)` 가 종목별로 6축 종합(`composite`)을
  **읽기 전용**으로 첨부 (`scanned[i]["composite"]`, `holistic_risk`, `overall_confidence`).
  기존 기술축 `risk_score` 는 호환을 위해 유지. **주문 0** (`auto_order_created: False`).
- `decline/context.py` 가 축별 DB 테이블에서 context 를 빌드(순수 axes 와 DB 분리).
- **schema(additive only, drop 금지, 1 entity 1 table)**:
  `investor_flows` · `macro_indicators` · `market_events` · `sentiment_index` · `policy_events`.
  모두 **ingestion 지점** — 실데이터 적재 전까지 해당 축은 `data_available=False`.

### 다음 단계 (6축)

1. ~~**분산축 ingestion**~~ ✅ **완료** — `broker/kis_investor.py`(`KisInvestorFetcher`) 가 KIS
   `inquire-investor`(tr_id `FHKST01010900`, read-only)로 외국인/기관/개인 순매수+거래량을
   `investor_flows` 에 멱등 적재. `security_selection` 후보 비교에도 수급 신호 반영(이탈→진입속도 조절,
   기관 방어매수→방어 후보; 설명 중심·단정 금지). **연기금/프로그램 등 세부 주체는 본 TR 미제공 →
   외국인/기관/개인 3주체만(정직).** 키움 fetcher 는 추후(endpoint 확인 필요).
2. **거시축 ingestion** — ECOS/FRED → `macro_indicators` (장단기 금리·CPI·신용·환율).
3. ~~**이벤트/심리 ingestion**~~ ✅ **적재 경로 완료** — `event_calendar.py`:
   - **이벤트 캘린더**: `add_event`/`seed_official_schedule`(공식 일정 **수동 입력** — 자동 캘린더 API 미연동, placeholder 금지) → `market_events`.
     `event_risk_alert()` 는 임박 고영향 발표를 **변동성 위험 알림(예측 아님)**으로 정리(자동주문 0, 관망/현금/헤지 후보는 사람 승인).
   - **심리지표**: `upsert_sentiment`(VIX/VKOSPI/풋콜/신용잔고/거래대금) → `sentiment_index`. **거시와 분리**. `sentiment_coverage()` 로 지표 수 정직 보고(VIX 하나면 confidence↓, 과장 금지).
   - 정책 ingestion(뉴스/DART)은 별도 후속.
4. **track record 실측** — 실 백테스트(일봉 fetch 후) 결과로 축별 reliability 누적 → 가중 정교화.
5. **웹 대시보드(조회 전용)** — 종목별 6축 breakdown + 메타인지(신뢰/부족/상충) 시각화.

---

## 13. 성장 루프 영속화 (`decline/analysis_log.py`, `store.decline_analyses`)

`composite`/`track_record` 가 **메모리(런타임)** 였다면, 이 층은 **분석→결과→reliability 변화를
DB 에 영속화**해 다음 사이클이 더 나은 상태로 시작하게 한다(성장 §11). **분석 기록만 — 자동주문 0.**

### 13.1 `decline_analyses` 테이블 (additive, 1 entity 1 table, drop 금지)

| 컬럼 | 의미 |
|---|---|
| `analysis_id` PK | 분석 1건 |
| `account_index` | 이 분석을 **조회한** 계좌(있으면). **성장은 계좌 무관**(시장 공통) — 교차적용 아님 |
| `code` / `sector` | 종목 / 섹터 |
| `analysis_date` | **예측 시점** — 이날까지 데이터만으로 산출(lookahead 차단 기준) |
| `available_axes` / `missing_axes` | 가용 / 미연동 축 (JSON) |
| `axis_scores` | 축별 {risk_0_100, confidence, reliability, weight} (JSON) |
| `overall_risk` / `overall_confidence` | composite holistic_risk / overall_confidence |
| `suggested_action` / `policy_draft_created` | 보수적 전환 제안 / 초안 생성 여부(자동적용 아님) |
| `user_action` | `ignored\|accepted\|modified\|saved_to_policy\|rejected_as_wrong` |
| `future_return_window` | 결과평가에 쓴 거래일 수(예: 10/20) |
| `actual_drawdown` | analysis_date **이후** window 내 실제 최대 낙폭(%, 음수). NULL=미평가 |
| `hit_or_miss` | `pending\|hit\|miss\|no_prediction` |
| `reliability_before/after` | 결과평가 직전/직후 대표(종목) reliability |
| `lesson_id` / `created_at` / `evaluated_at` | 연결 lesson / 생성 / 평가 시각 |

### 13.2 흐름 (3단계)

1. **`record_analysis(code, scan_result, analysis_date, ...)`** — scan_instrument(composite 포함)
   결과를 예측 시점으로 저장. `hit_or_miss='pending'`.
2. **`set_user_action(analysis_id, action, ...)`** — 사용자 반응 갱신(무시/적용/수정/정책저장/오답).
3. **`evaluate_outcome(analysis_id, ...)`** — 결과 평가 → 성장:
   - **lookahead 차단**: `trade_date > analysis_date` 인 일봉만 사용해 낙폭 계산.
     미래 일봉이 아직 없으면 `no_future_data_yet` 로 **평가 보류**(정직 — 미래 누설 금지).
   - 기준가 = analysis_date 종가, 낙폭 = `(min(future_close)/base − 1)×100`.
   - 낙폭 ≤ −7%(기본) → 실제 하락. 예측(overall_risk ≥ 15) ∧ 하락 = **hit**, 아니면 **miss**.
   - `track_record` 로 **종목·섹터·발화 축** reliability 갱신(시장 공통, 계좌 교차적용 아님) →
     `reliability_before/after` 기록.
   - `evaluate_pending()` — window 경과한 미평가 분석 일괄 평가.

> **lookahead 차단 증거**(005930 실 일봉, 2026-06-21): analysis_date `2026-05-07`(예측 시점,
> overall_risk 28.8) → 결과평가는 `2026-05-08 ~ 2026-05-21` 일봉만 사용(분석일 이후만).
> 실제 낙폭 −1.1%(< 7%) → **miss** → 종목 reliability 0.5 → **0.333**(거짓경보 페널티 — 성장 동작).

### 13.3 Dashboard 데이터 (`decline/dashboard.py`, 조회 전용 — 웹 화면 후속)

데이터 함수만(웹은 조회 전용, 하드코딩 0). `auto_order_created=False`, `read_only=True`.

| 함수 | 데이터 |
|---|---|
| `risk_trend` | 최근 위험점수 + confidence 추이(분석일 오름차순) |
| `missing_axes_freq` | 미연동(부족) 데이터 축 빈도 — 어디를 채워야 신뢰 오르는지 |
| `conservative_shifts` | 보수적 전환 제안 이력 + 사용자 반응 |
| `prediction_scoreboard` | 제안 적중/미스 집계 — **평가 완료분만**(표본<5면 신뢰 낮음 명시 — 과장 금지) |
| `reliability_snapshot` | 축/종목/섹터 reliability 현재값(데이터 부족=0.5 중립 — 정직) |
| `dashboard(account_index)` | 위 묶음(계좌 필터는 "그 계좌가 본 분석"만, 성장은 계좌 무관) |

### 13.4 진입점

```bash
# 결과 평가(미래 데이터 적재 후) — 단일 / 일괄
python -m main_mission.portfolio_os.decline.analysis_log --evaluate 12 --window 10
python -m main_mission.portfolio_os.decline.analysis_log --evaluate-pending --window 10
# 대시보드 데이터(JSON, 조회 전용)
python -m main_mission.portfolio_os.decline.dashboard --account 1
```

> **정직**: 실현 결과(hit/miss)가 적으면 reliability 는 **중립 0.5 유지**.
> mock 으로 가짜 이력을 쌓아 "성장 완료"로 보고하지 않는다(CLAUDE.md §11.8).
> 테스트: `tests/test_decline_growth.py` — 분석저장·user_action·**lookahead 차단**·
> hit→reliability↑·miss→↓·중립유지·scope 격리·dashboard·자동주문 0.

---

## 14. confidence 별 판단 강도 (`decline_scan.confidence_judgment`)

`overall_confidence`(6축 메타인지)에 따라 **조언 강도를 제한**한다 — *신뢰도 낮은데 강한 조언 금지*
(CLAUDE.md §11.8). 경계는 `CONFIDENCE_BANDS`(config 의미): `low=0.3`, `mid=0.6`.

| confidence | tier | assert_ok | 허용 강도 | 입장(stance) |
|---|---|---|---|---|
| `< 0.3` (또는 미상 None) | `insufficient` | **False** | `candidate_only` | 관망/주의 · "데이터 추가 필요" · 보수전환은 **후보로만** |
| `0.3 ~ 0.6` | `weak` | False | `weak` | 약한 보수전환 · 현금밴드 소폭 상향 후보 |
| `≥ 0.6` | `moderate` | **True** | `moderate` | 비교적 강한 보수전환 (단, **항상 사람 승인**) |

- `_conservative_proposal` 가 집합 종목 `overall_confidence` 평균을 내 **강도를 캡**한다.
  위험점수는 높아도 신뢰도가 `candidate_only` 면 `strength="candidate"`, `reduce_risk_assets=False`,
  `consider_hedge=False` 로 **강등**(단정 금지). 6축 미연동(기술축만)이면 confidence 가 낮아
  자연히 후보 수준으로만 나온다(거짓 강조언 방지).
- `proposal.allowed_actions` 는 **운용기준 조정 후보**만 나열(주문 아님): 관망·리스크 경고·
  현금밴드 상향(후보)·위험자산 축소(후보)·테마 노출 축소(후보)·신규매수 보류(후보)·
  리밸런싱 속도 완화(후보)·헤지 검토(강한 신호 한정, 후보). **"하락 확정"·매수/매도 단정 금지.**
- `scan_instrument(multi_axis=True)` 결과에도 종목 단위 `confidence_judgment` 가 붙는다(읽기 전용).

---

## 15. policy draft 연결 (`decline_policy_draft.py`)

보수적 전환 제안 → **policy draft**(사람 승인 전 운용기준 조정 후보). **자동 적용 절대 금지.**

```
분석(decline_scan) → 제안(shift_conservative) → draft 생성(저장은 draft 상태)
   → (사람 검토·승인) → policy version 반영 → allocation 재계산
```

- `build_draft(proposal, account_index, summary)` → draft 객체. proposal 가 None(위험 낮음)이면
  `has_draft:False`(거짓 경보 금지). 모든 draft 는 `auto_applied:false, requires_user_approval:true`,
  `status:"draft"`.
- `proposed_changes` = 운용기준 조정 **후보** 묶음(현금밴드 상향 후보·위험자산 축소 후보·헤지 검토 후보).
  confidence 미달(`candidate_only`)이면 현금밴드 변경값을 제시하지 않고 "관망/주의·데이터 추가" 수준만.
- `save_draft` 는 **기존 `advice_items` 의 미승인(status='open')** 행으로만 저장한다.
  `policy.compile_policy` 는 `status='accepted'` advice 만 읽으므로 **승인 전에는 정책/비중에 영향 0**
  (자연스러운 사람 승인 게이트 재사용 — schema 추가 없음). 거절(rejected) 이력은 존중(반복 강요 금지).
- **자동 적용 차단 증거**: draft 저장 후에도 `compile_policy(account).cash_band` 불변
  (`tests/test_decline_policy.py::test_saved_draft_does_not_change_policy`).
- `generate_and_save(account)` = 유니버스 스캔 → 제안 → draft 생성·저장(미승인). `auto_order_created:false`.

```bash
python -m main_mission.portfolio_os.decline_policy_draft --account 1          # 스캔→draft(미승인)
python -m main_mission.portfolio_os.decline_policy_draft --account 1 --list   # draft 목록
```

---

## 16. Daily Review 연결 (`daily_review._decline_block`)

일일 점검에 **"하락 징후 점검" 섹션**을 추가(읽기 전용·broker-neutral·자동주문 0).

- 종목 수집은 **account_snapshots(holdings) + universe_instruments** 에서(브로커 직접 호출 없음 — broker-neutral).
- 각 종목: **6축 가용성(`axes_available`)·`overall_confidence`·신뢰축/부족축/상충신호·위험점수**.
  일봉 없으면 `status:"not_enough_data"` 로 **정직 표기**(거짓 경보 금지).
- 집합: 보수적 전환 **후보**(`proposal`, 없으면 None) + **오늘의 조치**(`today_action`):
  `유지`(위험 낮음) · `관망`(분석 가능 종목 없음/데이터 부족) · `보수적 전환 제안`(트리거 충족).
- `auto_order_created:false`, `policy_draft:null` — review 는 **제안까지만**(draft 저장은 사람 흐름인
  `decline_policy_draft` 에서). confidence 낮은 종목은 단정 없이 관망/주의로 표기.
- 모든 분기(스냅샷 없음·decision 차단·hold·watch·rebalance)에서 동일하게 포함되어
  `review["decline"]` + `payload.decline` 로 노출된다.

> **정직 + 안전**: Daily Review 의 하락 징후 섹션은 *주문·정책 자동변경을 만들지 않는다*.
> 보수적 전환은 후보이며, 사람 승인 후 `decline_policy_draft` → policy version 경로로만 반영된다.
> 테스트: `tests/test_decline_policy.py` — confidence 강도·draft 흐름·자동적용 차단·자동주문 0·
> Daily Review not_enough_data 정직 표기.
