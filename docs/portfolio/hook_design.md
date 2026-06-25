# Portfolio OS — Prehook / Posthook 설계

> 모든 중요한 판단(목표 비중·리밸런싱·리스크·주문) 전후에 hook 을 적용.
> 원칙: prehook 은 **현재 의사결정에 필요한 핵심만 압축** 인출. 메모리 오염 금지.
> hook 은 투자 의사결정에 **실제로 사용**된다 (완료 기준 6·7).

---

## 0. 투자 의사결정용 Prehook / Posthook (재정의)

### Prehook — 판단 전 인출 (T4·T6·T8·T9·T12 직전)
모든 중요한 판단 전, 아래 8가지를 **압축**해 주입한다 (top-k, token budget soft 8k).

| 인출 항목 | 소스 | 주 사용 task |
|---|---|---|
| CEO 투자 원칙 | memory_docs(ceo_preferences) | T4·T6 |
| 현재 포트폴리오 | RDB balances/snapshots | T2·T6·T7 |
| 최근 시장 상황 | RDB quotes + T1 결과 | T5·T6 |
| 관련 lesson | Index(FTS)+Vector(2-stage rerank) | T4·T8·T9 |
| 리스크 한도 | RDB risk_limits (SSOT) | T9 |
| 과거 유사 판단 | Vector(pgvector) + Graph(memory_links) | T4·T6 |
| 시장별 chief 의견 | T5 산출(korea/us/global) | T6 |
| 주문 가능 시간·환율·API 상태 | broker.is_healthy·time_guards·fx | T12 |

### Posthook — 판단 후 기록·학습 (각 task 직후)
| 기록 항목 | 저장 위치 | 주 task |
|---|---|---|
| 제안 결과 | rebalance_proposals + audit_logs | T8 |
| CEO 승인/거절 이유 | approvals + audit_logs | T11 |
| 주문 결과 | orders/order_events + audit_logs | T12 |
| 체결 결과 | fills + order_events | T13 |
| 성과 결과 | portfolio_snapshots/metrics | T14 |
| 실패/성공 lesson | lessons (reflection 7질문) | T15 |
| 다음 판단용 knowhow 후보 | lessons(stage 승격) | T16 |

> 핵심: posthook 이 남긴 lesson/knowhow 는 다음 사이클 **prehook 의 '관련 lesson·과거 유사 판단'으로 다시 인출**된다 → 학습 회로 완성(완료 기준 7).

---

## 1. 메모리 소스 구분 (어떤 DB 를 언제)

| 저장소 | 무엇 | prehook 에서 언제 인출 |
|---|---|---|
| **RDB** (`portfolio_os_db`) | 현재 잔고·목표·리스크 한도·미체결·최근 주문 | 항상 (현재 상태) |
| **Index DB** (PG FTS `tsvector`) | 과거 lesson/노하우 키워드 검색 | 컨셉/종목 키워드 매칭 시 |
| **Vector DB** (`pgvector`) | 의미 유사한 과거 의사결정·실패 사례 | 컨셉 의미 유사 검색 |
| **Graph DB** (`memory_links` recursive CTE) | "이 전략 → 이 실패 → 이 교훈" 연결 | 전략-결과 인과 추적 시 |

**압축 규칙**: prehook 은 위에서 **top-k(기본 4)** + 현재 리스크 한도만. 전체 history dump 금지.

---

## 2. 단계별 Prehook (인출)

| Task | prehook 이 불러오는 핵심 |
|---|---|
| T4 target_weights | 기존 투자 원칙, **현재 risk_limits**, 최근 CEO 피드백, 유사 과거 컨셉의 결과 lesson |
| T6 rebalance_propose | 현재 포트폴리오 상태, drift band 설정, 과거 "과도한 거래" lesson, 거래비용 |
| T7 risk_check | **risk_limits 전체(SSOT)**, 과거 한도 위반 사례, 현재 현금/숏/레버리지 비중 |
| T9 order_execute | broker is_healthy, 미체결 주문, 장 운영시간, 동일 client_order_id 존재 여부 |

prehook 출력 = `{retrieved_context, active_limits, warnings}` (압축 JSON, token budget soft 8k).

---

## 3. 단계별 Posthook (기록 + 학습)

