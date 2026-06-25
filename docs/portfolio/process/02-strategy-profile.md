# Strategy / Investor Profile Agent 시스템 프로세스 정리

> 영역: 자연어 컨셉 입력 → 대전제(운용방식)·중전제(관심/생각) 추출 → 구조화 변수 저장 → policy object 컴파일 → 버전 이력 → 전략 뷰어 → 보완 코칭 → 사람 수정.
> 코드 근거: `main_mission/portfolio_os/profile.py`, `main_mission/portfolio_os/policy.py`, `main_mission/portfolio_os/store/schema.sql`, `web/app/api/accounts/[id]/profile/route.ts`, `web/app/api/profile/distill/route.ts`, `web/app/accounts/[id]/strategy/page.tsx`, `web/app/accounts/[id]/strategy/view/page.tsx`, `web/lib/server/portfolioDb.ts`.

---

## 1. 목적

CEO 의 투자 사고 위계(§2.5 대전제→중전제→소전제) 중 **소전제(종목) 직전 단계**를 책임진다. 자유 입력(컨셉)을 받아 운용 성향·숏 정책·현금밴드·기간·지역·조정 pace·개별주 한도를 구조화하고, 이를 decision engine 이 그대로 쓰는 **정책 객체(policy object)** 로 승격(컴파일)한다.

- 단기 trading 이 아니라 **포트폴리오 비중 관리 + 분할 리밸런싱**의 기준선(현금밴드·pace·한도)을 정의한다.
- "메모리로 성장하는 에이전트" 원칙: 1차 정리는 `profile.distill()` 규칙 기반(즉시), 깊은 다듬기는 Claude+메모리가 수행(Anthropic API 미사용).
- 모든 전제 변경은 append-only 이력으로 추적, policy 는 version 으로 추적 → decision provenance 가 `policy_version` 을 남길 수 있게 한다.

---

## 2. 전체 흐름

```text
[웹 UI strategy/page.tsx]  사용자 자유 컨셉 입력(posture_text)
        │  POST /api/profile/distill { text }
        ▼
[profile.py distill()]  규칙 기반 1차 정리 → suggested/keywords/gaps/current_cash_hint (저장 X, 제안만)
        │  (사용자가 폼 값 확인·수정, 보완점 코칭 확인)
        │  POST /api/accounts/[id]/profile { ...form, doc }
        ▼
[profile.py save()]  investor_profile UPSERT + investor_profile_history append (매 저장 1행)
        │
        ▼  (CLI 전용 — 웹 route 없음)
[policy.py compile_policy()]  investor_profile + RiskLimits → policy object
[policy.py save()]            portfolio_policies version+1 append
        ▼
[웹 UI strategy/view/page.tsx]  getProfile / getProfileHistory / getLatestPolicy 조회(DB truth, 읽기전용)
        │
        ▼
[다음 영역]  /accounts/[id]/allocation (목표비중 3안) — 본 영역 범위 밖
```

웹은 DB truth 조회·쓰기 트리거만. 실제 DB 쓰기는 Python(`profile.py`)이 수행하며 web route 는 `execFile` 로 Python 모듈을 호출한다(`web/app/api/accounts/[id]/profile/route.ts` POST).

---

## 3. 입력

| 입력 | 경로 | 형태 |
|---|---|---|
| 자유 컨셉(대전제 원문) | UI `Textarea#posture` → `posture_text` | 한국어 자유 문장 |
| distill 요청 | `POST /api/profile/distill { text }` | text 필수, 계좌 무관 |
| 구조화 변수(폼) | `POST /api/accounts/[id]/profile` body | `risk_tolerance, short_policy, cash_min_pct, cash_max_pct, horizon, interests_text, views_text, individual_cap_pct, individual_count, region_pref, rebalance_pace` |
| doc(하이브리드 문서) | 동 body `doc` | `{ keywords, gaps, updated_from }` JSON (UI 가 distill 결과를 담아 전송) |
| refined_by | 동 body | `user` | `claude_agent` (기본 `user`) |
| 정책 컴파일 트리거 | `policy.py --account N --compile` (CLI) | account_index |

