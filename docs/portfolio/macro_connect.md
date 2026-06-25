# 거시/시장 데이터 연결 (Track B) — macro_connect

> **거시가 우선이다(CEO).** 거시 환경(금리·역전·인플레·환율·유가·공포지수·지수)을 먼저 읽고,
> 그 다음 현금밴드·채권/국채·위험자산·성장속도·달러노출·미국ETF·헤지로 *연결*한다.
> 단순 표시가 아니라 **판단(후보)** 으로 잇되, 비중/주문 자동변경은 0(사람 승인).

코드: [main_mission/portfolio_os/macro_connect.py](../../main_mission/portfolio_os/macro_connect.py)
테이블(스키마 불변): `macro_indicators(indicator, obs_date, value, source, captured_at)` ·
`sentiment_index`(동일 구조). PK `(indicator, obs_date)` → 멱등 적재.

---

## 1. 원칙 (불변)

- **가짜 데이터 금지.** API 키 없으면 명확 실패(`MacroConfigError`) — 합성 지표/점수 0건.
- **출처·기준일·freshness 저장.** `source`(ecos|fred|test) + `obs_date` 보존.
  freshness 는 obs_date 기준 decay(반감기 45일) — stale 임계는 지표 발표주기별로 다름.
- **데이터 없으면** `macro_snapshot.data_available=False`, `macro_to_portfolio.connected=False` (정직).
- **stale 지표는 신호에서 제외** — 오래된 거시를 최신처럼 쓰지 않는다(§11.8).
- **자동주문/policy 자동변경 0.** 적재 + 해석(후보)까지만.
- **비밀(.env) 0** — 키는 `.env`(`ECOS_API_KEY`/`FRED_API_KEY`)에서만. 코드/DB/로그 평문 금지.
- **Anthropic API 미사용** — 지능은 규칙 + Claude+메모리.

---

## 2. 확인한 공식 endpoint (WebSearch 검증 — 임의추측 아님)

| 소스 | endpoint | 형식 |
|---|---|---|
| **FRED** (미국) | `https://api.stlouisfed.org/fred/series/observations` | `?series_id=&api_key=&file_type=json&sort_order=desc&limit=` (결측='.' 제외) |
| **ECOS** (한국은행) | `https://ecos.bok.or.kr/api/StatisticSearch/{KEY}/json/kr/{start}/{end}/{stat}/{cycle}/{start_date}/{end_date}[/{item}]` | cycle: A 연·Q 분기·M 월·D 일. 날짜: M→YYYYMM, D→YYYYMMDD |

- FRED docs: fred.stlouisfed.org/docs/api/fred/series_observations.html
- ECOS docs: ecos.bok.or.kr/api/

### 매핑 series (응답 검증 후 적재 — 코드 개정 시 정직 실패)
- FRED: `DFEDTARU`→policy_rate_us · `DGS10`→yield_10y_us · `DGS2`→yield_2y_us ·
  `DCOILWTICO`→wti_oil · `VIXCLS`→vix · `NASDAQCOM`→nasdaq · `SP500`→sp500 · `CPIAUCSL`→cpi_index_us.
- ECOS: 한은 기준금리(722Y001) · 국고채 10Y/2Y(817Y002) · 원/달러(731Y001) · CPI yoy(901Y009).
  > ⚠️ ECOS 통계표/항목 코드는 한은이 개정할 수 있음. 응답이 `RESULT`(오류)면 그 지표만
  > `MacroConfigError` 로 건너뛰고(다른 지표는 계속) 정직하게 errors 에 기록 → **확인 필요**.

---

## 3. 사용

```bash
# 적재(키 필요 — 없으면 not_connected, 가짜 데이터 0)
python -m main_mission.portfolio_os.macro_connect --load
# 최신 지표 + freshness/stale (키 불필요, DB 만 읽음)
python -m main_mission.portfolio_os.macro_connect --snapshot
# 거시 → 포트폴리오 매핑(후보)
python -m main_mission.portfolio_os.macro_connect --map
```

설정: `.env` 에 `ECOS_API_KEY` / `FRED_API_KEY`
(템플릿: [config/portfolio/secrets.example.env](../../config/portfolio/secrets.example.env)).

---

## 4. 거시 → 포트폴리오 매핑 (판단 규칙, 후보만)

| 거시 신호 | 포트폴리오 방향(후보) |
|---|---|
| 금리↑ (한/미 기준금리 ≥3%) | 현금/단기채↑, 위험자산↓, 성장속도 완화, 듀레이션 짧게 |
| 장단기 역전 (10Y-2Y ≤ 0) | 방어↑, **헤지 검토** (침체 선행) |
| 고인플레 (CPI yoy ≥3%) | 현금/단기채 선호, 듀레이션 짧게 |
| 달러 강세 (원/달러 ≥1350) | **미국ETF/달러노출 우호**(환차익), 추격 경계 |
| 달러 약세 (≤1200) | 미국ETF 신규 환노출 분할/관망 |
| 유가↑ (WTI ≥90) | 인플레/비용 → 방어 가산 |
| VIX↑ (≥25, 공포) | 헤지/현금 검토 |

- 결과 `lean`(defensive/neutral/aggressive) + `tilts`(버킷별 방향) + `signals`(사람용 한 줄).
- **stale 지표는 매핑에서 제외**(정직). 데이터 없으면 `connected=False`.

---

## 5. 다른 모듈 연동

- `decline/axes/macro.py` — `decline/context.py` 가 DB 의 **신선** macro 지표만 채워 거시축 실점수.
  stale 지표는 context 에서 제외 → 거시축이 가짜 점수 내지 않음. 지표 0개면 `data_available=False`.
- `portfolio_impact.py` — `analyze_account.macro` 에 거시 해석을 싣고, **거시발 후보를 포트폴리오
  후보 맨 앞**에 배치(현금밴드/채권/달러/미국ETF/헤지). `different_interpretations.common_facts`
  에 `macro_connected`/`macro_lean`/`macro_signals` 노출.
- `perspective_variants.py` — A/B/C 각 안에 `macro_reading`(거시 우선 해석). 거시 미연동이면
  "거시 미연동 — 키 설정 후 우선 반영"이라 **정직** 표기. base 비중 로직/compile_policy/draft 불변.

---

## 6. 검증 (tests)

[main_mission/portfolio_os/tests/test_macro_connect.py](../../main_mission/portfolio_os/tests/test_macro_connect.py)
— 키 없을 때 안전 실패 · 멱등 적재 · freshness/stale · 미연동 정직 · 매핑(금리/역전/달러/VIX) ·
stale 제외 · 거시축 실점수/미연동 · FRED/ECOS 파싱(monkeypatch, 네트워크 0) · 자동주문/policy 0 ·
anthropic 0.
