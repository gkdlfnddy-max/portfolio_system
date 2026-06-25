# Research / Evidence Agent 시스템 프로세스 정리

> 이 문서는 **실제 코드 기반**으로 작성되었다. 추측 금지 — 함수명·테이블명·필드명·경로는 코드에 있는 그대로다.
> 미구현 항목은 정직하게 "미구현/계획"으로 표기한다.
> 공통 원칙: 단기 trading 아님(포트폴리오 비중관리 + 분할 리밸런싱) · 웹은 DB truth 조회만 · KIS 호출은 백엔드 sync/job만 · 운영화면 mock/하드코딩 금지 · 목표비중 없이 주문후보 금지 · 사람 승인 없이 주문 금지 · live 주문은 `KIS_LIVE_CONFIRM` 없이 하드차단 · 모든 decision 은 snapshot/version/provenance 기록 · RDB=금액/잔고/주문 truth, Vector=근거검색, Graph=관계설명.

근거 코드/문서:
- `main_mission/portfolio_os/store/schema.sql` — `evidence_documents` / `decision_evidence_links` 테이블 정의(**스키마만**, 라인 282~306)
- `agents/portfolio/research-chief.md` — 근거 provenance 5필드 / ETF 스크리닝 SOP 의 **사람·에이전트 운영 규약**(코드 아님)
- `docs/portfolio/data_architecture.md` §4 Vector DB / §7 남은단계 4(DART·뉴스 sync) — **설계, 로컬 미구현**
- `docs/portfolio/portfolio_os_design_v2.md` §1.F / §3 Vector·Graph 승격안
- `main_mission/portfolio_os/lessons.py` — `lesson_candidates.evidence_ref` 를 통한 **간접 evidence 참조**(현재 유일하게 evidence 개념을 만지는 코드, 단 본 영역 테이블엔 적재 안 함)

> ⚠️ **핵심 사실**: 본 영역(뉴스/공시/리포트/실적/배당 수집 → evidence 저장 → 근거→목표비중 연결)은 **현재 거의 전부 미구현**이다. RDB 메타 테이블 2개(`evidence_documents`, `decision_evidence_links`)의 **DDL만 존재**하며, 이를 적재(INSERT)·조회(SELECT)·연결하는 **Python/웹 코드는 0건**이다. DART·뉴스 sync job 도 미연동이다. 아래 각 섹션은 이 사실을 전제로 한다.

---

## 1. 목적

Research / Evidence Agent 는 **"목표비중 변경의 근거를 출처·신선도·확신도와 함께 저장하고, '어떤 근거 → 어떤 비중변경'을 추적 가능하게 연결"** 하는 영역이다. CLAUDE.md 의 본질 원칙("자동매매가 아니라 안전한 위임 + 추적 가능한 의사결정")을 데이터 측에서 떠받친다.

핵심 책임(설계 기준):
1. 뉴스 · 공시(DART) · 리포트 · 실적(fundamental) · 배당(dividend) 수집 — **백엔드 sync job 전용**(웹 직접 호출 금지).
2. 수집한 근거를 메타(`source_type`/`freshness`/`confidence`/`affected_theme`/`affected_asset`)와 함께 `evidence_documents` 에 저장.
3. 결정(`decisions`/목표비중 변경)과 근거를 `decision_evidence_links` 로 연결 — `weight_change` 와 `note` 로 "근거 → 비중변경" 명시.
4. (승격 계획) 본문 임베딩을 Vector DB 에 적재해 의미검색, 관계를 Graph 로 설명.

> 설계 의도(`portfolio_os_design_v2.md` §1.F): *"'Claude가 판단함'이 아니라 '어떤 근거→어떤 비중변경'을 저장한다."* — 즉 reasoning 의 provenance 화가 본 영역의 존재 이유다. 지능 자체는 Anthropic API 가 아니라 Claude+메모리 에이전트가 수행한다(CLAUDE.md §2 규칙 17).

---

## 2. 전체 흐름

설계상 흐름(대부분 미구현 — 점선은 코드 없음):