distill 추출 규칙(`profile.py`): 정규식 기반. risk(공격/방어/중립), short(숏·인버스 → none/insurance/active), 현금밴드(`현금 NN~MM`), 현재현금(`지금/현재 NN%`), horizon, themes(`THEME_KEYWORDS`: 로봇/바이오/양자컴퓨터/AI/반도체/2차전지/우주항공/방산/에너지), individual_cap_pct, individual_count, region_pref(전세계/미국/국내), rebalance_pace(slow/normal/fast).

---

## 4. 출력

| 출력 | 생성 함수 | 소비처 |
|---|---|---|
| distill 제안 | `profile.distill()` → `{ ok, suggested{...}, keywords[], gaps[], current_cash_hint, note }` | UI 폼 자동채움 + 키워드칩 + 보완코칭 |
| 저장 결과 | `profile.save()` → `{ ok, account_index, version_saved }` | UI 저장 확인 |
| 프로필 1행 | `profile.get()` / web `getProfile()` | strategy/page(편집 로드), strategy/view |
| 변경 이력 | `profile.history()` / web `getProfileHistory()` | strategy/view 버전 목록 |
| 정책 객체 | `policy.compile_policy()` → `{ risk_tolerance, horizon, region_pref, pace, cash_band{min,max,target}, limits{...}, forbidden_assets, compiled_at }` | decision engine, strategy/view 표시 |
| 정책 저장 | `policy.save()` → `{ ok, version, policy }` | `getLatestPolicy()` |

policy object 의 `cash_band.target` 산식: 밴드 안에서 성향별 — aggressive=하한, defensive=상한, neutral=중간(`policy.py` L43-46). `forbidden_assets`: short_policy=="none" 이면 `["inverse"]`.

---

## 5. DB 테이블

`store/schema.sql` 기준 (SQLite, `data/portfolio.sqlite3` = 운영 truth).

| 테이블 | 역할 | 핵심 컬럼 |
|---|---|---|
| `investor_profile` | 계좌별 1행(PK=account_index). 대전제+중전제+다운스트림 변수+doc | `posture_text, risk_tolerance, short_policy, cash_min_pct, cash_max_pct, horizon, interests_text, views_text, individual_cap_pct, individual_count, region_pref, rebalance_pace, doc(JSON), refined_by, updated_at` |
| `investor_profile_history` | append-only 변경 이력(되돌리기·감사) | `id, account_index, snapshot(JSON 전체), source, created_at` |
| `portfolio_policies` | 컴파일된 정책 객체, version 관리 | `id, account_index, version, policy(JSON), source, created_at` |

- `investor_profile` 는 1 entity = 1 table 규칙 충족. UPSERT(`ON CONFLICT(account_index) DO UPDATE`)로 항상 1행 유지.
- `investor_profile_history` 는 `save()` 호출마다 `get_in_tx()` 로 읽은 현재 스냅샷을 1행 적재(`profile.py` L223-228).
- `portfolio_policies.version` 은 `COALESCE(MAX(version),0)+1` 로 증가(`policy.py` L82-85).
- 자격증명(키/시크릿/계좌번호 평문)은 본 테이블 어디에도 저장 안 함.

---

## 6. API / 함수

### Python (백엔드, 쓰기 권한)
- `profile.distill(text) -> dict` — 규칙 기반 1차 정리(저장 X).
- `profile.get(account_index) -> dict|None` — investor_profile 1행.
- `profile.save(account_index, data) -> dict` — UPSERT + history append (트랜잭션 내).
- `profile.get_in_tx(conn, account_index)` — 트랜잭션 내 재조회(history 스냅샷용).
- `profile.history(account_index, limit=20) -> list`.
- `profile.main()` — CLI: `--distill TEXT` | `--account N --get` | `--account N --json PAYLOAD` | `--account N --history`.
- `policy.compile_policy(account_index) -> dict` — investor_profile + `RiskLimits` → policy.
- `policy.save(account_index, policy, source="user") -> dict` — version+1 append.
- `policy.latest(account_index) -> dict|None`.
- `policy.main()` — CLI: `--account N --compile` | `--account N --get`. (required `--account`)

