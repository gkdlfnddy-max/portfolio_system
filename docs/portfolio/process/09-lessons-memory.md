# Lessons / Memory Agent 시스템 프로세스 정리

> 본 문서는 "메모리로 성장하는 에이전트"의 DB측 substrate(lessons / lesson_candidates) 운영 프로세스를 **실제 코드 기준**으로 정리한다.
> 근거 코드: `main_mission/portfolio_os/lessons.py`, `main_mission/portfolio_os/store/schema.sql`, `main_mission/portfolio_os/store/db.py`, 소비처 `main_mission/portfolio_os/advice.py`.
> 원칙: Anthropic API 미사용 — 분석/판단은 Claude+메모리 에이전트가 수행하고, 그 산출물을 본 영역이 DB에 누적·재사용한다. 단기 trading 메모리가 아니라 포트폴리오 비중관리·분할 리밸런싱 의사결정의 장기 기억이다.

---

## 1. 목적

- 시장/경제/섹터/종목/전제/결정/리스크 분석에서 얻은 관찰을 **일회성 로그와 장기 메모리로 분리**해 누적한다.
- 아무 로그나 `lessons` 에 쌓지 않는다. 관찰은 `lesson_candidates` 에 모으고, **승격 기준(반복성·근거/결과·확신)** 을 충족할 때만 `lessons` 로 승격한다 (`lessons.py` 모듈 docstring, `_eligible()`).
- 승격된 lesson 을 다음 decision/advice 단계에서 `scope`/`ref` 로 조회해 **참고 근거로 재인출**한다 (자동 반영 아님 — `search()` docstring: "자동 반영이 아니라 참고").
- 메모리 오염 방지: 검증되지 않은 단발 관찰이 영구 메모리에 섞이지 않도록 후보층을 분리한다.

---

## 2. 전체 흐름

```text
[Claude+메모리 에이전트 분석/회고]
        │  (관찰 1건)
        ▼
add_candidate(scope,title,body, ref?, evidence_ref?, outcome?, confidence?)
        │  같은 (scope, ref, title) → observed_count += 1 (반복성 누적)
        │  아니면 → 새 lesson_candidates 행 (status='candidate')
        ▼
[누적: lesson_candidates]
        │
        │  promote()  (CLI: python -m main_mission.portfolio_os.lessons --promote)
        │  _eligible(): observed_count>=2 AND confidence>=0.6 AND (evidence_ref OR outcome)
        ▼
[lessons] (source='promoted')  +  후보 status='promoted'
        │
        │  search(scope, ref)  ← 다음 decision/advice 가 인출
        ▼
advice.generate() 가 scope='sector'(테마별), scope='premise' 로 조회 → 조언 raw 에 "[메모리]" 항목 추가
```

- 후보 적재 → 반복성 누적 → 승격 게이트 → 장기 lessons → 재인출, 의 단방향 파이프라인이다.
- decay/archive(노후 lesson confidence 하향·보관) 단계는 **미구현**(§14). `lessons.py` docstring 에 `decay()` 가 언급되나 함수는 존재하지 않는다.

---

## 3. 입력

`add_candidate()` 인자 (`lessons.py:33-35`):

| 인자 | 의미 | 비고 |
|---|---|---|
| `scope` | 분류 축 (필수) | market\|economy\|sector\|instrument\|premise\|decision\|risk |
| `title` | 제목 (필수) | (scope, ref, title) 가 반복성 키 |
| `body` | 본문 (필수) | 갱신 시 덮어씀 |
| `ref` | scope 내 키 (종목코드/섹터명/주제) | 반복성 매칭에 `IFNULL(ref,'')` 사용 |
| `account_index` | 계좌 (NULL=전역 교훈) | |
| `evidence_ref` | 근거 참조 (예: evidence_documents.id) | 승격 OR 조건의 한 축 |
| `outcome` | 실제 결과 | 승격 OR 조건의 다른 축 |
| `confidence` | 0~1 확신도 (기본 0.0) | 누적 시 `MAX(confidence, ?)` |
| `source` | 출처 (기본 "claude_agent") | |