```text
외부 소스(DART 공시 · 뉴스 · 운용사 리포트 · 실적/배당 일정)
        ┊ (수집은 백엔드만, 웹 직접 호출 금지)
        ▼   ── 미구현: DART/뉴스 sync job 없음 (data_architecture §7-4)
[근거 수집 + provenance 부여]
   source_type / freshness / confidence / affected_theme / affected_asset
        ┊
        ▼   ── 미구현: 적재 코드 없음
evidence_documents (RDB 메타)         ← 스키마만 존재
        ┊
        ▼   ── 미구현: 링크 생성 코드 없음
decision_evidence_links               ← 스키마만 존재
   decision_id ↔ evidence_id + weight_change + note
        ┊ (어떤 근거가 어떤 비중변경으로)
        ▼
[목표비중 초안 / decisions]            ← decision.py·allocation.py 는 evidence 미참조
        ┊
        ▼   ── 승격 계획(PostgreSQL 후)
Vector DB(본문 임베딩 의미검색) · Graph Index(종목-뉴스-리스크 관계)
```

**현재 실제로 동작하는 인접 흐름**은 `lesson_candidates.evidence_ref`(텍스트 참조) 뿐이며(`lessons.py`), 이는 본 영역의 `evidence_documents.id` 를 가리키도록 설계됐으나 실제로는 자유 텍스트로 들어가고 evidence 테이블과 FK·조인되지 않는다.

---

## 3. 입력

설계상 입력(현재 수집 경로 미구현):

| 입력 | 출처(계획) | 현재 |
|---|---|---|
| 뉴스 | 언론 피드 | **미연동** |
| 공시 | DART OpenAPI | **미연동**(`data_architecture.md` §7-4 "DART/뉴스 sync job → 이벤트·공시·실적 일정 DB 적재" = 남은 단계) |
| 리포트 | 운용사/증권사 자료 | **미연동** |
| 실적 | fundamental(`evidence_documents.scope='fundamental'`) | **미연동** |
| 배당 | dividend(`evidence_documents.scope='dividend'`) | **미연동** |
| 근거 provenance | research-chief 가 부여하는 5필드(`source_type`·`source_url_or_ref`·`as_of_date`·`confidence`·`reproducible`) | **운영 규약(에이전트 문서)만 존재**, 코드 검증 없음 |

> 입력 수집은 CLAUDE.md §2 규칙 18(웹 조회 전용) + `data_architecture.md` §1 에 따라 **반드시 백엔드 sync job** 이 담당해야 하며 웹은 KIS·DART·뉴스를 직접 호출할 수 없다. 현재 그 sync job 자체가 없다.

---

## 4. 출력

설계상 출력:
- `evidence_documents` 행(근거 1건 = 1행): `scope`(news|disclosure|report|fundamental|dividend) · `ref`(종목/테마) · `source_type` · `title` · `body` · `url` · `freshness` · `confidence` · `affected_theme` · `affected_asset`.
- `decision_evidence_links` 행: `decision_id` ↔ `evidence_id` + `weight_change`(근거가 유발한 비중변경) + `note`.
- (승격) Vector 임베딩, Graph 노드/엣지(`종목→뉴스/공시→리스크`).

**현재 실제 출력: 없음.** 위 테이블에 INSERT 하는 코드가 존재하지 않는다(§13).

---

## 5. DB 테이블

본 영역 직접 소유 테이블 — `main_mission/portfolio_os/store/schema.sql` 정의:

### `evidence_documents` (라인 283~296) — 근거 문서 RDB 메타
| 필드 | 의미 |
|---|---|
| `id` | PK (AUTOINCREMENT) |
| `scope` | news \| disclosure \| report \| fundamental \| dividend |
| `ref` | 종목/테마 키 |
| `source_type` | 출처 유형(공식공시/운용사/언론 등) |
| `title` / `body` / `url` | 제목 / 본문 / 링크 |
| `freshness` | 발행/수집 시점(신선도 근거) |
| `confidence` | 0~1 확신도 |
| `affected_theme` / `affected_asset` | 영향 테마 / 영향 종목 |
| `created_at` | 적재 시각 |