### Web API (조회 + 쓰기 트리거)
- `GET /api/accounts/[id]/profile` → `getProfile(id)` (DB 직접 조회).
- `POST /api/accounts/[id]/profile` → `execFile(python, profile.py --json payload)` (Python 이 DB 기록).
- `POST /api/profile/distill { text }` → `execFile(python, profile.py --distill text)` (저장 X).

### Web 서버 조회 함수 (`web/lib/server/portfolioDb.ts`, readOnly)
- `getProfile(index)` — `SELECT * FROM investor_profile WHERE account_index=?`.
- `getProfileHistory(index, limit=20)` — `investor_profile_history ... ORDER BY id DESC`.
- `getLatestPolicy(index)` — `portfolio_policies ... ORDER BY version DESC LIMIT 1`, policy JSON.parse.

---

## 7. UI 화면

| 화면 | 경로 | 성격 | 기능 |
|---|---|---|---|
| 전략 편집 | `/accounts/[id]/strategy` (`strategy/page.tsx`, client) | 입력/편집 | 컨셉 입력, "대전제 정리"(distill 호출), 칩/숫자 폼 편집, 저장, 다음 단계 링크 |
| 전략 정리 문서 | `/accounts/[id]/strategy/view` (`strategy/view/page.tsx`, server, force-dynamic) | 읽기전용 | 컨셉 원문, 핵심 변수표, 컴파일된 정책값(vN), 키워드, 보완점, 변경 이력 |

- 편집 화면 distill 결과는 폼 자동채움 + 키워드칩 + "보완하면 좋을 점"(gaps) 코칭 카드로 표시(`strategy/page.tsx` L156-178).
- 저장 시 keywords/gaps 를 `doc` JSON 으로 묶어 함께 저장(L78-83) — 단단한 변수=컬럼, 진화 내용=문서 하이브리드.
- view 화면은 전부 DB 저장값 조회(`getProfile/getProfileHistory/getLatestPolicy`). mock/하드코딩 표시값 없음.

---

## 8. 상태 전이

본 영역에 명시적 status 컬럼은 없다. 다음과 같은 **버전·존재 상태**로 전이한다.

```text
[프로필 없음]  getProfile=null → view 는 "아직 저장된 전략이 없습니다"
     │ save()
     ▼
[프로필 v1]  investor_profile 1행 + history #1
     │ save() (수정)
     ▼
[프로필 갱신]  같은 행 UPSERT(updated_at 갱신) + history #N append (이전 행 유지)
     │ policy.compile + save
     ▼
[정책 v1..vN]  portfolio_policies version 증가, latest = 최대 version
```

`refined_by`/`source` 값으로 변경 주체를 구분: `user`(직접수정) / `claude_agent`(메모리 에이전트 정리) / `distill`(주석상 명시되나 현재 web POST 는 `user` 기본 — §14 참조).

---

## 9. 예외 / 실패 케이스

| 케이스 | 처리 | 근거 |
|---|---|---|
| 잘못된 계좌 id(<1, 비정수) | `400 invalid id` | profile route `accId()` |
| distill text 빈값 | `400 text 필요` | distill route L13 |
| python 실행파일 미발견(ENOENT) | 다음 후보(python/python3/py) 시도, 모두 실패 시 `500 python 미발견` | route 루프 |
| Python 내부 오류 | `{ ok:false, error:"내부 오류: ..." }` 반환(코드 0) | `profile.main()`/`policy.main()` try/except |
| DB 파일 없음 | web 조회 함수는 빈 배열/null 반환(throw 안 함) | `portfolioDb.open()`→null, `query()`→[] |
| doc JSON 파싱 실패(view) | try/catch → doc=null, 키워드/gaps 빈 처리 | view L29 |
| individual_count 비정수 | NULL 저장 | `save()` L215 isdigit 가드 |
| 숫자 변환 실패 | `_num()` → None | profile.py L174-178 |

---

## 10. Hard-block 조건

**본 영역 자체에는 주문 hard-block 이 없다(해당 없음에 가까움)** — 주문 차단은 risk 게이트 영역(`risk/gate.py`) 소관. 다만 본 영역이 **하류 hard-block 의 입력값(정책 한도)을 공급**한다:

