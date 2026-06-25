# OpenDART(금융감독원) 재무 연결 가이드 — 저평가 우량주 필터용

> 목적: 개별주 후보를 **테마가 아니라 재무제표 기반 저평가 우량주 필터**로 거르기 위해,
> 금융감독원 OpenDART(공식·무료)에서 재무 수치를 적재한다.
> **원칙(불변): 공식·무료 우선 · 가짜 데이터 0 · 키 없으면 정직 not_connected · 자동주문 0 · 자격증명은 `.env` 에만.**
> 두뇌(분석·조율)는 Claude + 메모리. **Anthropic API 미사용.**

---

## 0. 왜 corp_code 매핑이 선행돼야 하나

- 한국거래소(KIS)는 종목을 **6자리 종목코드(ticker)** 로 식별한다 (예: 삼성전자 `005930`).
- OpenDART 는 종목코드가 아니라 **8자리 고유번호(corp_code)** 를 쓴다 (예: 삼성전자 `00126380`).
- 따라서 재무를 받으려면 먼저 **ticker(6) → corp_code(8) 매핑**이 있어야 한다.
- 매핑은 **공식 파일(CORPCODE.xml)** 로만 만든다. **corp_code 추측 금지** — 없으면 정직 실패.

```text
6자리 ticker(005930)
   └─ corp_code_map.json ─→ 8자리 corp_code(00126380)
                              └─ OpenDART fnlttSinglAcntAll ─→ fundamentals 적재
                                                                  └─ quality_filter 자동 활성
```

---

## 1. 전체 흐름 (한눈에)

```text
① OpenDART 가입 + 인증키 발급
   →  ② .env 에 DART_API_KEY 입력
   →  ③ corp_code 매핑 생성 (--build-corp-map)
   →  ④ 재무 적재 (--load / --load-many)
   →  ⑤ 상태 확인 (--status)  →  저평가 우량주 필터(quality_filter) 자동 가동
```

---

## 2. OpenDART 인증키 발급

1. **OpenDART** 접속: `https://opendart.fss.or.kr`
2. 회원가입 → 로그인 → **인증키 신청/관리** 메뉴에서 인증키 발급(무료).
   - 발급 즉시 40자리 인증키 문자열을 받는다.
3. ⚠️ 이 인증키는 **비밀**. 메신저/캡처/코드/DB/로그에 붙여넣지 말 것 — `.env` 전용.

> 일일 호출 한도(분당/일별)가 있다. 대량 적재는 종목 수를 나눠서 진행.

---

## 3. `.env` 에 입력

프로젝트 루트 `.env` 에 아래를 채운다 (값은 따옴표 없이):

```dotenv
DART_API_KEY=발급받은_인증키
# (선택) 매핑 파일 경로. 미설정 시 기본 data/corp_code_map.json
# DART_CORP_MAP=data/corp_code_map.json
```

> 템플릿: `config/portfolio/secrets.example.env`. 실제 값은 git 에 커밋되지 않는다(.gitignore).
> **키 없이 실행하면** 모든 명령이 `not_connected` 로 정직 실패한다 (가짜 재무 0).

---

## 4. corp_code 매핑 생성

OpenDART 공식 파일 `CORPCODE.xml`(zip)을 받아 **상장사(stock_code 있는 행)만** 매핑한다.

```bash
python -m main_mission.portfolio_os.financials_connect --build-corp-map
```

- 공식 endpoint: `https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key=..`
- 결과: `data/corp_code_map.json` (`{"005930": "00126380", ...}`) 생성, `{"ok":true,"mapped":N}`.
- 키 불량 등으로 zip 이 아닌 응답이면 **정직 실패**(FinancialsConfigError) — 가짜 매핑 0.

---

## 5. 재무 적재

### 5.1 한 종목·한 기간 (연간 기본)

```bash
python -m main_mission.portfolio_os.financials_connect --load 005930 --year 2024
# 분기: --reprt 11013(1Q)·11012(반기)·11014(3Q)·11011(연간, 기본)
# 별도재무: --fs-div OFS (기본 CFS 연결)
```

### 5.2 한 종목·여러 기간 일괄 (연간 + 분기)

```bash
python -m main_mission.portfolio_os.financials_connect --load-many 005930 \
    --years 2022,2023,2024 --reprts 11011,11012,11013,11014
```

- 무자료(status=013) 기간은 **건너뛰고**(가짜 0) 나머지만 적재. 멱등(같은 ticker+period 갱신).
- 적재 컬럼: 매출(revenue)·영업이익(op_income)·순이익(net_income)·영업이익률(op_margin)·
  부채비율(debt_ratio = 부채/자본×100)·영업현금흐름(cash_flow_op)·재고(inventory).
- **ROE/PER/PBR/EV-EBITDA/capex 는 재무제표만으론 산출 불가 → `None`(정직, 가짜 0)**.
  (주가/시가총액/EBITDA 등 별도 데이터 연동 시 채워진다.)

---

## 6. 상태 확인 + 저평가 우량주 필터 가동

```bash
python -m main_mission.portfolio_os.financials_connect --status
```

표기(정직):
- `dart_api_key`: set / not_set
- `corp_code_map`: N tickers / not_provided
- `fundamentals_loaded_tickers`, `fundamentals_rows`
- `quality_filter_active`: fundamentals 행이 있으면 true

**fundamentals 가 적재되면** `security_selection.quality_filter` 가 자동으로 그 수치를 읽어
저평가·재무안정·저부채·현금흐름 기준으로 개별주를 판정한다:

- 적자(net_income ≤ 0) · 현금유출(영업현금 < 0) · 고부채(부채비율 > 200%) 등은 **제외/경고**.
- **테마만 좋고 재무 부실한 기업은 강한 추천 금지.**
- fundamentals 미연동이면 `passed=None`("필터 적용 불가 — 데이터 필요") — **가짜 통과 0**.

---

## 7. 안전 규칙 요약 (CLAUDE.md §2, §11.8 준수)

| 규칙 | 동작 |
|---|---|
| 공식·무료 우선 | OpenDART 공식 endpoint 만 사용(임의추측 endpoint 금지) |
| 가짜 데이터 0 | 키/매핑 없거나 무자료면 정직 실패/0건 — 합성 재무·점수 생성 안 함 |
| 키 없으면 안전 실패 | `not_connected` + `FinancialsConfigError` |
| 자동주문 0 | 본 모듈은 재무 적재까지만. 비중·주문은 사람 승인 |
| secret 0 | DART_API_KEY 는 `.env` 에만 — 코드/DB/로그/메모리 평문 금지 |
| Anthropic API 미사용 | 지능은 Claude + 메모리 |

---

## 8. 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| `DART_API_KEY 가 .env 에 없습니다` | 키 미설정 — §2~3 |
| `corp_code 매핑 없음` | `--build-corp-map` 먼저, 또는 corp_code_map.json 보강 |
| `응답이 zip 이 아님 — 키 확인` | 인증키 오류/한도 초과 — OpenDART 에서 키 상태 확인 |
| `status=013` 무자료 | 해당 기간 재무 없음(정직 0건) — 다른 연도/보고서코드 시도 |
| `status=020` 등 | 호출 한도 초과 등 — 잠시 후 재시도(종목 분할) |
| `quality_filter passed=None` | fundamentals 미적재 — §5 적재 후 자동 활성 |
