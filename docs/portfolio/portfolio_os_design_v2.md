# Portfolio OS 설계 v2 — 외부 벤치마크 · gap · DB 승격안

> 본 문서는 CEO 지시(2026-06-20, "두뇌 루프 전에 부문별 벤치마크 후 정교화")의 산출물.
> 관점: **외관·마케팅 제외. 포트폴리오 운영방식·데이터구조·리스크·리밸런싱·근거저장만.**
> 최종 기준: *"이 기능이 계좌를 목표 포트폴리오에 더 안전하고 설명 가능하게 가깝게 만드는가?"*

---

## 1. 부문별 외부 벤치마크 (≥2건/부문, 구조 관점)

### A. Investor Profile / 투자전제 구조화
- **Betterment**: goal-based. 목표유형+투자기간 → glide path. 설문 → 위험점수 → 주식/채권 비중. 목표별로 별도 포트폴리오.
- **Wealthfront**: 객관적+주관적 위험감내 설문 → risk score(0.5~10) → 다자산 배분. 세금(자산위치·TLH) 반영.
- **Aladdin Wealth**: 위험을 stock/bond가 아니라 **factor exposure + risk budget**으로 표현.
- **배울 점**: 선호 → **policy object**(위험점수 + 제약). hard 제약(금지자산·단일상한)과 soft 선호 분리.
- **그대로 안 됨**: 미국 세금/은퇴 glidepath 가정, 독점 설문 스코어링.
- **우리 적용**: 대전제 posture → policy object(성향·현금밴드·단일/섹터/국가/통화/개별 한도·pace·금지자산). 자연어=원문/문서, 변수=컬럼.

### B. Anchor + Tilt / 목표비중
- **Vanguard SAA**: 전략적 자산배분(anchor) 장기 고정 + 소폭 전술 조정.
- **Black-Litterman**: 시장균형(anchor) + 투자자 view(tilt)를 **confidence 가중**으로 혼합. tilt 크기 제한.
- **BlackRock**: strategic(장기) vs tactical(단기), tilt 한도 적용.
- **배울 점**: anchor=장기 중립, tilt=한도 있는 편차(확신 가중), 능동위험 총량 cap.
- **그대로 안 됨**: BL 수식/시총균형은 KR 리테일에 직접 적용 어려움.
- **우리 적용**: **대전제→anchor**(현금+광범위 envelope), **중전제 테마→tilt**(테마별·총합 cap). 보수/기준/공격 **3안** 생성 후 사람 선택.

### C. Drift / Rebalancing
- **Betterment**: **5/25 룰**(절대 5%p 또는 상대 25% 이탈 시). **cash-flow rebalancing**(입금·배당으로 저비중 매수 → 매도/세금 회피). TaxMin 순서.
- **Vanguard 리서치**: time-only vs threshold vs **time+threshold 혼합**. 주기점검+밴드(예 5%) 권장. 추적오차 vs 비용 균형.
- **Wealthfront**: threshold + 세금 인지.
- **배울 점**: threshold+calendar 혼합, **cash-flow-first**, 밴드는 변동성/자산군별.
- **그대로 안 됨**: 미국 tax-lot 최적화.
- **우리 적용**: 밴드=min(절대5, 상대25%) 유지하되 **자산군/변동성별 조정 가능**하게. 입금·배당·매도대금 우선(cash-flow-first). 3~5회 분할. **pace→주기**(slow=주 단위, normal=며칠, urgent=별도 승인).

### D. 분할 매수/매도
- **기관 집행(TWAP/VWAP)**: 대형 주문 슬라이스, 지정가, 참여율 cap.
- **DCA/스케일인**: 분할 진입, 불리하면 보류.
- **배울 점**: 슬라이스, 지정가, 불리 시 보류, 다음 cycle 재평가.
- **우리 적용**: 총조정 vs 이번회차 분리, 회차/남은회차/남은금액/보류조건 표시, qty0·불리=**보류 후보**, 1회차 상한, 실주문 전 paper 검증.

### E. Risk / Exposure
- **Aladdin**: **factor 기반 위험분해 + scenario/stress + what-if**(거래 전 영향 시뮬). 포트폴리오 단위 위험.
- **Betterment/Wealthfront**: 단순 — 배분 drift + 분산.
- **배울 점**: 다차원 노출(단일·섹터·국가·통화·factor·ETF중복), 스트레스 시나리오, 거래 전 what-if.
- **그대로 안 됨**: 풀 factor model은 과중.
- **우리 적용**: 리스크 게이트=**방향 검증기**. 단일/섹터/국가/통화/ETF중복/인버스·레버리지/현금 계산. 단순 스트레스(반도체 -10%, 환율 +5%, 금리↑, 바이오 급락). 관계는 Graph.