- `compile_policy()` 가 `RiskLimits` 기본값을 정책 limits 로 박아 넣는다: `single_name_max_pct(20)`, `inverse_max_pct(short_max_pct=10)`, `leverage_max_pct(15)`, `one_order_cap_pct(single_order_max_pct=5)`, `cash_min_pct(10)`. (`policy.py` L60-71)
- `sector_max_pct=30`, `country_max_pct=70`, `currency_max_pct=80` 은 정책에서 직접 고정.
- short_policy=="none" → `forbidden_assets=["inverse"]` 로 인버스 진입 금지 신호.
- 공통 원칙상 **목표비중 없이 주문후보 금지 / 사람 승인 없이 주문 금지 / live 는 KIS_LIVE_CONFIRM 없이 하드차단**은 본 영역 다운스트림(allocation·decision·order·broker)에서 적용된다.

---

## 11. 로그 / 감사 기록

- **전제 변경 이력**: `investor_profile_history` 에 매 저장마다 전체 스냅샷 append (되돌리기/감사 근거). source 로 변경 주체 기록.
- **정책 버전**: `portfolio_policies` version+source+created_at append (decision provenance 가 policy_version 참조 가능하도록).
- `audit_logs` 테이블은 schema 에 존재하나 **본 영역(profile/policy) 코드는 audit_logs 에 기록하지 않는다** — 현재 audit 기록은 주문/승인/차단 영역 중심(§14 미구현 항목).
- web 조회는 readOnly 연결(`new DatabaseSync(..., {readOnly:true})`)이라 부수효과 없음.

---

## 12. 테스트 기준

- **현재 본 영역 전용 자동화 테스트는 없음**(`main_mission/portfolio_os/tests/` 에는 `test_risk_gate.py`, `test_order_safety.py` 만 존재. profile/policy/distill 테스트 파일 없음 — §14).
- 충족해야 할 기준(권장):
  1. `distill()` 정규식: "공격적, 현금 20~40%, 지금 50%, 로봇·바이오·양자, 개별주 3종목 10%" 입력 시 suggested 각 필드와 gaps(현재현금>상한, 하락장 트리거 없음 등) 정확 추출.
  2. `save()` 가 UPSERT 후 history 행이 1 증가하는지(append-only).
  3. `compile_policy()` cash_band.target 성향별(aggressive=min, defensive=max, neutral=중간) 정확.
  4. short_policy=none → forbidden_assets 에 inverse 포함.
  5. `save()`(policy) version 단조 증가.
- 수동 검증 CLI: `python -m main_mission.portfolio_os.profile --account 1 --get`, `... --distill "텍스트"`, `python -m main_mission.portfolio_os.policy --account 1 --compile`.

---

## 13. 현재 구현 상태

**구현 완료:**
- `profile.distill()` 규칙 기반 추출(risk/short/현금밴드/현재현금/horizon/themes/개별주/지역/pace) + keywords + gaps 코칭.
- `profile.save()` UPSERT + `investor_profile_history` append. `get`/`history`/`get_in_tx`.
- `policy.compile_policy()` + `save()`(version) + `latest()`. RiskLimits 한도 주입, cash_band.target 성향별, forbidden_assets.
- DB 스키마 3테이블(`investor_profile`, `investor_profile_history`, `portfolio_policies`) 정의 완료.
- Web: `GET/POST /api/accounts/[id]/profile`, `POST /api/profile/distill` 동작. 쓰기는 Python execFile 위임(웹 직접쓰기 안 함).
- Web 조회: `getProfile`, `getProfileHistory`, `getLatestPolicy` (readOnly).
- UI: `strategy/page.tsx`(편집+distill+코칭), `strategy/view/page.tsx`(읽기전용 문서+정책값+이력). mock/하드코딩 표시 없음.

**원칙 준수 확인:**
- 웹 DB truth 조회만 / 쓰기는 Python: 충족.
- Anthropic API 미사용(규칙 기반 distill, 깊은 정리는 Claude+메모리): 충족.
- 한글 문서/영문 코드: 충족.
- 전제 변경·정책 version provenance 기록: 충족.

---

## 14. 미구현 / placeholder

