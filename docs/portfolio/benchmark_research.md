# Portfolio OS — 부문별 외부 벤치마크 조사 (8부문, 각 ≥2건)

> CEO 지시 (2026-06-20). 외관/마케팅 아닌 **기능·기술·데이터흐름·리스크·저장·운영** 관점.
> 8개 부문 에이전트가 실제 외부 사례를 조사한 결과 종합. 적용은 CEO 원칙(웹=조회전용, DB truth, 하드코딩 0, API 미사용=Claude+메모리, 시장가 정책) 안에서.

---

## 1. 종목 유니버스 / 검색
- **출처**: Alpaca Assets API, IBKR Contract/SecDef, OpenFIGI, KIS 종목마스터(mst).
- **핵심**: 자산 레지스트리(ticker·name·exchange·currency·sector·asset_class·is_leveraged/inverse·is_delisted)를 DB 동기화 + 사용자 입력 2단 검증(정규식 → DB/시세 조회). conId(IBKR)·tick_size·min_qty 메타.
- **적용(MVP, 즉시 Y)**: **직접 입력(ticker) + KIS inquire-price 검증 + DB 저장 + 목표비중 편집 + 비활성화**. mock 종목목록 제거.
- **확장(later)**: KIS mst 마스터 적재, 이름 자동완성(FTS), tick_size/호가단위 주문검증, 폐지/거래정지 실시간 체크.

## 2. 목표비중 / 리밸런싱 엔진
- **출처**: Betterment(5/25 rule 백서), Wealthfront(band-edge), Vanguard(rebalancing 백서), 한국 로보(라쿤/오션).
- **핵심**: 우리 5/25 rule(min(절대5%p, 목표×25%))은 **적절**(이미 구현). 개선점 = ① **섹터/국가/통화 집중도 한도**(현재 단일종목만) ② **현금 동적 밴드**(VKOSPI 연동, 하한 8%) ③ 최소주문 0.1%→0.5~1% ④ 정기 리밸런싱 스케줄.
- **적용(later)**: concentration_limits 테이블 + 게이트 확장(섹터/국가/통화). 현금밴드는 대전제(investor_posture)와 연결.

## 3. 리스크 게이트 (주문 전 hard-block)
- **출처**: Alpaca(buying power/position limit), IBKR(margin liquidation), KRX VI·서킷브레이커, LULD.
- **핵심**: 현재 6한도 + order_service 검증은 견고. **빠진 것** = ① **누적 drawdown 서킷브레이커**(고점 대비, 일일손실만으론 trending loss 방어 부족) ② **레버리지/인버스 보유기간 한도**(decay 방어, 인버스 5일/2x 10일/3x 5일) ③ 시장 서킷브레이커/VI 발동 시 신규주문 차단 ④ 시간대(개장15분/종료10분) 강화.
- **적용(즉시 Y 일부)**: risk_limits에 max_drawdown_stop + 보유기간 한도 컬럼, gate.py 확장. (실시간 VI/서킷 감지는 수집 job 필요 → later)

## 4. 주문 정책 / 체결 관리
- **출처**: Alpaca(order lifecycle/validation chain), IBKR(in-doubt state machine, TWAP/VWAP 분할), 지정가 산정(지지선·MA·ATR·VWAP).
- **핵심**: ① 주문 상태머신에 validation_stage 라벨 ② **무릎 가격(knee price)** = 지지선/ATR/VWAP 혼합으로 지정가 예측진입가 산정(시장가 매수금지 보완) ③ in-doubt 자동 재조회(폴링) ④ 분할주문(TWAP, 일/주 기준).
- **적용(later)**: knee_price 모듈(데이터=과거시세 스냅샷 필요), 분할주문 계획. (KIS 실시간 tick 미제공 → 과거 스냅샷/외부 데이터 필요)