### F. Research / Evidence
- **Aladdin RMS류**: 리서치 노트를 종목에 링크, 데이터+분석 연결.
- **RAG/Vector store**: 문서 임베딩 검색, evidence→decision 링크.
- **배울 점**: evidence를 메타(source/freshness/confidence)와 함께 저장, **근거→비중변경 링크**.
- **우리 적용**: Vector DB(뉴스/공시/리포트/과거 reasoning) + RDB `evidence_documents`/`decision_evidence_links`. 모든 목표비중 초안에 evidence_id. "Claude가 판단함"이 아니라 "어떤 근거→어떤 비중변경"을 저장.

### G. Lessons / 성장
- **ExpeL / Reflexion**: 경험을 재사용 인사이트로 distill, 반복 패턴 승격.
- **리스크 플랫폼 post-trade attribution**: 사후 분석으로 학습.
- **배울 점**: 원시 로그 ≠ 승격 원칙. 승격 기준(반복성·근거·결과·confidence). outdated decay.
- **우리 적용**: `lesson_candidates` vs `lessons` 분리. 승격 기준 함수. scope 명확. confidence decay/archive.

### H. Strategy Document Viewer
- **IPS(Investment Policy Statement)**: 목표·제약·배분정책·리밸런싱정책·점검주기를 담는 공식 문서.
- **제안서/리스크 리포트**: 구조화·버전관리.
- **배울 점**: IPS 구조, 사람읽기+기계정책값 링크, 버전 diff.
- **우리 적용**: `/strategy/view` = IPS: 원문 + 추출변수 + 보완질문 + **최종 정책값** + 변경이력 + **적용된 decision 목록**. 변수→decision 추적, 버전 diff, 사람수정 vs 자동추출 구분.

---

## 2. 현재 구현 gap 분석

| 영역 | 현재 | gap → 목표 |
|---|---|---|
| profile | 컬럼+doc+history | **policy object 미승격**(한도 일부만, 국가/통화/금지자산 없음) |
| 목표비중 | universe 수동 target_weight | **anchor+tilt 생성 규칙 없음**, 3안 없음, tilt cap 없음 |
| drift | 5/25 고정 | 자산군/변동성별 조정·cash-flow-first 없음 |
| 분할 | total/cycle/rounds 계산 | **DB 미저장**(plan/steps 테이블 없음), 보류후보 분류 약함 |
| risk | 단일·섹터·qty0·stale·현금밴드 | 국가/통화/ETF중복/스트레스 없음, Graph 없음 |
| evidence | 없음 | evidence_documents·decision_evidence_links 없음 |
| lessons | lessons 1개 테이블 | **후보/승격 미분리**, 승격기준·decay 없음 |
| viewer | 문서+이력 | IPS 아님(정책값/적용 decision/diff 없음) |

---

## 3. DB 승격안 (SQLite MVP → PostgreSQL + Vector + Graph)

### RDB (truth — 반드시 RDB)
`investor_profiles` · `investor_profile_versions`(=현 investor_profile_history) · **`portfolio_policies`**(컴파일된 정책 객체, 버전) · **`target_allocations`**(anchor+tilt 3안 제안) · `account_snapshots` · `price_snapshots`(현 quotes 승격) · `portfolio_decisions`(현 decisions) · **`rebalance_plans`** · **`rebalance_plan_steps`** · `risk_checks` · `orders` · `order_events` · `lessons` · **`lesson_candidates`** · **`evidence_documents`** · **`decision_evidence_links`**

### JSON document (스키마 미고정·진화)
자유 컨셉 원문 · 보완 코칭 · Claude analysis note · 테마별 해석 · 지역배분 아이디어 · 말투/의도. → 현 `investor_profile.doc`, 확장 시 별도 doc store.

### Vector DB (의미검색) — PG 승격 시
뉴스 · 공시 · 리포트 · 종목/ETF 설명 · 과거 decision reasoning · 승인/거절 사유 · 장기 lesson 설명.

### Graph Index (관계) — PG 승격 시
계좌-종목 · 종목-섹터 · ETF-구성종목 · 종목-뉴스/공시 · decision-evidence-risk-order · 테마-종목-섹터 · 국가/통화 노출.

> **승격 전제 설계 원칙**: 모든 RDB 키는 정수 PK + scope/ref 패턴(Graph 이식 용이), 모든 truth는 RDB, 진화내용은 doc, 의미검색은 Vector, 관계설명은 Graph. SQLite는 동일 스키마로 시작해 PG로 무손실 이관.

---

## 4. 구현 순서 (CEO 지정)
1.~4. 본 문서(벤치마크·gap·DB안) ✅
5. profile → **policy object 승격**(`policy.py` + `portfolio_policies`)
6. **anchor+tilt 3안**(`allocation.py` + `target_allocations`)
7. drift/rebalance를 **회차 단위 저장**(`rebalance_plans`/`steps`)
8. **lesson 후보/승격 분리**(`lesson_candidates` + 승격기준)
9. **viewer → IPS**(정책값·적용 decision·diff)
10. (다음) 중전제 자료조사 → 목표비중 초안 시연