> 주석(라인 282): *"본문 임베딩은 Vector 승격 시."* → RDB 에는 메타+본문만, 의미검색용 임베딩은 PostgreSQL 승격 후.

### `decision_evidence_links` (라인 299~306) — 결정↔근거 링크
| 필드 | 의미 |
|---|---|
| `id` | PK |
| `decision_id` | 결정 참조(`decisions.id` 추정 — FK 제약 없음) |
| `evidence_id` | 근거 참조(`evidence_documents.id` 추정 — FK 제약 없음) |
| `weight_change` | 이 근거가 유발한 비중변경(%) |
| `note` | 설명 |
| `created_at` | 시각 |

> ⚠️ 두 테이블 모두 **외래키(REFERENCES) 제약이 없다**. `holdings.snapshot_id` 같은 다른 테이블과 달리 무결성 강제가 없어, 적재 구현 시 애플리케이션 레벨 검증이 필요하다.

### 인접 테이블(본 영역이 참조/연결할 대상)
- `decisions`(라인 154) — `decision_evidence_links.decision_id` 의 연결 대상(payload=JSON: total/cash/lines/risk).
- `target_allocations`(라인 225) / `allocation_selections`(라인 310) — 목표비중 제안/확정. 근거→목표비중 연결의 최종 귀착점(현재 evidence 미참조).
- `lesson_candidates`(라인 264) — `evidence_ref` 필드로 evidence 를 간접 참조(현재 유일하게 evidence 개념을 만지는 코드, `lessons.py`).

해당 없음: Vector DB 테이블(`memory_embeddings` 등) / Graph `*_links` 테이블 — **로컬 SQLite 미존재**(PostgreSQL 승격 시, `data_architecture.md` §4·§5).

---

## 6. API / 함수

**본 영역 전용 함수: 없음(미구현).** `evidence_documents`/`decision_evidence_links` 를 적재·조회·연결하는 Python 함수나 웹 API route 가 코드베이스에 존재하지 않는다(Grep 확인: 두 테이블명은 `schema.sql` 과 docs 에서만 출현).

인접하게 evidence 개념을 다루는 **유일한 실제 함수**(본 영역 테이블엔 적재 안 함):
- `main_mission/portfolio_os/lessons.py`
  - `add_candidate(..., evidence_ref=None, ...)` — `lesson_candidates.evidence_ref`(텍스트)에 근거 참조 저장. 동일 (scope, ref, title) 존재 시 `observed_count` 증가·`evidence_ref` COALESCE 갱신.
  - 승격 기준 함수: `evidence_ref` 또는 `outcome` 이 있고 `confidence >= MIN_CONFIDENCE(0.6)` 이며 `observed_count >= MIN_OBSERVED` 일 때 `lessons` 로 승격(`_eligible`). 즉 **"근거 또는 결과"가 lesson 승격의 필수 조건**으로, evidence 의 존재가 성장 루프에 이미 의미를 갖도록 설계됨.

운영 규약(코드 아님, `agents/portfolio/research-chief.md`):
- 모든 근거 노트 5필드 필수: `source_type` | `source_url_or_ref` | `as_of_date` | `confidence`(high/med/low) | `reproducible`(yes/no). "도메인 지식 추정"은 confidence=low + reproducible=no 강제.
- ETF 후보 6필드 스크리닝(TER·실질비용·AUM·tracking difference·유동성·레버리지/인버스), 각 값에 provenance 동반.
- 이 규약을 **enum 검증하는 코드는 없음** — 현재 에이전트(사람+Claude)가 수동 준수.

---

## 7. UI 화면

**해당 없음(미구현).** 근거 열람/연결 전용 화면이 없다. `data_architecture.md` §1 원칙상 웹은 DB truth 조회 전용이어야 하므로, 향후 화면은 `evidence_documents`/`decision_evidence_links` 를 `node:sqlite` 로 SELECT 만 하는 조회 페이지가 되어야 한다(쓰기는 백엔드 sync job 경유). 현재 그 테이블이 비어 있어 표시할 truth 자체가 없다.