## 5. 리서치 / 근거 수집 루프
- **출처**: Open DART(공시), 한국은행 ECOS·FRED(매크로), 어닝 캘린더, FinDER(금융 RAG+신뢰도), audit-trail/provenance.
- **핵심**: 뉴스·공시·실적·매크로를 이벤트로 수집 → **근거를 출처·수집시각·신뢰도(공시0.95/리포트0.75/뉴스0.6)·재현성과 함께 DB 저장** → 제안의 tilt에 evidence/source_quote로 **연결**. "AI가 판단함"만 금지.
- **적용(later, 두뇌)**: financial_events·evidence_documents·event_decision_links·macro_indicators 테이블 + DART/ECOS 수집 job. **reasoning은 Claude+메모리**(API 아님).

## 6. Vector DB / Memory
- **출처**: ExpeL(경험학습, support/refute 투표·importance_count), Provenance-Aware Tiered Memory(3계층·bi-temporal·valid_until), pgvector, hybrid search(BM25+vector).
- **핵심**: **lesson 승격 게이트** = reflection(실패/hook gap) + recurrence≥3 + confidence≥0.6 + CEO 승인만. 일회성 로그(audit_logs) ≠ 장기메모리(lessons). provenance chain(proposal→risk→order→fill→결과) + staleness(valid_until, 6개월 재검증). 금액/주문 truth는 항상 RDB.
- **적용**: 로컬 SQLite 단계 = lessons에 support/refute/confidence/provenance_json/valid_until/is_critical 컬럼 + is_promotable() 게이트(순수 Python, 즉시 Y). **임베딩/2-stage retrieval은 PostgreSQL+pgvector 승격 시**(later).

## 7. Graph Index / 관계 추적
- **출처**: 금융 Knowledge Graph(Bloomberg/FactSet 공급망), ETF look-through/overlap(Morningstar/Vanguard tool), Temporal KG(Zep).
- **핵심**: 노드(종목·섹터·테마·국가·통화)+엣지(belongs_to·etf_component·competitor·currency_exposure) → "왜 이 리스크에 노출되는가" 설명. **ETF look-through 중복노출**(QQQ+직접보유 TSLA = 실제 노출 합산), 상관계수. 로컬 = edge 테이블 + recursive CTE(SQLite 지원).
- **적용(later)**: instrument_relations·etf_constituents·etf_lookthrough 테이블 + CTE. 깊이 한도(≤3~5)·순환 방지·stale(30일) 경고.

## 8. UI / 의사결정 플로우
- **출처**: Betterment(제안 3계층: goal→현황→action, before/after 시뮬), IBKR(주문 최종확인·상태머신·hard-block 4단 설명+해결옵션).
- **핵심**: ① 정보 3계층(목표·현황·액션) ② **차단 사유 = 위반종류+정량 gap+개선방안** ③ before/after 비중 시뮬 ④ 주문 전 **최종 확인 단계**(명시적 click) ⑤ override는 사유 필수+감사. 전부 **DB 저장 truth 조회**.
- **적용(later, step3와 함께)**: /portfolio를 DB 스냅샷 기반으로 + 차단사유 개선옵션 + 최종확인 모달.

---

## 채택 우선순위 (CEO 승인 순서: 유니버스 → 실계좌 → 리서치)

| 단계 | 내용 | 즉시 |
|---|---|---|
| **A. 종목 유니버스 MVP** | 직접입력+KIS검증+DB+목표비중편집+비활성화, mock 제거 | **이번 구현** |
| B. /portfolio 실계좌 DB 연결 | 현재비중=실잔고 스냅샷, 목표비중=유니버스 | 다음 |
| C. 집중도 한도·drawdown·보유기간 게이트 | 부문3 즉시 Y 항목 | 다음 |
| D. 리서치 루프(DART/ECOS) + 근거 DB + lesson 게이트 | 부문5·6, 두뇌(Claude+메모리) | 그 다음 |
| E. knee price·분할주문·Graph look-through | 부문4·7, 데이터 확보 후 | later |
| F. PostgreSQL 승격 → Vector/Graph 실구현 | 부문6·7 | later |

> 모든 단계: 웹=조회전용, 수집/검증/저장=백엔드 Python→DB, 하드코딩 0, reasoning=Claude+메모리(API 미사용).
> 출처 전체는 각 에이전트 보고 원문 참조(세션 기록).
