# 키움증권(Kiwoom) REST API 연결 가이드

> 목적: AI 운영 매니저(broker-chief 에이전트)가 키움 계좌를 관리하려면 먼저 키움 REST API 앱키를 발급받아 연결해야 한다.
> **원칙: 무조건 모의투자(paper) 먼저.** 실전(live)은 CEO 승인 체크리스트 후에만.
> 자격증명은 `.env` 에만. 코드/DB/로그/메모리 저장 금지 (안전 §8).
> **멀티 브로커 원칙**: 키움은 KIS 와 **완전히 분리된 독립 어댑터**다 (KIS 코드에 키움 분기 없음).
> **현재 단계 = 조회(잔고/보유종목/현재가)만.** 주문(매수/매도)은 **2차** — 아직 열리지 않음.

---

## 0. 전체 흐름 (한눈에)

```text
① 키움 계정 + 모의투자 신청   →  ② 키움 REST 앱키 발급(APP Key / Secret Key)
        →  ③ .env 에 KIWOOM_ACCOUNT_{n}_* 입력  →  ④ 연결 테스트(잔고 조회 확인)
        →  ⑤ broker-chief 에이전트가 "제안 + 승인" 방식으로 계좌 관리(조회 단계)
```

이 가이드는 ①~④. 주문(⑤의 매매)은 별도 2차 단계.

---

## 1. 키움 계정 + 모의투자 신청

1. 키움증권 계좌 개설(실계좌). 모의투자는 별도 신청.
2. 키움 REST API 포털: <https://openapi.kiwoom.com/> 접속 → 로그인.
3. **모의투자 신청** (모의투자 전용 앱키가 별도로 발급됨). 실전 키와 혼용 금지.

## 2. REST 앱키 발급 (APP Key / Secret Key)

1. 키움 OpenAPI 포털에서 **REST API** 사용 신청.
2. 앱 등록 시 **APP Key** 와 **Secret Key** 를 발급받는다.
   - 모의투자(paper)용과 실전(live)용이 **다르다.** 모의부터 발급.
3. 발급된 키/시크릿은 **절대 코드/DB/메모리/로그에 두지 않는다.** `.env` 에만.

## 3. `.env` 입력 (계좌별)

루트 `.env` 에 아래를 채운다 (`config/portfolio/secrets.example.env` 참고). `{n}` 은 계좌 번호(1~50).

```dotenv
# 계좌 n 을 키움으로 운용 (sync_job 이 broker 자동 인식: KIWOOM_ACCOUNT_n_APP_KEY 존재 시 kiwoom)
KIS_ACCOUNT_{n}_BROKER=kiwoom          # (선택) 명시. 미지정이어도 키움 키 있으면 자동 kiwoom
KIWOOM_ACCOUNT_{n}_APP_KEY=...         # 키움 REST APP Key
KIWOOM_ACCOUNT_{n}_APP_SECRET=...      # 키움 REST Secret Key
KIWOOM_ACCOUNT_{n}_ACCOUNT_NO=...      # 계좌번호 (마스킹되어 DB 저장)
KIWOOM_ACCOUNT_{n}_MODE=paper          # paper|live (live 는 별도 확인 후)
```

> ⚠️ `live` 는 `KIS_LIVE_CONFIRM=I_UNDERSTAND` 가 있어야만 어댑터가 생성된다(키움도 동일 하드락).

## 4. 연결 테스트 (잔고 조회)

키 입력 후, 동기화 작업으로 토큰 발급 + 예수금 + 보유종목을 가져와 DB(운영 truth)에 저장한다.

```bash
.venv/bin/python -m main_mission.portfolio_os.broker.sync_job --account {n}
```

- 출력 JSON `ok:true` 면 연결 성공 (KIS 와 동일한 표준 결과 구조).
- `stage=credentials` → `.env` 키 미입력. `stage=token` → 키/모의신청 확인. `stage=balance` → 잔고 응답 파싱 확인.
- 비밀값(키/시크릿/토큰)은 **출력되지 않는다** (마스킹).

## 5. 동기화 흐름

```text
sync_job --account n
  └─ broker 자동 인식(kiwoom)
       └─ KiwoomRestAdapter (독립)
            ├─ ensure_token()      OAuth /oauth2/token
            ├─ get_cash_krw()      예수금 kt00001
            └─ get_balance()       계좌평가잔고내역 kt00018
       → account_snapshots / holdings 에 KIS 와 동일한 표준 구조로 저장
```

웹은 이 job 을 trigger 만 하고, 화면은 DB 에 저장된 결과를 조회한다 (웹 조회 전용).

## 6. 주문은 2차 (현재 미개방)

- `place_order` / `cancel_order` 는 **NotImplemented** 로 막혀 있다.
- 주문은 잔고/가격 검증 · risk gate · CEO 승인 · account PIN · live 하드락 해제 후 단계에서만 연다.
- 진입은 항상 지정가(예측 진입). 시장가 매수 영구 금지 (CLAUDE.md §16).

---

## 부록 A — 조사한 키움 REST 스펙 (2026-06, WebSearch/WebFetch 검증)

| 항목 | 값 | 상태 |
|---|---|---|
| Base URL (paper/모의) | `https://mockapi.kiwoom.com` | ✅ 검증 |
| Base URL (live/실전) | `https://api.kiwoom.com` | ✅ 검증 |
| 토큰 endpoint | `POST /oauth2/token` (au10001) | ✅ 검증 |
| 토큰 body | `{grant_type: client_credentials, appkey, secretkey}` | ✅ 검증 |
| 토큰 응답 | `token`, `token_type`, `expires_dt`(YYYYMMDDHHMMSS) | ✅ 검증 |
| TR 헤더 | `api-id` (+ `cont-yn`, `next-key` 페이징) | ✅ 검증 |
| 예수금 | `kt00001` @ `/api/dostk/acnt` | ✅ 경로 검증 |
| 잔고/보유종목 | `kt00018` @ `/api/dostk/acnt` | ✅ 경로 검증 |
| 현재가/주식기본정보 | `ka10001` @ `/api/dostk/stkinfo` | ✅ 경로 검증 |

### ⚠️ 사용자 확인 필요 (응답 필드명·rate limit)

공식 문서 직접 확인 후 `kiwoom_adapter.py` / `kiwoom_client.py` 에서만 갱신:

- **응답 필드명**: kt00018 잔고 list 키(`acnt_evlt_remn_indv_tot` 등)와 항목 필드
  (`stk_cd`, `stk_nm`, `rmnd_qty`, `pur_pric`, `evlt_amt`), kt00001 예수금(`entr`),
  ka10001 현재가(`cur_prc`) 는 공식 문서 기준이나 **일부는 재확인 권장**.
  현재 코드는 `_pick()` 로 후보 키를 순회해 필드명 변동에 견디게 했다(임의 추측 아님, 폴백).
- **rate limit**: 키움 공식 초당 호출 한도 미확정 → 보수적으로 5/s(헤드룸 25%) 적용.
  공식 수치 확인 시 `kiwoom_client.RATE_LIMIT_PER_SEC` 갱신.
- **expires_dt 타임존**: KST 가정. 실제 응답 확인 후 보정 권장(현재는 보수적 만료 처리).

출처:
- 키움 OpenAPI 포털: <https://openapi.kiwoom.com/>
- 참고 Python 래퍼(필드/경로 교차검증): younghwan91/kiwoom-rest-api, bamjun/kiwoom-rest-api (GitHub)