- 입력 주체: Claude+메모리 에이전트(회고/분석 산출), 또는 결과(outcome) 피드백. **웹 UI 입력 경로 없음**(§7).
- `promote()`, `search()`, `overview()` 는 추가 입력 인자 없음(또는 검색 필터 scope/ref/limit).

---

## 4. 출력

- `add_candidate()` → `{"ok": True, "candidate_id": <int>}` — `lesson_candidates` 1행 신규/갱신.
- `promote()` → `{"ok": True, "promoted_count": n, "promoted": [{id, scope, ref, title}, ...]}` — `lessons` 신규행 + 후보 status='promoted'.
- `search(scope?, ref?, limit=20)` → `[{id, scope, ref, title, body, confidence, created_at}, ...]` (confidence DESC, id DESC).
- `overview()` → `{"ok": True, "candidates": {status: count}, "lessons": <count>, "criteria": {min_observed:2, min_confidence:0.6, needs_evidence_or_outcome: True}}`.
- CLI `main()`: `--promote` / `--list` / (무인자=overview) → JSON 을 stdout 으로 출력.
- 재인출 출력: `advice.generate()` 가 `search()` 결과를 `source="lesson:<id>"`, `severity="info"`, title `[메모리] ...` 로 advice_items 에 반영(`advice.py:79-91`).

---

## 5. DB 테이블

`store/schema.sql` 기준 — 본 영역의 1차 truth는 RDB(SQLite, `data/portfolio.sqlite3`).

### `lesson_candidates` (schema.sql:264-280) — 승격 전 관찰층(일회성/검증중)
- `id` PK AUTOINCREMENT
- `account_index` (NULL 가능)
- `scope` NOT NULL — 주석상 `market|economy|sector|instrument|premise|decision|risk`
- `ref`, `title` NOT NULL, `body` NOT NULL
- `evidence_ref` — evidence_documents.id 등
- `observed_count` NOT NULL DEFAULT 1 (반복성 카운터)
- `outcome` — 실제 결과
- `confidence` REAL DEFAULT 0.0
- `status` NOT NULL DEFAULT 'candidate' — candidate | promoted | rejected
- `source`, `created_at`, `updated_at`
- 인덱스 `idx_lesscand (scope, ref, status)`

### `lessons` (schema.sql:195-206) — 승격 후 장기 메모리층
- `id` PK AUTOINCREMENT
- `account_index` (NULL = 전역 교훈)
- `scope` NOT NULL — 주석상 `market|economy|sector|instrument|premise|decision` (스키마 주석은 `risk` 미포함; 후보 주석엔 risk 포함 — §9 불일치)
- `ref`, `title` NOT NULL, `body` NOT NULL
- `confidence` REAL (0~1, 반복 검증되며 성장)
- `source` — claude_agent | user | outcome (승격 경로는 코드상 'promoted' 로 기록 — §9 불일치)
- `created_at`
- 인덱스 `idx_lessons_scope (scope, ref, id DESC)`

연관(참조만, 직접 미사용): `evidence_documents`(근거 메타), `decision_evidence_links`(decision↔evidence). `lesson_candidates.evidence_ref` 는 텍스트 참조일 뿐 FK 제약 없음.

---

## 6. API / 함수

모듈: `main_mission/portfolio_os/lessons.py`. (HTTP API 아님 — 파이썬 함수 + CLI)