참고: 인접 영역의 의사결정 화면(`/portfolio`)은 아직 `lib/portfolio/mock.ts` 사용 중(`data_architecture.md` §6 ⏳)이며, mock 제거 후에도 evidence 연결 표시는 별도 미구현.

---

## 8. 상태 전이

본 영역에서 의미 있는 상태값은 인접 테이블에 정의돼 있으나 **본 영역 코드가 전이를 일으키지 않는다**:
- `lesson_candidates.status`: `candidate` → `promoted` | `rejected` (전이는 `lessons.py` 가 수행, evidence_ref 가 승격 조건의 하나).
- `evidence_documents` / `decision_evidence_links`: **상태 컬럼 없음**(append 모델). 신선도는 `freshness` 값으로 표현하며 stale 판정 로직 미구현.

해당 없음(미구현): 근거의 수집→검증→연결→outdated decay 같은 상태머신은 설계(`portfolio_os_design_v2.md` §1.G "outdated decay")만 있고 코드 없음.

---

## 9. 예외 / 실패 케이스

미구현이므로 런타임 예외 처리도 없다. 설계상 다뤄야 할(현재 미처리) 케이스:
- 출처 없는 수치 인용 → 금지(research-chief.md "절대 안 하는 것"). **enum/코드 검증 없음.**
- 미래 수익 단정/보장 표현 → 금지(Fact/Opinion 분리). **코드 검증 없음.**
- DART/뉴스 수집 실패 → sync job 자체가 없어 미처리(향후 `sync_events` 패턴 재사용 권장).
- 신선도 만료(stale) 근거가 비중변경에 사용됨 → `freshness`/`confidence` 컬럼은 있으나 stale 차단 로직 미구현.
- FK 무결성 깨짐(`decision_id`/`evidence_id` 가 존재하지 않는 행 가리킴) → DDL 에 FK 제약이 없어 **앱 레벨 검증 필요**(현재 없음).

---

## 10. Hard-block 조건

**본 영역 자체에는 hard-block 이 없다(미구현).** 근거 수집/저장은 주문 경로가 아니므로 직접적 매매 차단을 하지 않는다.

단, 본 영역이 떠받치는 공통 hard-block 원칙(다른 영역에서 강제, `06-risk-gate.md` 참조):
- 목표비중 없이 주문후보 금지 — 근거→목표비중 연결이 비어도 주문 경로는 `allocation_selections`(active) 부재 시 막힌다(본 영역 무관, 인접 영역 책임).
- 사람 승인 없이 주문 금지 / live 는 `KIS_LIVE_CONFIRM` 없이 하드차단(`factory._require_live_confirm`) — 본 영역과 무관하나 동일 시스템 불변식.

설계상 본 영역이 향후 가져야 할 약한 게이트(미구현): "근거 없는(또는 stale·low-confidence) 비중변경은 경고/보류 후보로 강등"(research-chief.md 의 "미공시는 stale flag 로 강등", "임계 미달은 제외 사유 enum" 정신). 현재 코드화 안 됨.

---

## 11. 로그 / 감사 기록

- 공통 감사 인프라 `audit_logs`(schema.sql 라인 85) 는 존재하나, **본 영역 액션을 적재하는 코드는 없음**(미구현). 향후 근거 수집/연결도 `audit_logs`(actor/action/entity_type/entity_id/payload) 패턴으로 기록해야 한다(`entity_type='evidence_documents'` 등).
- 신선도(freshness) 자체가 일종의 provenance 로그 역할: `evidence_documents.freshness`(발행/수집 시점) + `confidence` + `source_type` 로 "언제·어디서·얼마나 믿을 근거인가"를 행 단위 기록(설계). 적재 코드 미구현.
- `decision_evidence_links.note` + `weight_change` 가 "근거→결정" 추적 기록의 핵심(설계). 미구현.

