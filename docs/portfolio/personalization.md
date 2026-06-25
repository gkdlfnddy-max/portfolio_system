# 통합 개인화 루프 (Track A)

> 사용자의 **선택 / 무시 / 수정** 신호를 누적해 *다음 조언의 표시 순서(ranking)* 에 반영한다.
> **자동 주문·자동 policy 는 절대 없다 — 순서만 바뀐다.** Anthropic / LLM API 미사용 (CLAUDE.md §17).

## 1. 위치

- 코드: [main_mission/portfolio_os/personalization.py](../../main_mission/portfolio_os/personalization.py)
- 배선: [theme_suggestions.py](../../main_mission/portfolio_os/theme_suggestions.py) (후보 ranking)
- 테스트: [tests/test_personalization.py](../../main_mission/portfolio_os/tests/test_personalization.py)
- 테이블: `personalization_weights` (스키마 편집 금지)

## 2. 테이블 `personalization_weights`

`UNIQUE(account_index, scope, key)` — 계좌×scope×key 당 한 행.

| 컬럼 | 의미 |
|---|---|
| `account_index` | 계좌별 격리 (교차 금지) |
| `scope` | `perspective` \| `theme` \| `advice_type` \| `candidate_type` \| `hedge` |
| `key` | 대상 키 (예: `C`(공격안)·`반도체`·`hedge`·`defensive`) |
| `accepted_count` / `ignored_count` / `modified_count` | 행동 카운트 |
| `last_reason` | 마지막 무시/수정 이유 |
| `weight` | 파생 가중 (>1 선호 · <1 비선호) — ranking 조정용 |

## 3. API

- `record_feedback(account, scope, key, action[accepted|ignored|modified], reason=None)`
  → upsert(카운트++ · weight 재계산). account 없으면 hard-block.
- `weight_for(account, scope, key) -> float` — 조회(미존재/계좌 없음 = 중립 1.0).
- `weights_map(account, scope) -> {key: weight}` — rank() 배치 조회.
- `rank(account, scope, items, key_field='key') -> list` — 개인화 가중 적용 정렬(표시순서만).

## 4. weight 산식 (베이지안 평활)

```
pos   = accepted + modified*0.4       # 수정 = 약한 긍정(관심 있으나 그대로는 아님)
neg   = ignored
score = (pos + PRIOR/2) / (pos + neg + PRIOR)   # PRIOR=2 평활 → 표본 적으면 0.5 근처
weight= clamp(1 + (score-0.5)*2*SPAN, 0.1, 2.0) # SPAN=0.9, score 0.5 → weight 1.0(중립)
```

- 선택 多 → weight > 1 (상향) · 무시 多 → weight < 1 (하향).
- **평활**: 한두 표본으로 과격하게 튀지 않음(예: accepted 1회 → ~1.2).

## 5. ranking 반영 (theme_suggestions)

`suggest()` 가 후보 생성 후 `_apply_personalization()` 으로 정렬:

- `candidate_type` 가중 × `theme`(candidate_theme) 가중을 confidence 에 곱해 `personalized_score` 산출.
- 정렬키: `(deprioritized, -personalized_score, -confidence)` — 반복무시는 여전히 후순위.
- 원본 `confidence` 보존, `personalization_weight` / `personalized_score` 만 부가(비파괴).
- 행동 기록(`record_action`) 시 `_record_personalization()` 으로 candidate_type·theme 두 축에
  피드백을 적재(`added_to_research`/`saved_to_policy`=accepted, `ignored`/`rejected`=ignored).

예: 반도체 hedge 수용·바이오 hedge 무시 → `theme` scope 가 분리돼 테마별 차등.
예: 공격안 C 반복 무시·방어안 B 선택 → `perspective`/`candidate_type` 가중으로 B 가 상단.

## 6. 불변 원칙

1. **계좌 격리**: 모든 함수 `account_index` 필수. 타 계좌 가중 미반영.
2. **공통 agent memory(lessons) 와 분리**: personalization_weights 에만 기록 — 그 계좌
   사용자 개인 선호만. agent 공통 노하우(lessons)는 절대 건드리지 않는다.
3. **자동 주문 / 자동 policy 0**: ranking(표시 순서)만 바꾼다. long/적용/주문 결정 없음.
4. **secret 0 · API 0**: 규칙(베이지안 평활)만. Anthropic SDK / 키 의존 없음.
