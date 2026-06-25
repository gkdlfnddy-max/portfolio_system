# 성장 아키텍처 (Growth Architecture) — Portfolio OS

> 목표: "다음 실행 때 이전보다 더 나은 판단을 하는" 성장형 Portfolio OS.
> 본 문서는 모든 Agent가 공유하는 **성장 스캐폴딩**(메모리·prehook·posthook·workflow/task)과
> Agent별 개선 감사(audit)를 정의한다. 지능은 **Claude+메모리**(Anthropic API 미사용, §17).

관련 코드: [main_mission/portfolio_os/growth/](../../main_mission/portfolio_os/growth/) ·
[lessons.py](../../main_mission/portfolio_os/lessons.py) ·
스키마: [store/schema.sql](../../main_mission/portfolio_os/store/schema.sql)

---

## 1. 4-관점 성장 모델

모든 Agent는 자기 task에서 4관점을 만족해야 한다.

| 관점 | 질문 | 스캐폴딩 |
|---|---|---|
| **Memory** | 내 task에 맞는 메모리를 정확히 불러오는가? outdated를 계속 참조 안 하는가? | `agent_memory_scope`, `lessons.decay/touch`, `feedback_memory` |
| **Prehook** | 작업 전 정책·선택안·스냅샷·위험을 *안전 점검*하는가? | `growth/prehooks.prepare()` (게이트 + memory 로드 + task 개시) |
| **Posthook** | 작업 후 배운 점을 *후보*로 정리하고, 거절도 학습하는가? | `growth/posthooks.finalize()` (candidate·feedback·next_action) |
| **Workflow/Task** | 중단돼도 재개 가능하고, 중복 없이, DONE 기준이 명확한가? | `tasks` 상태머신 + `task_memory_links` provenance |

---

## 2. 구축된 스캐폴딩 (이번 작업 — 즉시 구현)

### 2.1 DB (append-only 지향, 정수 PK + scope/ref → PostgreSQL/Vector/Graph 승격 용이)

| 테이블 | 역할 | 신규/변경 |
|---|---|---|
| `tasks` | 표준 task 상태머신 + prehook/posthook provenance | **신규** |
| `agent_memory_scope` | Agent별 memory scope 선언(검색 대상·우선순위) | **신규** |
| `task_memory_links` | task ↔ memory(어떤 메모리를 참조했나) provenance | **신규** |
| `feedback_memory` | 사용자 거절/수정 = negative memory | **신규** |
| `lessons` | `last_seen_at`·`status(active\|archived)`·`agent` 추가 | 컬럼 추가 |
| `lesson_candidates` | `last_seen_at`·`agent` 추가 | 컬럼 추가 |

마이그레이션: `store/db.py:_ADD_COLUMNS` (멱등 ALTER) + 신규 테이블은 `schema.sql` CREATE IF NOT EXISTS.
`connect()`/`init()` 양 경로 모두 `_migrate()` 실행 → 프로덕션·테스트 DB 동일 보장.

### 2.2 Python 모듈 (`growth/`)

- `registry.py` — `seed()`(멱등), `scopes_for(agent)`. 7개 agent × scope를 **데이터로** 선언(하드코딩 아님).
- `memory.py` — `recall(agent, refs)`(scope·decay 가중, freshness touch), `record_feedback()`, `recall_feedback()`.
- `tasks.py` — `open_task / link_memory / update_task / get_task`. 상태: open→running→(done\|blocked\|failed\|cancelled).
- `prehooks.py` — `prepare(agent, task_type, account_index, refs)`: **게이트** + memory 로드 + task 개시 + provenance 링크.
- `posthooks.py` — `finalize(task_id, ...)`: lesson_candidate(즉시 승격 금지)·feedback·next_action·unresolved_risk.
- `lessons.py`(확장) — `decay()`(archive), `touch()`(freshness), `search()` archived 제외 + `eff_confidence`(decay 가중).

### 2.3 Prehook 안전 게이트 (task_type별, `prehooks.REQUIRE`)