| 함수 | 시그니처 | 동작 |
|---|---|---|
| `add_candidate` | `(scope, title, body, *, ref=None, account_index=None, evidence_ref=None, outcome=None, confidence=0.0, source="claude_agent")` | 같은 (scope,ref,title,status='candidate') 존재 시 `observed_count+1`·body 갱신·`confidence=MAX(...)`·evidence_ref/outcome=`COALESCE`; 아니면 신규 INSERT |
| `_eligible` | `(row) -> bool` | `observed_count>=MIN_OBSERVED(2)` AND `confidence>=MIN_CONFIDENCE(0.6)` AND `(evidence_ref OR outcome)` |
| `promote` | `() -> dict` | status='candidate' 전수 스캔, `_eligible` 통과분만 `lessons` 로 INSERT + 후보 status='promoted' |
| `search` | `(scope=None, ref=None, limit=20) -> list` | lessons 조회 (ORDER BY confidence DESC, id DESC). 참고용 — 자동 반영 아님 |
| `overview` | `() -> dict` | 후보 status별 카운트 + lessons 총수 + criteria |
| `main` | argparse `--promote`/`--list` | CLI 진입점 |
| `_now` | `() -> str` | UTC ISO8601 |

- 상수: `MIN_OBSERVED = 2`, `MIN_CONFIDENCE = 0.6` (`lessons.py:25-26`).
- DB 접근: `store_db.connect()` (`store/db.py`) — 최초 연결 시 schema.sql 멱등 적용.
- 외부 소비: `advice.py` 가 `from . import lessons as lessons_mod` 후 `lessons_mod.search(...)` 호출.

---

## 7. UI 화면

**해당 없음 (전용 운영화면 미구현).**
- `web/app/api/**/route.ts` 에 lessons/lesson_candidates 를 읽거나 쓰는 라우트 없음(존재 라우트: accounts/sync/universe/decision/profile/allocation).
- `web/lib/portfolio/types.ts` 의 `LessonCandidate { stage: "reflection"; title; ... }` 는 allocation 결과 객체(`VariantResult`/`...lessons`)의 in-memory 타입일 뿐, DB `lessons` 테이블을 표시하는 화면이 아니다.
- 원칙상 웹은 DB truth 조회 전용이어야 하나, 본 영역은 아직 조회 UI 자체가 없다. lesson 의 사용자 가시화는 현재 `advice` 화면을 통해 간접적으로만(`source="lesson:<id>"`) 노출된다.

---

## 8. 상태 전이

### lesson_candidates.status
```text
candidate ──(_eligible 통과 + promote())──▶ promoted
candidate ──(거부; 현재 자동 경로 없음, 수동/미구현)──▶ rejected
```
- `candidate` → `promoted`: `promote()` 가 수행. 한 번 promoted 된 행은 `status='candidate'` 필터에서 빠져 재승격되지 않음.
- `candidate` → `rejected`: 스키마에 값은 정의되나 **코드상 전이 함수 없음**(§14).

### 반복성(observed_count)
- 동일 (scope, ref, title) 재관찰 시 `add_candidate` 가 `observed_count+1`. 2회 이상이어야 승격 1차 게이트 통과.

### confidence
- 누적 시 `MAX(confidence, new)` 로 단조 증가만 가능. **하향(decay) 전이 없음** → 한번 0.6 이상이면 떨어지지 않음(§9 위험).

### lessons
- INSERT only. 상태 컬럼 없음 → archive/강등 상태 전이 미구현.

---

## 9. 예외 / 실패 케이스

- **scope 검증 부재**: `add_candidate`/`promote` 어디에서도 scope 값을 7종(market/economy/sector/instrument/premise/decision/risk)으로 검증하지 않음. 오타 scope 도 그대로 저장 → 재인출 시 매칭 누락 위험.
- **스키마 주석 불일치**: `lessons.scope` 주석은 `risk` 미포함, `lesson_candidates.scope` 주석은 `risk` 포함. risk scope 후보가 승격되면 lessons 의 scope 합의와 어긋남.
- **source 불일치**: `lessons.source` 주석은 `claude_agent|user|outcome` 이지만, `promote()` 는 `'promoted'` 로 INSERT → 주석 enum과 실제 값 불일치.
- **confidence 단조 증가**: `MAX(confidence,?)` 로만 갱신 → 잘못된 lesson 강등/노후화 반영 불가(decay 미구현).
- **반복성 키 약점**: 매칭 키가 (scope, ref, title) 문자열 동일성 → title 미세 변형 시 동일 관찰이 별도 후보로 분리, observed_count 분산 → 영영 승격 못 할 수 있음.
- **트랜잭션 일관성**: `promote()` 는 INSERT lessons + UPDATE 후보를 단일 commit 으로 묶음(정상). 단 중복 INSERT 가드(idempotency) 없음 — 같은 후보 두 번 promote 호출 가능성은 status 필터로 방지됨.
- **빈 결과**: search 결과 없으면 빈 리스트 — advice 가 메모리 항목 없이 진행(정상 fallback).

