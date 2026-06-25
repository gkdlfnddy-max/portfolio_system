# 관점별 포트폴리오 후보 (A/B/C안) — 같은 데이터 다른 해석

> **하나의 정답 금지.** 같은 데이터·같은 견해라도 *관점*에 따라 다르게 해석된다.
> 각 계좌에 정답 1개가 아니라 **관점별 후보 3안**을 제시하고, **사람이 골라 승인**해야 반영된다.

코드: [`main_mission/portfolio_os/perspective_variants.py`](../../main_mission/portfolio_os/perspective_variants.py)
관련: `allocation.py`(비중 base 로직) · `portfolio_impact.py`(다른 해석 출력) · `user_views`(견해) · `investor_objective`(목적, 병렬 A 에이전트)

---

## 1. 세 가지 관점

| 안 | 관점 | 비중 방향 |
|---|---|---|
| **A** | 사용자 **현재 관점** best (견해·성향·목적에 가장 충실) | 현재 관점 기준(목적 lean 반영) |
| **B** | 조금 더 **방어적** | 현금/채권↑·위험자산↓ (drawdown 보호) |
| **C** | 조금 더 **공격적** | 위험자산/테마 tilt↑ (단, 한도·risk gate 준수) |

- **수익률 최대화가 아니다.** 목적(`investor_objective`)에 맞춘 최선이다.
  목적이 '손실 축소'면 C 라도 *목적 안에서 절제*된다(정직 표기).
- 비중은 `allocation._variant`(검증된 base 로직)을 재사용 → **합계 100·섹터상한·인버스 한도** 동일 보호.

### 각 안에 반드시 담기는 것
`{요약 · 왜 이 안이 사용자 관점에 맞는지 · 비중(현금/채권/위험자산/테마/헤지) · 장점 · 위험 · 언제 이 안이 깨지는지(break_triggers) · 추가 확인할 자료(more_to_confirm)}`

---

## 2. 관점 강도는 현금 수준으로만 단조롭게

관점 차이는 **현금 수준(cash_pct)** 으로만 준다(B 현금↑, C 현금↓).
tilt-share 를 관점마다 다르게 두면 *테마가 없을 때* 잔여 tilt 가 현금으로 흡수되며
방어 순서가 역전된다. 그래서 tilt-share 는 `'base'` 로 고정한다.

보장:  `B 방어 ≥ A ≥ C 방어`  /  `C 위험 ≥ A ≥ B 위험` (테마 유무 무관).
C 는 현금↓ → invested↑ → 테마/앵커 절대비중이 더 커진다.

현금 매핑(`_perspective_cash`): A=목적 lean 반영 현금, B=A와 상한 사이, C=A와 하한 사이.

---

## 3. 같은 데이터 다른 해석 출력

`portfolio_impact.different_interpretations(account_index)` — 고정 순서 블록:

```
공통 사실(common_facts)        — 관점과 무관한 측정값(보유/견해/자료/하락신호)
사용자 관점(user_perspective)  — 견해 요약 + 목적(있으면)
관점에 따른 해석(interpretations) — 종목별 일치/충돌; 충돌이면 단정 금지·mixed_swing
포트폴리오 영향(portfolio_impact)
선택 가능 후보(selectable_candidates)  — A/B/C
각 후보 장단점(candidate_pros_cons)
사용자 승인 필요(requires_user_approval=true)
```

- 관점 충돌(사용자 장기긍정 ↔ 단기 하락신호)이면 **매도/매수 단정 금지** →
  `mixed_swing`(long 유지 + 분할매수 + hedge 검토) 구조로 제시.

---

## 4. 자동 차단 (불변 — CLAUDE.md §2, §4)

- A/B/C 는 전부 **draft 후보**다. `target_allocations(status='draft', variant=A|B|C)` 로만 저장.
- **사람이 한 안을 골라 승인**해야 policy/비중에 반영된다.
- `auto_order_created=false` · `auto_applied=false` · `requires_user_approval=true`.
- draft 저장은 `compile_policy`(accepted 만 읽음)를 **바꾸지 않는다** — 자동 적용 차단 증거(test).

---

## 5. 목적 미설정 (graceful)

`investor_objective` 테이블/행이 없으면(병렬 A 에이전트가 생성 중):
`objective.set=false`, note=`"목적 미설정 — A안은 견해만 반영, 목적 입력 시 정교화"`.
가짜 목적을 만들지 않는다(정직).

---

## 6. 실행

```bash
.venv/bin/python -m main_mission.portfolio_os.perspective_variants --account 1 --generate
.venv/bin/python -m main_mission.portfolio_os.portfolio_impact --account 1 --interpretations
```

테스트: [`tests/test_perspective_variants.py`](../../main_mission/portfolio_os/tests/test_perspective_variants.py)
