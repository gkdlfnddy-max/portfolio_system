# 종목/ETF 선정 엔진 (Step 2–5) — `security_selection.py`

> **위치**: `main_mission/portfolio_os/security_selection.py`
> **목적**: 3안(자산배분)이 확정된 뒤, bucket별로 **실재 후보(ETF/종목)를 나열**하고
> **실측 데이터만 모아 비교**한다. **추천이 아니라 비교·토론 자료**다.

## 파일명에 대한 메모 (중요)

CEO 지시 원문은 `selection.py (NEW)` 였으나, 같은 경로에 **이미 다른 용도의
`selection.py`(3안 자산배분 *선택 확정* 서비스)** 가 존재한다 — `decision.py`,
`allocation_explain.py`, `growth/prehooks.py` 가 이 모듈을 import 한다. 덮어쓰면
의사결정 파이프라인이 깨진다. 따라서 종목/ETF 선정 엔진은 충돌을 피해
**`security_selection.py`** 로 신설했다. (1 책임 = 1 파일 원칙 유지.)

| 파일 | 책임 |
|---|---|
| `selection.py` (기존, 건드리지 않음) | 자산배분 3안 중 1안 **선택 확정** |
| `security_selection.py` (신규) | bucket별 **종목/ETF 후보 비교** |

## 핵심 원칙 (불변)

- **비교·토론 중심, 단정 금지.** 출력은 "현 정책·관점 기준 적합도 + 장단점".
- **근거 없는 강한 추천 금지.** 데이터 부족이면 `strong_conclusion_allowed=False` +
  "후보 비교 단계, 강한 추천 불가" 표기.
- **미연동·데이터 부족은 그대로 표기.** 가짜 지표 0, 가짜 evidence 0.
- **읽기 전용.** 자동주문/policy 변경 0, secret 0, Anthropic API 0.
- 후보 메타(운용보수 등)가 DB 미연동이면 `unknown`(추정 금지).

## Bucket 정의 + 후보 시드 (실재 티커)

| bucket | 라벨 | 종류 | 후보 시드 |
|---|---|---|---|
| `global_core` | 글로벌 코어 ETF | etf | SPY, VOO, QQQ, VT, VTI |
| `robotics` | 로봇/자동화 | etf | BOTZ, ROBO, ARKQ |
| `semiconductor` | 반도체 | mixed | SOXX, SMH, 005930(삼성전자), 000660(SK하이닉스) |
| `semiconductor_inverse` | 반도체 인버스(헤지) | inverse | SOXS, KODEX 반도체인버스 |
| `treasury` | 국채(방어) | bond | **A 에이전트(bond_bucket) 시드** — `universe_instruments` 에서 `asset_class='bond'` 또는 이름에 국채/국고채 |

시드는 "후보 나열"일 뿐 추천 아님. 계좌 `universe_instruments` 에 사용자가 직접 넣은
같은 종류 후보도 병합한다. 국채는 A 에이전트 시드가 없으면 **빈 후보 + honest flag**.

## 읽는 데이터 소스 (함수만 호출, 본문 의존 X)

| 축 | 소스 함수 |
|---|---|
| 자료(재무/뉴스/공시/섹터) | `evidence_summary.evidence_for_account` / `briefs_by_source_type` |
| ETF 구성·겹침 | `etf_analysis.analyze_etf` / `overlap` |
| 하락 징후(6축) | `decline_scan.scan_instrument` |
| 가격·일봉 → 변동성 | `price_history.load_history` |
| 거시 | `macro_connect.macro_snapshot` |
| 관점/목적 | `user_views.list_views` / `investor_objective.criteria_for_account` |
| 후보(국채 포함) | `universe_instruments` |

> **주의(정직 관련):** `evidence_for_account` 는 **계좌 보유/관심(universe)에 연결된
> evidence 만** 돌려준다. 후보가 universe 에 없으면 evidence 가 부착되지 않는다(설계상
> 정상). 즉 후보를 본격 평가하려면 먼저 관심종목으로 등록되어 있어야 한다.

## 공개 API

| 함수 | 반환 |
|---|---|
| `list_buckets()` | bucket 목록(라벨/종류/시드 수) |
| `bucket_candidates(account, bucket)` | 후보 리스트 + honest_flags |
| `data_availability(account, cand)` | 후보별 7축 connected/미연동 (ETF면 재무="직접대상 아님") |
| `evidence_for(account, cand)` | 가용 evidence 만 부착(없으면 빈 채 정직) |
| `quality_filter(ticker)` | **개별주 저평가 우량주 필터** — 재무/밸류에이션/컨센서스. 미연동이면 `passed=None` |
| `etf_scorecard(ticker, account)` | **ETF 선정 기준 스코어카드** — 항목별 connected/미연동 |
| `compare_bucket(account, bucket)` | 비교표 {장점·리스크·비용·중복노출·변동성·하락위험·관점적합성·quality/scorecard·confidence} |
| `classify_bucket(account, bucket)` | final/alternatives/excluded/need_more_data 분류(적합도, 추천 아님) |

