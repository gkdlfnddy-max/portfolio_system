# Evidence 요약 엔진 (자료 정리)

> 코드: [main_mission/portfolio_os/evidence_summary.py](../../main_mission/portfolio_os/evidence_summary.py)
> freshness/stale/decay 재사용: [main_mission/portfolio_os/evidence.py](../../main_mission/portfolio_os/evidence.py)
> 테이블: `evidence_items` (스키마 편집 금지)
> 테스트: [main_mission/portfolio_os/tests/test_evidence_summary.py](../../main_mission/portfolio_os/tests/test_evidence_summary.py)

## 0. 목적

사람은 모든 재무제표 · 기사 · 공시 · 리포트 · ETF 구성 · 거시지표 · 수급을 꼼꼼히 못 본다.
시스템이 대신 정리한다. 각 evidence = **{무엇이 새로 나왔나, 관련 종목/ETF/섹터,
긍정/부정/불확실, 단기/장기 영향, 내 포트폴리오 영향, 추가 확인 필요}**.

## 1. 불변 원칙

- **Anthropic API 미사용.** 분류·요약 구조화는 전부 키워드/룰 기반(`_POSITIVE_KW` / `_NEGATIVE_KW` / `_UNCERTAIN_KW` 등). LLM 호출 0.
- **근거 없는 강한 조언 금지.** eff_confidence 가 `STRONG_ACTION_MIN_CONF`(0.45) 미만이거나 상충/stale 이면 `suggested_action` 을 `watch_only` 로 약화한다.
- **출처/날짜/freshness/confidence 필수.** stale(`eff < 0.25`) 자동 표시.
- **가짜 evidence 생성 금지.** 실 자료 소스는 외부 커넥터 필요 → 수동 입력 + ingestion stub 으로 프레임만 완성하고, 미연동을 정직 표기.

## 2. API

| 함수 | 설명 |
|---|---|
| `summarize(evidence)` | 규칙기반 요약 → 긍정/부정/불확실·단기/장기 영향·portfolio_impact_hint·추가확인·stance·eff_confidence·stale·conflicting |
| `add_evidence(source_type, ...)` | `evidence_items` 적재. summarize 로 요인/영향/액션 채움. confidence 게이트 적용 → id 반환 |
| `evidence_for_account(account_index)` | 최신 snapshot 의 holdings + universe_instruments 와 `related_ticker/related_etf` 가 겹치는 evidence 만 추림. 타 계좌 격리(None=공통). conflicts·stale_count·data_source_status 포함 |
| `record_feedback(id, feedback)` | `accepted\|ignored\|modified\|rejected_as_wrong` 기록 |
| `source_type_trust(source_type)` | 피드백 누적 → multiplier ∈ [0.6, 1.3] (샘플<3 → 1.0 중립) |
| `effective_confidence(source_type, base, freshness_at)` | freshness decay × source_type 신뢰 보정 |
| `data_source_status()` | 실 자료 커넥터 연동 상태(정직 표기) |
| `ingest_stub(source_type, payload)` | 외부 커넥터 자리. 미연동이면 자동 적재 거부(가짜 evidence 방지) |

`source_type` 허용값: `financials | filing | news | sector | etf | macro | flow`.
stance 는 [evidence.py](../../main_mission/portfolio_os/evidence.py) 의 `VALID_STANCES` 와 호환
(`long_support | risk_warning | watch_only | conflicting_evidence | insufficient_evidence`).

## 3. freshness / stale / 상충

- freshness decay: `base * 0.5 ** (age_days / 90)` — [evidence.py](../../main_mission/portfolio_os/evidence.py) `decayed_confidence` 재사용. `source_date`(YYYY-MM-DD) 기준.
- stale: eff_confidence < `STALE_EFF_THRESHOLD`(0.25) → `evidence_items.stale = 1`.
- 상충: 같은 `related_ticker/etf` 에 긍정 요인 자료와 부정 요인 자료가 공존 → `conflicts[]` 표시, 강한 조언 보류.

## 4. 성장 (메모리)

새 API 0. `evidence_items` 자체 피드백 통계만 사용한다.
같은 `source_type` 의 `accepted/modified` 비율이 높으면 신뢰 multiplier ↑,
`ignored/rejected_as_wrong` 비율이 높으면 ↓. 다음 적재의 eff_confidence 에 반영.

## 5. 데이터 소스 현실 (정직 표기)

| 소스 | 상태 |
|---|---|
| 수동 입력 | available |
| DART 공시 | not_connected |
| 뉴스 API | not_connected |
| 재무제표 피드 | not_connected |
| 거시(ECOS) | not_connected |
| 수급(외인/기관) | not_connected |

실 자료 커넥터가 생기면 `ingest_stub` 의 connected 분기에서 `add_evidence` 를 호출하도록
배선한다. 그 전까지는 수동 입력만 동작하며, 자동 적재(가짜 evidence)는 거부한다.
관련: [data_architecture.md](data_architecture.md) §7-4 (DART/뉴스 sync job).