| task_type | 정책 필요 | selected allocation 필요 | fresh snapshot 필요 |
|---|---|---|---|
| `decision` | ✓ | ✓ (없으면 **hard-block**) | ✓ (stale면 **hard-block**) |
| `risk_check` / `selection` / `allocation_generate` | ✓ | — | — |
| `theme_advice` / `view_coach` / `consult` / `profile_save` | — | — | — (조언은 block 없음) |

> 실데이터 검증(account 1): decision prehook → selected_alloc #5 + fresh snapshot #7 보유 → gate=pass.
> 미설정 계좌(99) → "selected allocation 없음" hard-block, 그래도 task는 `blocked`로 provenance 보존.

### 2.4 메모리 승격/감쇠 규칙

- **candidate → promoted**(`lessons.promote`): observed_count≥2 AND confidence≥0.6 AND (evidence 또는 outcome). candidate/promoted/rejected 분리(`lesson_candidates.status`).
- **decay/archive**(`lessons.decay`): 유효 confidence = base × 0.5^(age/90d). archive_below 0.15 & 30d 초과 시 `status='archived'`(삭제 아님). 검색에서 제외.
- **freshness**: prehook recall 시 `touch()`로 `last_seen_at` 갱신 → 자주 쓰는 메모리는 감쇠 시계 리셋.

---

## 3. 표준 Workflow (재현 가능 + DONE 기준)

### W1. 중전제 상담 → 정책 반영 (불변 흐름, [[consult-to-policy-flow]])
```
상담조언(consult/theme/view) → 폼필드 임시반영 → 사람 저장(profile.save + history)
→ policy version(portfolio_policies) → allocation 3안 재계산 → 사람이 selected 확정
→ decision(prehook 게이트) → 리스크 게이트 → 승인 대기
```
- **DONE**: 각 단계가 별도 행으로 남고(append-only), "그대로 적용"이 저장 전엔 DB policy 불변.
- 안전: 상담은 보조. 저장·선택 없이 policy/주문 반영 금지.

### W2. 테마 조언 → allocation tilt → risk
```
theme-sector-advisor(prehook: sector lessons) → 테마 long/hedge/watch 분류 + tilt cap
→ allocation tilt 후보 → selection precheck(sector_max) → posthook(candidate/feedback)
```
- **DONE**: 테마가 tilt cap 내 bucket으로 연결 + 과열/변동성/분산 설명 + candidate 적재.

### W3. selected allocation 없는 decision 차단
```
decision 요청 → prehooks.prepare("...","decision") → 게이트(selected_allocation, fresh_snapshot)
→ block이면 task=blocked + 사유 기록, 진행 중단
```
- **DONE**: selected allocation/fresh snapshot 없으면 decision 미생성(hard-block) + provenance 보존.

### W4. 메모리 정비 루프 (배치)
```
posthook 적재(candidate) → (반복 관찰) → lessons.promote → 주기적 lessons.decay(archive)
```
- **DONE**: candidate/promoted/archived가 분리 집계되고, outdated가 검색에서 빠짐.

세부 단계 문서: [process/](process/) (01~12). 본 문서는 그 위의 성장/메모리 계층.

---

## 4. Agent별 개선 감사 (관점별 2건 = 8건/agent · ✅즉시구현 / ▷후속)

> 즉시구현(✅)은 §2 공통 스캐폴딩으로 해당 Agent에 *지금* 적용 가능. 후속(▷)은 도메인별 추가 작업.

### 4.1 Strategy / Investor Profile (view-coach)
- M1 ✅ premise scope 분리 recall(`scopes_for("view-coach")=premise→decision→risk`).
- M2 ✅ 거절된 보완을 `feedback_memory`로 — 같은 보완 강요 회피.
- P1 ✅ prehook이 `investor_profile_history`+premise lessons 로드(과거 견해 변천 참조).
- P2 ▷ 저장 직전 diff(변경 전/후)를 prehook 점검 항목으로 추가.
- O1 ✅ posthook이 빠진 변수→`unresolved_risk`, 보완유도→`next_action`.
- O2 ▷ 반복 확인된 코칭 포인트만 premise lesson 승격(observed≥2 게이트 적용).
- W1 ✅ W1 흐름 문서화(저장 전 policy 불변 보장).
- W2 ▷ profile_save task에 변경 필드 목록을 outcome으로 표준 적재.