### 개별주 저평가 우량주 필터 `quality_filter(ticker)`

재무(매출성장·영업이익률·순이익·부채비율·영업현금흐름·ROE) + 밸류에이션(PER/PBR/EV-EBITDA)
+ 실적/컨센서스를 종합해 *저평가·재무안정·현금흐름·저부채*를 통과 기준으로 본다.

- **현재 구조화 재무/밸류에이션 수치는 미연동**(연동된 건 가격/일봉뿐). 그러므로
  `passed=None` + `"필터 적용 불가(데이터 필요)"` 로 **정직 표기** — 가짜 통과/가짜 점수 0.
- evidence_items 의 `financials` 자료는 *정성 자료*라 수치 판정 근거로 쓰지 않는다
  (`qualitative_financials_evidence` 로 존재 여부만 표시).
- 급등주/적자테마/부실 종목을 우량주로 표기하지 않는다. fundamentals 가 연동되면
  `_structured_financials()` 한 곳만 교체하면 group별 판정이 자동 동작한다(흑자/저부채/저평가 등).
- ETF/지수형 티커는 `applicable=False`(→ `etf_scorecard` 사용).

### ETF 선정 기준 스코어카드 `etf_scorecard(ticker, account)`

점검 축: 기초지수·상위구성·섹터/국가노출·운용보수·거래량·괴리율·추적오차·환헤지·분배금·
**기존 보유와 중복노출**·최근성과·하락징후·거시민감도.

- **연동(현재)**: 상위구성·섹터/국가노출(`etf_constituents`) · 기존보유 중복노출(`holdings`+구성) ·
  최근성과/하락징후(`price_history`) · 거시 컨텍스트(`macro_connect`).
- **미연동(추정 금지 → "미연동"/unknown)**: 기초지수메타·운용보수·거래량·괴리율·추적오차·환헤지·분배금.
- `overlap_with_holdings`: 후보 ETF ↔ 계좌 보유/관심 ETF 간 겹침. **20%+ → concentration_flag**.
- `strong_conclusion_allowed`: 구성(top_holdings) connected **그리고** 중복노출 계산 가능할 때만.

### 비교표 항목

- **장점/리스크**: 실측에서만 도출. 데이터 없으면 "데이터 부족"으로 정직 표기.
- **비용(운용보수)**: 메타 미연동 → `unknown`(추정 금지).
- **중복노출**: `etf_analysis.overlap` — ETF끼리만, 구성 있을 때만. 겹침 20%+ 집중 플래그.
  후보↔보유 중복노출은 `etf_analysis.candidate_overlap_with_holdings`(공통종목 + **합산 간접노출**).
- **변동성**: 가격 20봉+ 있으면 일간수익률 표준편차 연율화(%), 아니면 `unknown`.
- **하락위험**: `decline_scan.scan_instrument` 6축, 데이터 부족이면 `not_enough_data`.
- **관점 적합성**: `user_views`/`objective` 대비 정합성(단정 아님).
- **confidence**: 가용 데이터 축 + evidence + 보조지표 기반(상한 0.9 — 단정 방지).
  `strong_conclusion_allowed` 는 confidence≥0.5 **그리고** (evidence>0 또는 축≥2) 일 때만.

### 분류 규칙 (`classify_bucket`)

- `excluded`: 하락 6축 `high` 가 측정된 후보(헤지 bucket 제외) · **개별주 우량주 필터가 명확히 미달(`passed=False`, 재무 부실/적자/고평가)**.
- `need_more_data`: 실측·자료 전무 → 판단 보류.
- `final_candidates`: `strong_conclusion_allowed=True` 인 후보만(추천 아님, 적합도).
  **단, 개별주는 우량주 필터 데이터 미연동(`passed=None`)이면 final 승격 금지** → `alternatives`.
- `alternatives`: 비교 가능하나 근거 부족 → 자료 보강 시 승격(우량주 필터 미연동 개별주 포함).

## CLI

```bash
python -m main_mission.portfolio_os.security_selection --account 1 --buckets
python -m main_mission.portfolio_os.security_selection --account 1 --bucket semiconductor
python -m main_mission.portfolio_os.security_selection --account 1 --compare semiconductor
python -m main_mission.portfolio_os.security_selection --account 1 --classify semiconductor
python -m main_mission.portfolio_os.security_selection --account 1 --quality 005930
python -m main_mission.portfolio_os.security_selection --account 1 --scorecard SPY
```

## 테스트

`main_mission/portfolio_os/tests/test_security_selection.py` (임시 SQLITE_PATH 핀).

검증: bucket 후보 시드 · 미연동 정직표기 · evidence 가용분만 부착 · 데이터 부족 시
강한 추천 금지 · 데이터 충분 시 비교/승격 · 국채 A 시드 미연동 honest flag ·
인버스 헤지 전용 표기 · **주문/선택 테이블 무변경(읽기 전용)**.

```bash
.venv/bin/python -m pytest main_mission/portfolio_os/tests/test_security_selection.py -q -p no:randomly
```