| Task | posthook 이 기록하는 것 |
|---|---|
| T6 propose | 제안 내용·근거·drift → `rebalance_proposals` + audit_log |
| T7 risk_check | pass/fail·위반 enum → `risk_checks`. fail 이면 **lesson_candidate** 자동 생성 |
| T8 approval | CEO 결정·사유 → `approvals` + audit_log |
| T9/T10 order/fill | 주문 결과·체결·실패원인 → `orders`/`fills` + audit_log |
| T11 record | 수익/손실·비중 변화 → `portfolio_snapshots` |
| T12 reflect | reflection 7질문 + 사용자 피드백 → `lessons` |

---

## 4. Reflection 7질문 (투자 맥락 회고 양식)

```
q1_what_was_done      어떤 비중 변경/주문을 했나
q2_what_worked        효과 있던 판단 (예: drift band 가 과매매 방지)
q3_what_failed        틀린 가정/체결 실패/리스크 오판
q4_next_action        다음 사이클 개선
q5_memory_actually_used  실제로 쓴 과거 lesson
q6_memory_unused      인출했지만 안 쓴 것 (precision 측정)
q7_hook_gap           hook 이 놓친 것 (개선 후보)
```

---

## 5. Lesson 승격 파이프라인

```text
raw_event → reflection → lesson_candidate → validated_lesson → knowhow → SOP → (안전수칙은) risk_limit
```

- 승격 트리거: 동일 패턴 **recurrence ≥ 3** 또는 손실 유발 critical.
- 예: "장 시작 직후 시장가 미국주문 → 슬리피지 큼" 이 3회 반복 → knowhow → risk_limit(`no_market_order_near_open`) 승격(CEO 승인).
- **일회성 로그**(단순 조회 성공)는 reflection 까지만, 승격 안 함 → 메모리 오염 방지.

---

## 6. Hook ↔ 안전 게이트 (block_on_failure)

posthook 이 다음을 감지하면 **세션 차단**:
- `broker_unhealthy` (API 장애)
- `risk_hard_block` (한도 위반)
- `duplicate_client_order_id` (중복 주문 시도)
- `mode_mismatch` (paper 의도인데 live adapter)
- `secret_in_log` (자격증명 노출)

연결: [safety_rules.md](safety_rules.md) · [task_tree.md](task_tree.md)
설정 위치(추후): `config/portfolio/hook_profiles/`.

---

## Wave 1 개선 (memory-chief + code-architect-chief 자료조사, 2026-06-19)

### 7. Prehook 인출 = 2-stage (즉시반영 — 정밀도↑, 압축 유지)
출처: RAG 2-stage retrieval + reranking. 현재 top-k=4 단순 인출 → precision 저하(q6 미사용↑).
- **Stage1 과인출**: FTS + pgvector로 후보 k=20.
- **Stage2 rerank**: 현재 task 컨텍스트(task_type, 대상 instrument, active risk_limits 위반종류)로 LLM-as-reranker(haiku) 재순위 → **최종 top-4만** 주입. token budget soft 8k 유지.

### 8. Lesson 양방향 신뢰도 (plan_required — 스키마 변경, migration 002)
출처: ExpeL(UPVOTE/DOWNVOTE, importance count 0이면 제거), Reflexion.
- `lessons`에 `support_count / refute_count / confidence(=support/(support+refute)) / last_validated_at` 추가.
- **승격 기준 강화**: recurrence≥3 **AND** confidence≥0.6. (loss-유발 critical은 1회도 즉시 candidate, CEO-GATE 유지)
- **강등 경로 신설**: confidence<0.4 → stage 1단계 강등(knowhow→candidate). **삭제는 CEO 승인**(강등/아카이빙은 자동).
- `is_critical` lesson은 decay 면제(점수 하한 고정) — rare-critical 유실 방지.

### 9. 일회성 ↔ 장기기억 분리 enum (즉시반영 — 오염 방지)
lesson_candidate 승격 자격 enum: `{risk_check 실패, 체결 실패, 슬리피지 임계초과, CEO 반려, recurrence}`. 그 외(단순 조회 성공·정상 체결·헬스OK)는 `audit_logs`에만, lessons 미진입. posthook `is_promotable(event)→bool`(haiku) 게이트. reflection q3(실패)·q7(hook gap) 모두 비면 승격 차단.

### 10. Hook 선언/구현 분리 (즉시반영 — 결합도↓)
출처: Everything Claude Code(hooks.json 선언 + scripts/hooks 구현 + adapter 변환). `config/portfolio/hook_profiles/{task}.yaml`에 `pre/post/block_on` 선언, `hooks/registry.py`가 `run_pre/run_post` 제공. 루프는 registry만 호출, 구체 handler 모름. profile 부재 시 안전 no-op.