### 4.2 Theme / Sector Advice (theme-sector-advisor)
- M1 ✅ sector→instrument→market→economy scope 우선 검색.
- M2 ✅ 축소/거절 테마 feedback_memory 저장.
- P1 ✅ prehook refs=[테마]로 테마 lessons 정밀 로드.
- P2 ▷ 테마별 과열/변동성 evidence(`evidence_documents`) freshness 점검을 prehook에 추가.
- O1 ✅ 테마 해석을 sector lesson_candidate로(즉시 승격 금지).
- O2 ▷ tilt cap 초과 제안 시 자동 경고를 posthook candidate로.
- W1 ✅ W2(테마→tilt→risk) 문서화.
- W2 ▷ theme_advice → allocation tilt 자동 연결 API(현재 수동).

### 4.3 Allocation
- M1 ✅ 과거 selection 이력(`allocation_selections`)을 prehook provenance로 링크.
- M2 ▷ variant별 사후 결과(outcome)를 lesson_candidate(scope=decision)로.
- P1 ✅ prehook이 policy_version 고정 → 생성-선택 정합성.
- P2 ▷ 직전 selected와 diff를 prehook이 미리 계산.
- O1 ▷ region_targets/bond 미반영 경고를 posthook candidate로(다음 Phase 본구현).
- O2 ▷ anchor/tilt 분리 위반 점검.
- W1 ✅ selected allocation 확정→decision 연결(W3).
- W2 ▷ allocation_generate DONE 기준(3 variant + precheck) task outcome 표준화.

### 4.4 Decision / Rebalance
- M1 ✅ decision scope lessons + 과거 보류/차단(feedback) prehook 로드.
- M2 ▷ 분할 조정 결과를 outcome→lesson_candidate.
- P1 ✅ **selected allocation 없으면 hard-block** + **stale snapshot hard-block**(W3, 실데이터 검증).
- P2 ✅ policy/selected/snapshot id를 task provenance로 고정.
- O1 ▷ drift/분할 계획 요약을 task outcome으로 표준 적재.
- O2 ▷ 반복 차단 사유를 risk lesson_candidate로.
- W1 ✅ W3 차단 워크플로 + 재개 가능(task status).
- W2 ▷ portfolio balance 관점 지표(편중도) 추가.

### 4.5 Risk Gate
- M1 ✅ risk scope 전용 recall(`scopes_for("risk-chief")=risk→decision`).
- M2 ✅ 반복 차단 사유를 lesson_candidate(scope=risk)로(역할문서 명시).
- P1 ▷ risk memory(과거 위반 패턴)를 prehook에서 선로딩 후 게이트.
- P2 ▷ hard-block vs warning 구분을 prehook 점검 결과에 명시.
- O1 ✅ posthook이 위반을 candidate로(승격은 반복 후).
- O2 ▷ 섹터/국가/통화/채권/인버스/레버리지 한도 연결(다음 Phase: regionbond.validate 배선).
- W1 ▷ risk_check task DONE 기준(violations=0 또는 사람승인).
- W2 ▷ daily_loss/drawdown 한도 활성화(현재 YAML 정의만).

### 4.6 Evidence / Research
- M1 ✅ `evidence_documents`에 freshness·confidence 이미 존재 → recall 시 활용.
- M2 ▷ evidence ↔ lesson_candidate 링크(evidence_ref) 표준화.
- P1 ▷ research-chief prehook이 stale evidence(freshness 초과) 경고.
- P2 ▷ source confidence 임계 미만 evidence 차단.
- O1 ▷ decision_evidence_links로 근거→비중변경 연결(테이블 존재).
- O2 ▷ 외부조사 결과를 evidence_documents로 표준 적재.
- W1 ▷ Vector DB 승격 계획(본문 임베딩) 문서화.
- W2 ▷ research task DONE 기준(출처·freshness 필수).