---

## 10. Hard-block 조건

**본 영역 자체의 hard-block 은 해당 없음** — lessons/memory 는 주문 경로가 아니라 자문/기억 substrate 이므로 주문 차단 책임이 없다.

- 다만 운영 전체의 hard-block(목표비중 없는 주문후보 금지, 사람 승인 없는 주문 금지, `KIS_LIVE_CONFIRM` 없는 live 차단, 리스크 게이트)은 `risk/gate.py`·주문 경로 책임이며 본 영역은 그 결정의 근거를 **참고로만** 제공한다.
- 승격 게이트(`_eligible`)는 hard-block 이라기보다 **품질 게이트**다: 미충족 후보는 메모리 오염을 막기 위해 lessons 진입이 차단된다.

---

## 11. 로그 / 감사 기록

- **전용 audit_logs 연동 없음**: `lessons.py` 는 `audit_logs` 테이블에 기록하지 않는다. 후보 적재/승격이 감사로그로 남지 않음(§14 개선 후보).
- 자체 추적은 행 단위 메타로 대체: `lesson_candidates.created_at/updated_at`, `observed_count`, `status`, `source`; `lessons.created_at`, `source='promoted'`.
- 비밀값 미저장 원칙 준수: 본 영역은 키/토큰/평문 계좌번호를 다루지 않으며 텍스트 관찰만 저장.

---

## 12. 테스트 기준

- **현재 lessons 전용 테스트 없음**: `main_mission/portfolio_os/tests/` 에는 `test_risk_gate.py`, `test_order_safety.py` 만 존재. lessons/memory 테스트 미작성(§14).
- 권장 테스트 기준(미작성, 다음 항목):
  1. `add_candidate` 반복 호출 → 동일 (scope,ref,title) 의 `observed_count` 증가, confidence `MAX` 적용.
  2. `_eligible`: observed<2 / confidence<0.6 / evidence·outcome 둘다 없음 → 각각 False; 모두 충족 → True.
  3. `promote`: 경계값(observed=2, confidence=0.6, evidence 또는 outcome 한쪽) 승격, 미달 비승격, promoted 후 재호출 시 중복 승격 없음.
  4. `search`: scope/ref 필터·정렬(confidence DESC, id DESC)·limit.
  5. `advice.generate` 가 promoted lesson 을 `source="lesson:<id>"` 로 재인출.

---

## 13. 현재 구현 상태

**구현 완료 (코드 검증됨):**
- `lesson_candidates` / `lessons` 테이블 (schema.sql) — `store/db.py` 가 최초 연결 시 멱등 생성.
- `add_candidate()` — 반복성 누적(observed_count++) + COALESCE/MAX 갱신 + 신규 INSERT.
- 승격 기준 `_eligible()` 3조건(반복성≥2 AND confidence≥0.6 AND (evidence OR outcome)).
- `promote()` — 후보→lessons 승격 + status 전이.
- `search()` — scope/ref/limit 조회, confidence·id 정렬.
- `overview()` — 후보 status별/lessons 카운트 + criteria 노출.
- CLI `--promote` / `--list`.
- 재인출 소비처 `advice.py` 1곳(`search(scope='sector'/'premise')`).
- scope 7종 정의: market/economy/sector/instrument/premise/decision/risk (스키마 주석 + 본문 흐름).

---