- **policy 컴파일 web route 없음**: `policy.py` 는 CLI 전용. `web/app/api/` 하위에 policy 관련 route 가 없어, UI 에서 "정책 컴파일" 버튼으로 호출할 경로가 없다. view 화면은 이미 컴파일된 `getLatestPolicy()` 만 읽는다 → 정책 생성은 수동 CLI 의존.
- **본 영역 전용 자동화 테스트 부재**: distill/save/compile_policy 회귀 테스트 없음.
- **audit_logs 미연동**: 프로필 저장/정책 컴파일이 `audit_logs` 에 INFO 레벨로 남지 않음(이력은 history/policies 테이블에만).
- **refined_by="distill" 미사용**: schema/주석은 source 로 `distill` 을 상정하나, web POST route 는 `refined_by` 기본 `user` 로 보냄 → distill 로 자동채움 후 저장해도 source 가 `user` 로 기록.
- **Claude+메모리 깊은 코칭 미구현(설계상 placeholder)**: distill 의 gaps 는 규칙 기반 고정 문구. "메모리로 성장"하는 동적 코칭(lessons 참조)은 아직 코드로 연결 안 됨.
- **doc 자유문서 활용 제한**: 현재 `doc` 에는 keywords/gaps/updated_from 만 저장(UI L79). 지역분배·Claude 노트·lesson 참조 등 schema 주석이 예고한 항목은 미적재.
- **short_policy=active 의 정책 반영 없음**: forbidden_assets 는 none 만 처리, active/insurance 차등은 policy 에 미반영.
- **views_text 다운스트림 미연결**: 중전제 견해는 저장만 되고 policy/allocation 으로 연결되는 코드 경로는 본 영역에 없음(다음 영역 의존).

---

## 15. 다음 개선 항목

1. `POST /api/accounts/[id]/policy/compile` route 추가 → UI "정책 컴파일" 버튼으로 `policy.py --compile` 호출(CLI 의존 제거).
2. `tests/test_profile.py`, `tests/test_policy.py` 추가(§12 기준 1~5).
3. 프로필 저장·정책 컴파일 시 `audit_logs` INFO 적재(actor, action="profile_save"/"policy_compile", entity_type, payload 요약).
4. distill 자동채움 후 저장 시 `refined_by="distill"` 전달해 변경 주체 구분 정확화.
5. gaps 코칭을 `lessons`/`lesson_candidates` 참조 동적 코칭으로 승격(메모리 성장 연결).
6. short_policy=insurance/active 를 inverse_max_pct 차등으로 policy 에 반영.
7. policy 컴파일 시 현재 investor_profile 스냅샷·history id 를 policy provenance 에 링크(어느 전제 버전에서 나온 정책인지).

---

## 16. 다른 Agent와의 의존성

| 상대 영역 | 방향 | 인터페이스 |
|---|---|---|
| 계좌/Sync 영역 | 상류 | `account_index` 존재 전제(`accounts` 테이블). 본 영역은 잔고를 직접 안 읽음. |
| Risk Gate 영역 | 상류(값 공급) | `policy.compile_policy()` 가 `risk/gate.py RiskLimits` 기본값을 정책 limits 로 주입. |
| 목표비중/Allocation 영역 | 하류(주 소비처) | policy object(cash_band/limits/forbidden_assets/pace) + interests_text/views_text 를 받아 3안 생성. UI 흐름상 `/accounts/[id]/allocation` 이 본 화면의 "다음" 링크. |
| Decision/Rebalance 영역 | 하류 | `policy_version` 을 decision provenance 로 참조(설계). pace 가 분할 회차 산정 기준. |
| Order/Broker 영역 | 하류(간접) | 본 영역이 정한 한도가 주문 전 hard-block 입력. 직접 호출 관계는 없음. |
| Memory(Claude) | 양방향 | distill 1차 → Claude+메모리 깊은 정리(refined_by=claude_agent). lessons 로 코칭 성장(미구현 연결). |

본 영역은 **위계의 시작(대/중전제)을 DB truth 로 고정**하고, 하류(소전제·목표비중·decision·order) 전체가 이 policy object 를 단일 기준으로 참조한다.