### 4.7 Graph / Exposure
- M1 ▷ 계좌→종목→섹터→국가→통화→테마 관계 그래프 인덱스(정수 PK+scope/ref로 이식 용이).
- M2 ▷ ETF 중복 노출 추적 메모리.
- P1 ▷ exposure 그래프를 risk 설명 prehook 입력으로.
- P2 ▷ 관계 변경 이력 provenance.
- O1 ▷ 노출 변화 lesson_candidate.
- O2 ▷ 중복 노출 경고 feedback.
- W1 ▷ graph 적재 workflow 문서.
- W2 ▷ Graph Index 역할 분리(DB/Architecture와 연계).

### 4.8 Lessons / Memory (memory-lesson-chief)
- M1 ✅ candidate/promoted/archived **분리**(테이블 status + decay).
- M2 ✅ confidence decay + freshness(touch) + archive 구현.
- P1 ✅ agent별/task별 scope(agent_memory_scope + task_memory_links).
- P2 ▷ prehook 검색 품질 측정(hit rate) 지표 적재.
- O1 ✅ posthook candidate 표준 경로.
- O2 ▷ 중복 lesson 병합 배치.
- W1 ✅ 메모리 정비 루프(W4) 문서 + CLI(`--promote/--decay`).
- W2 ▷ scope별 보존정책(TTL) 차등.

### 4.9 UI / UX
- M1 ▷ 상담 결과를 policy draft 카드로(현재 폼필드 반영만).
- M2 ▷ 저장 전/후 상태 배지(임시반영/저장필요/재계산필요).
- P1 ▷ strategy viewer가 task provenance(이 값이 쓰인 decision) 표시.
- P2 ▷ prehook 게이트 결과를 UI 경고로.
- O1 ▷ "그대로 적용" 변경 전/후 프리뷰(다음 Phase 지역/채권).
- O2 ▷ 거절 버튼 → feedback_memory 기록.
- W1 ▷ dashboard에 history+decision 결과.
- W2 ▷ viewer = 투자 정책서 역할 강화.

### 4.10 DB / Architecture
- M1 ✅ RDB(운영 truth) / JSON(policy·doc) 역할 분리 유지; 성장 메모리 신규 4테이블.
- M2 ✅ append-only 테이블(history·selections·consultations·tasks·feedback) 삭제 금지 원칙 명문화.
- P1 ✅ memory·workflow provenance 저장(task_memory_links).
- P2 ▷ Vector DB(evidence 임베딩)/Graph Index 역할 분리 설계.
- O1 ✅ 멱등 마이그레이션(connect/init 동일 경로).
- O2 ▷ PostgreSQL 승격 매핑표(정수 PK/scope·ref 보존).
- W1 ✅ 스키마 = 단일 SSOT(schema.sql) + _ADD_COLUMNS.
- W2 ▷ provenance 보존 마이그레이션 테스트.

---

## 5. 후속 작업 (다음 Phase)
1. ✅ **완료** — 지역/채권 비중을 allocation 엔진·risk gate에 반영(CEO §9 ①②):
   - allocation `_variant`: 채권 bucket(현금과 분리) + 지역별 anchor 분해(`_split_region`).
   - `regionbond.validate` 배선(이전 죽은 코드) → `decision.compute` risk gate + `selection.precheck`.
   - 검증: `test_regionbond_engine.py` 9건. (UI 입력 필드/프리뷰·history 적재는 다음 Phase.)
2. ✅ **완료** — UI(CEO §7): 지역 비중 + 채권 목표(%)·듀레이션 입력 필드(`strategy/page.tsx`),
   상담 "그대로 적용" **변경전/후·임시반영·저장필요·재계산필요 프리뷰**, 저장 경고(지역 합계),
   strategy viewer **"지역·채권 구조" 카드** + 정책 카드 지역/채권/국가한도 표시(`view/page.tsx`). tsc 통과.