---

## 12. 테스트 기준

**본 영역 전용 테스트: 없음.** `tests/` 디렉터리에는 `test_risk_gate.py`, `test_order_safety.py` 만 있고 evidence 관련 테스트 파일이 없다.

간접 커버리지: `lessons.py` 의 evidence_ref 승격 조건(`evidence_ref` 또는 `outcome` 존재 + `confidence>=0.6`)은 lessons 테스트 범위에서만 의미를 가지나 전용 테스트는 확인되지 않음.

향후 테스트해야 할 기준(설계):
- 근거 적재 시 5필드 provenance 누락 거부.
- stale/low-confidence 근거가 목표비중 변경에 단독 사용될 때 경고.
- `decision_evidence_links` 가 존재하는 decision_id/evidence_id 만 참조(앱 레벨 FK 검증).

---

## 13. 현재 구현 상태

**구현됨 (실제 코드/스키마 존재):**
- ✅ `evidence_documents` 테이블 DDL (schema.sql 282~296) — 메타 컬럼 완비(scope/ref/source_type/freshness/confidence/affected_theme/affected_asset).
- ✅ `decision_evidence_links` 테이블 DDL (schema.sql 299~306) — decision_id/evidence_id/weight_change/note.
- ✅ `lesson_candidates.evidence_ref` 필드 + 그를 사용하는 승격 로직(`lessons.py`) — evidence 의 존재가 lesson 승격 필수조건이라는 **성장 루프 연결점**만 실재.
- ✅ research-chief 운영 규약(provenance 5필드, ETF 6필드 스크리닝) — **문서로만**(에이전트 SOP).

**미구현 (코드 0건):**
- ❌ DART/뉴스/리포트/실적/배당 **수집 sync job** — 없음(`data_architecture.md` §7-4 = 남은 단계).
- ❌ `evidence_documents` **적재(INSERT) 코드** — 없음.
- ❌ `decision_evidence_links` **생성 코드** — 없음. 따라서 "근거→목표비중 연결"이 실제로 한 번도 일어나지 않음.
- ❌ `decision.py`/`allocation.py` 가 evidence 를 참조하는 경로 — 없음(`03-allocation.md` 라인 235·261 명시: "evidence_documents/decision_evidence_links 스키마만 존재, 이 영역에서 사용 안 함").
- ❌ evidence 조회 **웹 API / UI** — 없음.
- ❌ provenance/Fact·Opinion/미래수익단정 **enum 검증 코드** — 없음(규약만).
- ❌ Vector DB / Graph Index — 로컬 SQLite 미존재(PostgreSQL 승격 시, §3 Vector·§5 Graph 미구현).

요약: **본 영역은 "RDB 메타 테이블 2개의 빈 스키마 + lessons 의 텍스트 참조 1개 + 에이전트 운영 문서"가 전부이며, 수집·적재·연결·조회의 end-to-end 경로는 0% 동작한다.**

---

## 14. 미구현 / placeholder

- DART/뉴스/리포트/실적/배당 수집 sync job (백엔드 전용) — 전무.
- `evidence_documents` 적재 함수 (provenance 5필드 부여 포함).
- `decision_evidence_links` 생성 (근거→비중변경, weight_change/note).
- `decision.py`/`allocation.py` 의 evidence_id 참조 (목표비중 초안에 근거 연결).
- 신선도(freshness) 만료·confidence decay·outdated archive 로직.
- provenance/미래수익단정/출처누락 enum 검증 코드 (현재 research-chief.md 규약뿐).
- evidence 조회 웹 API(`node:sqlite` SELECT) 및 UI 화면.
- `evidence_documents`/`decision_evidence_links` FK 무결성 검증 (DDL 에 FK 제약 부재 → 앱 레벨 필요).
- Vector DB 본문 임베딩 + 의미검색 (PostgreSQL/pgvector 승격 시).
- Graph Index 관계(`종목→뉴스/공시→리스크`, `decision→evidence→risk→order`) (승격 시).
- `audit_logs` 에 evidence 수집/연결 액션 기록.
- 본 영역 전용 테스트.

