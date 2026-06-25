# Daily Review 운영화 (수동 → 루틴)

> Daily Review 를 매일 자동으로 **생성**(점검 행 만들기)하는 운영 가이드.
> **주문 자동 실행은 절대 없다.** 자동화 대상은 *점검(review) 행 생성*까지이며,
> 주문은 예약성 후보(plan)까지만 만들고 실제 체결은 사람 승인 + PIN + live lock 이후 단계다.

관련 코드:
- `main_mission/portfolio_os/daily_review.py` — 계좌 1개 Daily Review 생성(`generate_review`)
- `main_mission/portfolio_os/daily_runner.py` — **전 계좌 일괄 생성 CLI**(`run_all`)

---

## 1. 무엇이 자동화되는가 / 안 되는가

| 항목 | 자동? | 비고 |
|---|---|---|
| 전 계좌 Daily Review 행 생성 | O (cron 설치 시) | 멱등 — 계좌×일 1행 |
| 직전 미체결 후보 재평가(carry/expire) | O | **재평가일 뿐, 자동 주문 아님**(추격 금지) |
| 근거(evidence) 연결 | O | 메모리에 근거 있으면 링크, 없으면 정직 빈 목록 |
| 예약성 지정가 후보(plan/step) 생성 | O | `candidate` 상태까지만 |
| **실제 주문 전송/체결** | **X (절대)** | 사람 승인 + PIN + live lock 통과 후 별도 단계 |

불변 규칙:
- **주문 자동 실행 0.** `run_all` 의 결과에는 항상 `orders_executed: 0` 이 포함된다.
- **관망 정상.** 스냅샷/선택안 없음·stale 이면 `watch` 로 정직 보고(실패 아님).
- **live lock 유지.** 일괄 생성은 후보만 만들고 모드 전환을 하지 않는다.
- **stale fail-closed.** 스냅샷이 오래되면 `decision.compute` 가 차단 → review 는 `watch` + `no_trade_reason` 에 stale 명시.

---

## 2. 수동 실행

```bash
# 전 계좌(accounts 테이블 전체) 일괄 — 멱등
python -m main_mission.portfolio_os.daily_runner

# 단일 계좌만
python -m main_mission.portfolio_os.daily_runner --account 1

# 특정 날짜로(YYYY-MM-DD, 보통 생략)
python -m main_mission.portfolio_os.daily_runner --date 2026-06-21
```

출력(JSON) 예:

```json
{"ok": true, "accounts": 2, "generated": 2,
 "with_order_candidates": 0, "orders_executed": 0, "results": [...]}
```

`orders_executed` 는 항상 0 (불변). `generated` = review 행이 생성/갱신된 계좌 수.

---

## 3. cron 설치 (사용자 몫)

> **cron 실제 설치는 사용자가 직접 한다.** 아래는 권장 예시일 뿐이며,
> 시스템에 자동으로 등록하지 않는다(권한·환경 차이 때문).

평일(월~금) 오전 8시 KST 에 전 계좌 점검을 생성하는 예:

```cron
# crontab -e 로 편집 후 아래 줄 추가 (경로는 실제 환경에 맞게 수정)
0 8 * * 1-5 cd /home/cyj/Project/investclaude && /home/cyj/Project/investclaude/.venv/bin/python -m main_mission.portfolio_os.daily_runner >> /home/cyj/Project/investclaude/data/daily_review.log 2>&1
```

설치 절차:

1. `crontab -e` 실행
2. 위 줄을 붙여넣기 (cwd·venv 경로를 본인 환경에 맞게 확인)
3. 저장 후 `crontab -l` 로 등록 확인
4. 첫날은 로그(`data/daily_review.log`)로 정상 생성 여부 점검

주의:
- cron 의 TZ 는 시스템 시간대를 따른다. KST 가 아니면 `CRON_TZ=Asia/Seoul` 을 crontab 상단에 추가하거나 UTC 기준으로 시간 환산.
- `.env`(자격증명)는 cwd 기준으로 로드된다 → `cd` 로 프로젝트 루트를 먼저 잡을 것.
- 이 cron 은 **점검 행만 만든다.** 주문은 여전히 웹에서 사람이 승인해야 실행된다.

---

## 4. carry-over (미체결/보류 후보 재평가)

매 `generate_review` 는 직전 cycle 의 미체결(`candidate`/`hold`) 예약 step 을 조회해 재평가한다:

- **carry(이월)**: plan 나이가 `CARRY_OVER_EXPIRE_DAYS`(기본 5일) 이내 → 다음 cycle 에서 다시 평가.
- **expire(만료)**: 그 일수를 초과 → plan `expired` + step `blocked` 로 닫음(**추격 금지** — 재진입은 새 점검 후보로).

결과는 review payload 의 `carry_over` 블록에 담기며, 웹 `DailyReviewCard` 의 "직전 미체결 후보 재평가" 섹션에 표시된다. 어느 경우에도 **주문은 자동 실행되지 않는다**(상태 전이만).

---

## 5. 검증

```bash
.venv/bin/python -m pytest main_mission/portfolio_os/tests -q -p no:randomly
```

핵심 테스트(test_daily_review.py):
- `test_run_all_generates_review_per_account` — 전 계좌 review 생성 + `orders_executed==0`
- `test_run_all_is_idempotent` — 같은 날 재실행 시 1행 유지
- `test_carry_over_*` — 이월/만료 + **주문 비자동** 증거
- `test_stale_snapshot_marks_watch_with_reason` — stale → watch + 사유
- `test_evidence_*` — 근거 연결 / 정직 빈 목록