3. ✅ **완료** — 확정 흐름 연결(CEO §9 ③): allocation 확정 화면이 현금/채권/지역anchor/테마/헤지를
   모두 표시(이전 버그: 첫 anchor만·채권 누락) + `selection.select`가 region/bond 구성을 `allocation_selections`에 보존(test).
4. ✅ **완료** — dashboard/history(CEO §9 ④): `/accounts/[id]/history` — 정책 지역·채권 변경 타임라인 +
   확정 목표비중 구성(방어 현금·채권·헤지 / 지역 / 테마) 이력. append-only(superseded/cancelled 보존) 표시.
5. theme_advice → allocation tilt 자동 연결, view_coach → profile diff API.
6. Evidence Vector DB / Exposure Graph Index 승격. 채권 duration 금리리스크 신호 배선.

## 6. 검증 (초기 스캐폴딩)
- 테스트: `test_growth.py` 9건 + 전체 28건 통과.
- 실데이터: account 1 prehook/posthook provenance 생성(§2.3) — task_memory_links에 lesson·policy·selection·snapshot·candidate 링크.
- mock/hardcoding/Anthropic 의존: 0 (스캔 통과).

---

# CEO 명세 (2026-06-21) — Agent + Task 성장 (정식 SSOT)

> CLAUDE.md §11 의 상세. **작업 전엔 과거를 읽고, 작업 중엔 현재를 판단하고, 작업 후엔 배운 것을 저장하고, 다음 작업은 더 나아진 상태로 시작.**
> Agent는 *전문성*, Task는 *절차·검증·실패방지*를 동시에 누적. 위 §1–6 스캐폴딩이 토대, 아래가 확장 명세.

## A. 성장 루프
```
prehook(과거 조회) → Agent 판단 → 결과 → posthook(경험 정리·candidate) → (검증) promoted → 다음 작업 재활용
```

## B. Agent 전문 분야
Strategy/Investor Profile · Theme/Sector · Defensive Asset · Allocation · Rebalance · Risk Gate · Broker/Sync · Memory/Evidence · Dashboard/History. (각 영역 책임은 CLAUDE.md §11·roles.md 참조)

## C. Memory Scope (4종, 격리 필수)
- **account**: 계좌 전용(예: 방어 40% 선호 / 실전→PIN 재인증 / 반도체 hedge 관리).
- **user**: 사용자 성향(예: 비중관리>단기매매 / 관심 무조건 롱 금지 / 숫자형 결론 선호 / 검증없는 DONE 싫어함).
- **agent**: 전문 공통 노하우(예: 바이오=ETF 코어 / 양자=소규모·watch / 인버스=hedge bucket만 / mixed_swing=net·gross 관리).
- **task**: 현재 작업 임시(바로 승격 금지 → posthook 평가). 충돌 시 **계좌 policy 우선**, account↔타계좌 적용 금지, agent lesson 이 policy override 금지.

## D. Prehook (작업 전 필수)
조회: account_id·snapshot·policy version·selected allocation·user/agent memory·promoted lesson·최근 실패/차단·evidence·적용/무시 이력·stale/충돌.
**Hard-block/warning**: account_id 없음 · snapshot 없음 · selected allocation 부재 · stale snapshot/policy · 타 계좌 memory 혼입 · user↔policy 충돌 · hard rule override · 강한 조언인데 evidence 없음 · live 주문인데 PIN/승인/lock 미충족.

## E. Posthook (작업 후 필수)
저장: 판단·참조 memory·참조 evidence·결과·사용자 적용/무시/수정·실패/차단·lesson candidate·갱신/폐기 memory·다음 unresolved issue. **posthook 없이 완료 금지.**

## F. Lesson 승격
`raw → candidate → (검증) → promoted → stale → archived`. 승격: 같은 유형 2회+ 유효 · 사용자 적용/승인 · 테스트 통과 · evidence 연결 · 타 계좌 재사용 · hard rule 무충돌. **검증 없는 승격 금지.**

## G. Memory 품질 필드
`memory_id·scope_type·scope_id·agent_name·domain·topic·summary·lesson_text·source_type·source_ref·evidence_ids·confidence·freshness_at·created_at·updated_at·last_used_at·use_count·success_count·rejection_count·promoted·archived·stale_reason`. 거절 조언 rejection_count↑·반복 금지 · stale warning · 근거 없는 memory 는 강한 조언 사용 금지.

