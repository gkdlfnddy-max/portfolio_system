# Portfolio OS — 의사결정 위계 (대전제 → 중전제 → 소전제)

> CEO 정의 (2026-06-20). 매 사이클 각 계층이 동적으로 갱신된다. 하드코딩 금지.
> 두뇌(자료조사·분석·조율·추천)는 **Claude + 메모리 에이전트**가 수행한다 — Anthropic API 미사용.

---

## 1. 3계층 위계

### 대전제 (Grand premise) — 투자 성향 자체
"나는 어떤 투자자인가"를 매번 정한다.

- 공격적 / 중립 / 방어적 (risk_tolerance)
- 숏(인버스) 허용 수준 — 0% / 보험 수준 / 적극 (short_policy)
- 현금 비중을 얼마나 **유동적으로** 운용할지 — 고정이 아닌 밴드(예: 20~40%) + 시장상황 연동 (cash_band)

→ 산출물: `investor_posture { risk_tolerance, short_policy, cash_band, leverage_policy }`
→ 이 값이 anchor(기본배분)와 tilt 상한·리스크 한도의 출발점.

### 중전제 (Mid premise) — 관심 분야 + 내 생각의 분석·조율
"무엇에 베팅하고 싶은가 + 그 생각이 타당한가"를 자료로 검증·조율한다.

- CEO 의 관심 섹터/테마 + 견해(자연어)
- **자료조사·분석**(뉴스·공시·매크로·실적) 으로 견해를 **검증/보정** = "조율"
- 견해와 자료가 충돌하면 CEO 에게 경고 (예: "반도체 축소 의견 ↔ 실적 호재 다수")

→ 산출물: 섹터/테마 `tilt[] { sector, direction, confidence, 근거(memory ref) }`
→ 근거는 메모리(과거 분석·결정·교훈)에서 인출하고, 새 분석은 메모리에 저장(성장 루프).

### 소전제 (Small premise) — 종목 선택 + ETF 구성
"구체적으로 무엇을 담는가"를 정한다.

- 종목/ETF **유니버스에서 검색 → 추가/삭제** (하드코딩 목록 아님)
- ETF 구성·중복 노출 점검 (예: 미국S&P500 ETF ↔ Apple 중복)
- 대/중전제 + 분석을 반영해 **목표비중** 산정

→ 산출물: `target_weights[]` (종목/현금/숏, 합 100%)

---

## 1.5 사용자 견해 = 1급 입력 (user_views)

CEO 가 자기 생각을 직접 넣는다("반도체 장기 긍정·단기 고점 같다", "바이오는 ETF로만",
"양자는 관찰만", "로봇 장기 조금"). 이 견해는 **Portfolio OS 판단의 1급 입력**이 된다.

- 저장: `user_views` 테이블 — **계좌별 격리(교차적용 금지)**.
  컬럼: layer(대전제 grand|중전제 mid|단기 short|장기 long) · theme · ticker · etf ·
  stance(positive|neutral|negative|observe) · conviction(0~1) · horizon · note · status · superseded_by.
- **계층 분리**: 같은 테마라도 단기(고점 경계)와 장기(긍정) 견해가 따로 공존한다.
- **이력 보존(supersede)**: 견해 변경 시 옛 행 `status=superseded` + `superseded_by=<새 id>`, 새 active 행 생성(덮어쓰기 아님).
- **견해 ≠ 데이터 우위/무시**: 데이터보다 무조건 우선하지도, 무시되지도 않는다.
  `compare_view_vs_data(account, ticker/theme, data_signal)` 가 stance 와 데이터 신호(예: 하락 위험↑)를
  비교해 `{agree|differ|conflict|observe|no_view}` + 설명을 반환. **충돌 시 어느 쪽이 옳은지 단정하지 않고 둘 다 제시**한다.
- **자동 적용 금지**: 견해는 **저장만** 한다. allocation/policy *draft* 에만 참고되고, 실제 반영은 Agent3 의
  advice_items 미승인 게이트(사장님 승인)를 거친다.
- 코드: `main_mission/portfolio_os/user_views.py` · 웹 입력 UI: `/accounts/[id]/views` (RBAC `requireAccountAccess`).

---

## 2. 진입 원칙 (불변) — 시장가 금지 · 예측 진입

포트폴리오에 종목을 추가/매수할 때:

- **시장가 매수 영구 금지.** 진입(매수)은 항상 **지정가(limit)**.
- 가격 흐름을 **예상**해 진입가를 정한다. *발끝(최저점)을 노리지는 않되, "이 정도면 무릎이다" 싶은 지점*.
- 타이밍 판단 기준: **일(日) · 주(週) 단위** (일중 추격매수 금지).
- 1주문 규모 한도(§ 리스크) + 분할 진입 허용.
- **예외 — 긴급 매도**: *정말 급하게 팔아야 할 때*에 한해 **시장가 매도 허용**. 명시적 플래그(`urgent_sell=True`)로만, CRITICAL 감사 기록. (시장가 *매수*는 예외 없이 금지.)

