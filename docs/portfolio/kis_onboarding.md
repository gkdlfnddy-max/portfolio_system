# 한국투자증권(KIS) Open API 연결 가이드

> 목적: AI 운영 매니저(broker-chief 에이전트)가 계좌를 관리하려면 먼저 KIS Open API 앱을 발급받아 연결해야 한다.
> **원칙: 무조건 모의투자(paper) 먼저.** 실전(live)은 CEO 승인 체크리스트 후에만.
> 자격증명은 `.env` 에만. 코드/DB/로그/메모리 저장 금지 (§26, 안전 A6).

---

## 0. 전체 흐름 (한눈에)

```text
① KIS 계정 + 모의투자 신청   →  ② KIS Developers 앱 등록(APP Key/Secret)
        →  ③ .env 에 키 입력  →  ④ 연결 테스트 CLI 로 모의 잔고 조회 확인
        →  ⑤ broker-chief 에이전트가 "제안 + 승인" 방식으로 계좌 관리
```

이 가이드는 ①~④. ⑤는 연결 확인 후 진행.

---

## 1. KIS 계정 + 모의투자 계좌 (실제 돈 X)

1. 한국투자증권 계좌가 없으면 먼저 개설 (한국투자 앱 / 영업점).
2. **모의투자 신청**: 한국투자 HTS(eFriend) 또는 홈페이지 → "모의투자" → 주식 모의투자 참가 신청.
   - 모의투자는 **가상 자금**이라 손실 위험이 없다. 연결·자동화 검증을 여기서 끝낸다.
3. 모의투자 **계좌번호**를 확인 (예: `50071023-01` 형태 → 앞 8자리 = `KIS_ACCOUNT_NO`, 뒤 2자리 = `KIS_ACCOUNT_PRODUCT_CODE`).

---

## 2. KIS Developers 앱 등록 (APP Key / Secret 발급)

1. **KIS Developers** 포털 접속: `https://apiportal.koreainvestment.com`
2. 로그인 (KIS 계정) → **API 신청 / 앱 등록** 메뉴.
3. 앱(App) 생성 시:
   - 사용할 **계좌번호 등록** (위 모의투자 계좌).
   - **모의투자(VPS) / 실전** 구분 선택 → **모의투자용으로 먼저 발급**.
4. 발급 결과로 받는 값:
   - **APP Key** (앱 키)
   - **APP Secret** (앱 시크릿) — 한 번만 보이는 경우가 있으니 안전하게 보관.
5. ⚠️ 이 두 값 + 계좌번호는 **비밀**. 메신저/캡처/코드에 붙여넣지 말 것.

> 모의투자와 실전은 **APP Key/Secret 이 서로 다르다.** 도메인도 다르다 (아래 §4).

---

## 3. `.env` 에 입력

프로젝트 루트 `.env` 에 아래 항목을 채운다 (값은 따옴표 없이):

```dotenv
KIS_MODE=paper
KIS_APP_KEY=발급받은_APP_KEY
KIS_APP_SECRET=발급받은_APP_SECRET
KIS_ACCOUNT_NO=12345678          # 계좌번호 앞 8자리
KIS_ACCOUNT_PRODUCT_CODE=01      # 뒤 2자리 (보통 01)
KIS_PAPER_BASE_URL=https://openapivts.koreainvestment.com:29443
KIS_LIVE_BASE_URL=https://openapi.koreainvestment.com:9443
```

> 템플릿: `config/portfolio/secrets.example.env`. 절대 git 에 커밋되지 않는다(.gitignore).

---

## 4. 도메인 / 한도 (검증됨 — `api_adapter.md §7`)

| | 모의(paper) | 실전(live) |
|---|---|---|
| REST | `openapivts.koreainvestment.com:29443` | `openapi.koreainvestment.com:9443` |
| WebSocket | `ops.koreainvestment.com:31000` | `ops.koreainvestment.com:21000` |
| 초당 호출 | **5건** | 20건 |
| 토큰 재발급 | 1분당 1회 | 1분당 1회 |

`KIS_MODE` 값으로 코드가 도메인·tr_id 를 자동 분기한다.

---

## 5. 연결 테스트

키를 채운 뒤 (모의투자 모드):

```powershell
python -m main_mission.portfolio_os.broker.kis_check
```

성공 시:
- ① OAuth 토큰 발급 OK
- ② 삼성전자(005930) 현재가 조회 OK
- ③ 모의투자 계좌 잔고 조회 OK (보유 종목/현금)

출력에는 **KIS 응답코드(rt_cd, msg)** 가 함께 표시되어, 키/계좌/권한 문제를 바로 알 수 있다.
APP Key/Secret/계좌번호/토큰은 **마스킹** 되어 로그에 원문이 남지 않는다.

자주 나오는 에러:
- `EGW00201` → 초당 호출 한도 초과 (잠시 후 재시도).
- 토큰 발급 실패 → APP Key/Secret 오타 또는 모의/실전 키 혼동.
- 잔고 rt_cd≠0 → 계좌번호(`KIS_ACCOUNT_NO`/`PRODUCT_CODE`) 또는 앱에 계좌 미등록.

---

## 6. 연결 후 — 계좌 관리 (제안 + 승인)

연결이 확인되면 broker-chief 에이전트가:
1. 잔고·시세 조회 (read)
2. 목표 비중 대비 drift 계산 → **리밸런싱 제안** 생성
3. **리스크 게이트**(`risk/gate.py`) hard-block 통과
4. **사장님(CEO) 승인** 후에만 주문 실행 (paper)

자동 주문(무승인)은 하지 않는다 (현재 설정: 제안 + 승인).
실전(live) 전환은 모의에서 충분히 검증 후, 별도 CEO 체크리스트로만.

---

## 7. 관련 파일

- 연결 정보·tr_id 매핑: [api_adapter.md](api_adapter.md)
- 안전 규칙: [safety_rules.md](safety_rules.md)
- broker 에이전트: [../../agents/portfolio/broker-chief.md](../../agents/portfolio/broker-chief.md)
- 자격증명 템플릿: [../../config/portfolio/secrets.example.env](../../config/portfolio/secrets.example.env)
- 어댑터 코드: [../../main_mission/portfolio_os/broker/](../../main_mission/portfolio_os/broker/)