---

## 15. 다음 개선 항목

`portfolio_os_design_v2.md` §4 구현 순서 기준(본 영역은 10번 "중전제 자료조사 → 목표비중 초안 시연" 근방):

1. **DART/뉴스 최소 sync job** — 1~2 소스만으로 `evidence_documents` 적재 PoC(백엔드 전용, 웹 직접호출 금지). `sync_events` 패턴 재사용해 freshness/source/오류 기록.
2. **적재 시 provenance 강제** — research-chief 5필드를 `source_type`/`freshness`/`confidence` 컬럼에 매핑하고 누락/미래단정/출처없음 enum 검증.
3. **`decision_evidence_links` 생성 경로** — `decision.py` 또는 `allocation.py` 가 목표비중 초안을 만들 때 사용한 근거를 `evidence_id` + `weight_change` 로 링크. "어떤 근거→어떤 비중변경" 최초 실현.
4. **stale/저신뢰 강등 게이트** — freshness 만료·confidence<임계 근거가 단독으로 비중변경에 쓰이면 보류 후보로 강등(hard 아닌 warn).
5. **조회 UI** — `node:sqlite` SELECT 전용으로 decision 별 연결된 근거 목록 표시(쓰기 없음).
6. **lesson_candidates.evidence_ref 정합화** — 텍스트 참조를 실제 `evidence_documents.id` 로 연결(현재 자유 텍스트 → FK 의미 부여).
7. **(승격)** PostgreSQL 후 Vector 본문 임베딩 + Graph 관계 — 의미검색/관계설명.

---

## 16. 다른 Agent와의 의존성

| Agent / 영역 | 관계 | 내용 |
|---|---|---|
| **Strategy / Profile** (`02-strategy-profile.md`, profile.py·policy.py) | **상류** | 중전제(`investor_profile.interests_text`/`views_text`)가 어떤 근거를 수집할지의 맥락. 근거는 이 관심·견해를 뒷받침/반박해야 함. (현재 연결 코드 없음) |
| **Allocation** (`03-allocation.md`, allocation.py) | **하류(핵심 귀착)** | 본 영역의 evidence 가 `target_allocations`(anchor+tilt 3안) 초안의 근거여야 함. `03-allocation.md` 라인 235·261 명시: **현재 "스키마만 존재, 이 영역에서 사용 안 함"** — 가장 중요한 미연결 지점. |
| **Decision/Order** (decision.py, order_service.py) | **하류** | `decision_evidence_links.decision_id` 가 `decisions` 를 가리킴. 근거→결정 추적의 종착. 현재 decision.py 는 evidence 미참조. 주문 안전(시장가매수금지·승인·live 가드)은 그 영역 책임(본 영역 무관). |
| **Risk Gate** (`06-risk-gate.md`, risk/gate.py) | **간접** | 근거가 리스크 판단의 입력(예: 공시→섹터 집중 경고). 현재 게이트는 evidence 미참조(`06-risk-gate.md` 의 violations 는 비중·stale 만 사용). |
| **Lessons / 성장** (lessons.py) | **양방향(부분 실재)** | 유일하게 동작하는 연결. `lesson_candidates.evidence_ref` 가 본 영역 근거를 가리키고, "근거 또는 결과" 존재가 lesson 승격 필수조건. 단 실제 FK 조인은 아직 없음. |
| **Web (조회 전용)** | **하류(읽기)** | 향후 `node:sqlite` SELECT 로 evidence/링크 조회만. 쓰기·KIS·DART 직접호출 금지(`data_architecture.md` §1). 현재 화면 없음. |
| **research-chief agent** (`agents/portfolio/research-chief.md`) | **수행 주체** | 실제 근거 조사/provenance 부여를 담당하는 에이전트(Claude+메모리, Anthropic API 미사용). 산출물을 본 영역 테이블로 영속화하는 코드가 미구현. |