구현 강제:
- `order_service.submit_order`: `order_type=="market"` → 매수는 무조건 차단, 매도는 `urgent_sell=True` 일 때만 통과(감사 기록). 그 외 차단.
- 진입가 산정 근거(예상 밴드, 일/주 기준)는 제안에 첨부하고 메모리에 저장.

---

## 3. 두뇌 = Claude + 메모리 에이전트 (API 아님)

- 자연어 컨셉 해석, 자료조사, 분석, 조율, 초안 추천 reasoning 은 **Claude Code 에이전트**가 수행.
- 에이전트는 **메모리(과거 분석·결정·근거·교훈)를 인출해 재사용**하고, 새 판단을 메모리에 **저장**하며 성장한다.
- 운영 truth(금액·잔고·주문·체결)는 PostgreSQL/SQLite(RDB) 기준. 근거/추론은 메모리(추후 Vector/Graph).
- **Anthropic API / `ANTHROPIC_API_KEY` 의존 코드 금지.**

---

## 4. 사이클 흐름

```text
[대전제] 성향 갱신 (공격/숏/현금밴드)
   → [중전제] 관심분야 + 견해 → (Claude+메모리) 자료조사·분석·조율 → 섹터 tilt + 근거
      → [소전제] 유니버스 검색·선택 → 목표비중
         → drift 계산(실계좌 DB 스냅샷 기준)
            → 리밸런싱 제안 (지정가 예측진입가 포함, 시장가 금지)
               → 리스크 게이트 → CEO 승인 → 주문(추적) → 체결
                  → 회고 → 메모리 저장 (다음 사이클 재사용)
```

---

## 4.5 투자 목적·성향 — "최선"의 기준 (대전제의 토대)

> CEO 원칙: **"최선" ≠ 수익률 최대화.** 사람마다 최선이 다르다(손실 줄이기·잠 잘자기·
> 배당·변동성↓·thesis 유지·현금 확보·공격적 성장…). **목적을 먼저 확인**해야 그 관점의
> 최선을 계산할 수 있다.

- **저장 위치(스키마 무변경)**: 기존 `user_views` 테이블에 `layer='objective'`, `note=JSON` 1행.
  계좌별 격리(교차적용 금지) + supersede 이력 보존.
- **필드**: `investment_goal`(loss_reduction|dividend|growth|aggressive_growth|
  volatility_reduction|thesis_hold|cash_preservation|stable_operation) · `horizon` ·
  `risk_tolerance`(low|mid|high) · `loss_aversion`(0~1) · `prefers`(cash/bond/dividend/growth/etf 다중) ·
  `allows`(inverse/leverage bool) · `region_pref`(kr|us|global) · `market_view`(short|long) · `note`.
- **"최선 기준" 매핑** `objective_to_criteria(goal)` — 목적 → 평가지표 우선순위(규칙 기반, Anthropic 미사용).
  예: 손실축소 → max_drawdown↓·cash_band↑ / 배당 → dividend_yield↑·방어 / 성장 → CAGR↑·growth_tilt↑ /
  변동성축소 → volatility↓·분산. 각 항목 `{metric, direction(min/max), weight}`.
- **자동 적용 0**: 저장만 한다. allocation/관점별 후보(B 에이전트 `perspective_variants`)가 *읽어* 쓸 뿐,
  여기서 포트폴리오/주문을 바꾸지 않는다.
- **정직**: 목적 미설정이면 기본값을 *가정하지 않고* "목적 미설정 — 먼저 입력 권장"을 표시.
- 코드: `main_mission/portfolio_os/investor_objective.py` · 웹: `components/InvestorObjectiveForm.tsx`,
  `app/accounts/[id]/views/page.tsx`, API `app/api/accounts/[id]/objective/route.ts`(RBAC + 저장만).

---

## 5. 구현 매핑 (단계)

1. **소전제 골격 — 종목 유니버스**: KIS 종목마스터 기반 검색→추가/삭제, DB 저장 (하드코딩 목록 제거).
2. **실계좌 연결**: `/portfolio` 현재 비중을 mock → 실잔고 DB 스냅샷.
3. **대/중전제 입력 모델**: investor_posture + 관심분야/견해를 DB 에 저장, UI 동적 편집.
4. **리서치 루프 (Claude+메모리)**: 자료조사·분석·조율 → 근거 메모리 저장 → 초안 추천.
5. **진입가 산정**: 일/주 기준 예측 밴드 → 지정가 제안 (시장가 금지 강제).