## 14. 미구현 / placeholder

- **decay() / archive 미구현**: `lessons.py` docstring·CLAUDE 메모리에 언급되나 함수·로직 없음. 노후 lesson confidence 하향·보관 경로 부재.
- **rejected 전이 경로 없음**: `lesson_candidates.status='rejected'` 값만 정의, 후보를 reject 하는 함수 없음.
- **lessons archive 상태 없음**: lessons 테이블에 status/archived_at 컬럼 없음(INSERT only).
- **scope 검증/정규화 없음**: 7종 enum 강제 안 함(자유 텍스트).
- **감사로그 연동 없음**: 후보/승격이 audit_logs 미기록.
- **전용 UI/웹 API 없음**: lessons 조회·후보 검토 화면 부재(§7).
- **전용 테스트 없음**(§12).
- **Vector(근거검색)/Graph(관계설명) 미연결**: 현재 RDB 텍스트 매칭만. `evidence_documents.body` 임베딩·scope/ref 그래프 이식은 v2 승격 전제(schema.sql 주석)로 placeholder.
- **고급 승격 파이프라인 미구현**: agent 정의(`memory-lesson-chief.md`)의 raw→reflection→candidate→validated→knowhow→SOP→risk_limit 다단계, 2-stage prehook 인출(과인출→rerank→top-k 압축), support/refute 양방향 신뢰도는 설계만 존재. 코드는 candidate→promoted 단일 게이트.

---

## 15. 다음 개선 항목

1. `decay()` 구현: 마지막 검증/created 이후 경과·반증(outcome 부정)에 따라 confidence 하향, 임계 이하 lessons archive(status/archived_at 추가).
2. `reject()` 함수 + 사유 기록: 잘못된 후보를 명시적으로 status='rejected' 전이.
3. scope 화이트리스트 검증(7종) + 스키마 주석/실제 source 값 정합화('promoted' vs enum).
4. 후보 적재·승격을 `audit_logs` 에 INFO 레벨로 기록(추적성).
5. lessons 전용 조회 웹 API/화면(DB truth 조회 전용 원칙) — scope/ref/confidence 필터.
6. 반복성 키 강화: title 외 정규화 키(해시/임베딩) 도입으로 동일 관찰 분산 방지.
7. lessons 전용 pytest 추가(§12 기준).
8. Vector(evidence 임베딩 근거검색)·Graph(decision↔evidence↔lesson 관계) 승격 단계.

---

## 16. 다른 Agent와의 의존성

| 의존 대상 | 방향 | 내용 |
|---|---|---|
| Advice / Strategy-Profile Agent (`advice.py`) | lessons → advice | `advice.generate()` 가 `lessons.search(scope='sector', ref=theme)`·`scope='premise'` 로 promoted lesson 을 인출해 조언(`source="lesson:<id>"`)으로 노출. 본 영역의 유일한 현 소비처 |
| Store / DB (`store/db.py`) | 양방향 | `connect()` 로 SQLite 접근, 최초 연결 시 schema.sql 멱등 생성. lessons/lesson_candidates 테이블 소유 |
| Decision Agent (`decisions` 테이블) | 향후 | scope='decision' lesson 의 본래 인출 대상. **현재 decision 경로는 lessons.search 미호출**(연결 미구현) |
| Evidence (`evidence_documents`, `decision_evidence_links`) | 향후 | `lesson_candidates.evidence_ref` 가 가리킬 근거 메타. 현재 텍스트 참조만, FK·자동링크 없음 |
| Risk Gate (`risk/gate.py`) | 무직접 | 주문 hard-block 은 risk 책임. lessons 는 scope='risk' 기억을 제공할 수 있으나 게이트가 lessons 를 조회하지는 않음(미연결) |
| 입력 주체 = Claude+메모리 에이전트 | 외부 → lessons | 회고/분석 산출을 `add_candidate` 로 적재. Anthropic API 미사용 원칙 하에 본 영역이 DB substrate 역할 |