## H. Task별 성장
모든 작업 `task_type` 보유: strategy_input_refinement · theme_direction_classification · defensive_asset_advice · allocation_generation · selected_allocation_validation · rebalance_plan_generation · daily_portfolio_review · broker_sync · risk_gate_check · evidence_research · memory_promotion · dashboard_history_update.
**Task 필드**: task_id·task_type·task_domain·task_owner_agent·account_id·policy_version_id·selected_allocation_id·input_summary·output_summary·status·success·failure_reason·validation_results_json·memory_used_ids·evidence_used_ids·lesson_candidate_ids·promoted_lesson_ids·user_action·created_at·completed_at.
**Task 저장소**: task_memories·task_lesson_candidates·task_promoted_lessons·task_failure_patterns·task_validation_checklists·task_regression_tests.

## I. Task Checklist / Regression (예)
- allocation_generation: selected 존재·합계100·순현금+채권+위험=100·long만 tilt·hedge 후보는 bucket·unknown/watch 미반영·차트=숫자 일치·risk gate 통과.
- daily_portfolio_review: 최신 snapshot·stale price·drift·no-trade reason·주문은 필요시만·승인 전 주문 금지·미체결 반영.
- risk_gate_check: hard rule override 없음·live lock·PIN/승인·인버스 한도·단일/섹터/테마/통화/국가 한도.
- defensive_asset_advice: 방어=순현금+채권·채권 무조건 추가 금지·숫자형 결론·합계100·채권>방어 hard error.
- **Regression 승격(반복 실패)**: "반도체 고점→short_or_hedge" / "방어40·채권10→순현금30·위험60·합계100" / "숏을 롱 tilt에 섞지 않음(hedge 분리)" / "DATABASE_URL env location checklist".

## J. Agent × Task Matrix
누가 어떤 task 에서 성장하는지 추적(Theme=direction/evidence/swing · Allocation=generation/validation/chart · Risk=gate/hard_rule/exposure · Broker=sync/token/import · Dashboard=history/chart/review_display).

## K. 출처 표시 / 성장 리포트
조언 UI에 출처(계좌 policy·사용자 선호·agent promoted lesson·evidence·지난 무시 조언). Agent 리포트(신규/promoted/archived·다음 prehook) + Task 리포트(task별 수행/성공/실패·신규 lesson·validation·regression·다음 prehook).

## L. 금지
매번 처음부터 판단·posthook 없는 완료·거절 조언 반복·stale을 최신처럼·계좌 memory 교차적용·agent lesson policy override·evidence 없는 단정·memory만 믿고 시장 무시·검증 없는 승격·**mock memory 완료 보고**.

## M. SSOT
운영 truth = **원격 PG `portfolio_os`(192.168.0.107)** — CEO single-SSOT. 자격증명 `.env`(gitignore, REMOTE_*). 원격은 정규 스키마(evidence_documents·research_runs·account_daily_snapshots·portfolio_drift_history·dashboard_metrics·asset_*_edges 등)이고 앱은 **원격에 단계적 정렬**(CEO 결정, 테이블명 매핑 포함). 스키마 변경은 중앙(DB/Architecture)만.

## N. 구현 Track (병렬)
A Memory Schema · B Prehook Engine · C Posthook Engine · D Agent별 전문 memory · E Memory UI/Audit · F Tests/Governance. (스윙/헤지·evidence·exposure 는 [swing_hedge.md] 별도 Track와 연동 — 미작성 시 본 문서 J·M 기준)

## O. 완료 보고 기준
Memory 구조(scope별 필드+예시) · Prehook(조회·stale·충돌·hard-block 증거) · Posthook(candidate·user_action·evidence link) · Agent 성장 · Task 성장 · 안전성(isolation·충돌·stale·거절반복방지·hard rule·live lock·PIN) · 검증(pytest·tsc·migration·mock/secret).
