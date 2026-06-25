# 채권 정책 (Bond Policy) — Portfolio OS

> CEO 신규 방침: 채권은 **국채(government/treasury) 위주**, **현금의 일부**로 취급(방어자산 family).
> 채권은 invested(위험자산)에서 빼지 않고 **cash 에서 분리**한다.

관련: [decision_hierarchy.md](decision_hierarchy.md) · [safety_rules.md](safety_rules.md) · [growth_architecture.md](growth_architecture.md)

---

## 1. 핵심 원칙

- 채권 = **국채 위주**(government / treasury). 회사채·고위험 채권은 기본 제외(별도 승인 시에만).
- 채권은 **방어자산 family** = 현금의 일부로 취급한다.
- **현금밴드 총량 안에서** 일부를 국채로 배분(상황에 따라 동적).

```
defensive = pure_cash + govbond        # 합 = 현금밴드(cash band)
invested  = 위험자산 (채권 제외)
```

- 채권은 위험자산(invested)에서 carve 하지 않고, **cash 에서 carve** 한다.

---

## 2. 현금 vs 국채 역할

| 자산 | 역할 | family |
|---|---|---|
| 현금 (pure_cash) | 즉시 매수 여력(유동성) | defensive |
| 국채 (govbond) | 방어 + 소폭 캐리/금리 대응 | defensive |

- 둘 다 방어자산이지만 **역할이 다르므로 구분 표시**한다(현금밴드 안에서 pure_cash / govbond 를 분해해 보여준다).

---

## 3. Allocation 반영

- 채권 bucket: `kind = "bond"`, `ref = "국채·{duration}"` (예: `국채·중기`).
- 채권 비중은 **cash 에서 carve** 한다 (invested 미차감).
- 표시: 현금밴드 = `pure_cash` + `bond` 합으로 분해 표시.

```
cash_band_total = pure_cash_pct + bond_pct
# allocation 예: [{kind:"cash", ref:null, weight_pct: X},
#                 {kind:"bond", ref:"국채·중기", weight_pct: Y}, ...invested...]
```

---

## 4. Validate (cash-band 검사)

- cash-band 검사는 **(pure_cash + bond) 기준**으로 수행한다.
- 규칙: `bond_target > cash_max` → **block**.
  - 채권은 현금밴드 안에서 배분되므로, 채권 목표가 현금밴드 상한(`cash_max`)을 넘으면 차단.
- (pure_cash + bond) 합이 현금밴드 [`cash_min`, `cash_max`] 범위를 벗어나면 경고/차단.

| 검사 | 조건 | 결과 |
|---|---|---|
| 채권 상한 | `bond_target > cash_max` | block |
| 현금밴드 하한 | `pure_cash + bond < cash_min` | block/warn |
| 현금밴드 상한 | `pure_cash + bond > cash_max` | block/warn |

> 리스크 게이트(`risk/gate.py`, `regionbond.validate`)와 selection precheck 에서 동일 기준 적용.

---

## 5. 국채만 (government_only) — 정밀화

> CEO 방침 강화: 채권은 **국채만**. 회사채·하이일드·신흥국채·전환사채/구조화 등 **복잡채권 전면 금지**.

- 프로필 컬럼 `bond_allowed_types` 기본값 `government_only`. 그 외 값은 `profile.save()` 가 **강제로 government_only 로 복귀**(비국채 요청 거부).
- `regionbond.detect_non_government_bonds(text)` — 자유입력에서 비국채 의도(회사채/크레딧·하이일드/정크·신흥국채·전환사채/구조화) 라벨 추출.
- `regionbond.validate(..., bond_allowed_types, bond_intent_text)` — `government_only` 아님 → `limit="bond_allowed_types"` 위반, 비국채 의도 텍스트 → `limit="non_government_bond"` 위반(차단).
- `parse_bond()` 반환에 `allowed_types`(=government_only)·`non_government`(차단 라벨)·`notes`(정직 표기) 추가.

---

## 6. 듀레이션 (duration)

- `bond_duration_pref ∈ short | intermediate | long | mixed`.
- **mixed → 기본 단기50 / 장기50**. 사용자는 `bond_duration_split`({short, long}) 로 변경 가능 — `profile.save()` 가 합 100 으로 정규화(예 {30,10} → {75,25}).
- mixed 가 아니면 split 은 저장하지 않음(None).

---

## 7. 방어자산 내부 구성 — `bond_bucket.py` (계산·후보 전용)

> 자동 주문/policy 변경 **없음**. 계산과 후보 제시만 한다.

- `compute_breakdown(defensive, bond_ratio, duration, split)` → 순현금·국채(단기/중기/장기)·위험 **절대%**(전체 기준). carve 로직은 `allocation._variant` 와 **동일**:
  - `govbond = defensive × ratio/100`, `pure_cash = defensive − govbond`, `risk = 100 − defensive`.
  - 검산: 방어40·국채비율40% → 국채16·순현금24·위험60. mixed → 단기8·장기8.
- `defensive_breakdown(account)` → 계좌 현금밴드(target=방어 총량)+프로필로 위 분해 + 국채 ETF 후보 + 경고.
- `govbond_etf_candidates(duration, region)` — **국채 ETF 후보(seed)**. 미국 SHY/IEF/TLT(단/중/장).
  한국은 **KRX 상장 실 종목코드**(WebSearch 확인 2026-06):

  | 만기대 | 한국 국채 ETF 후보 | 종목코드 | ISIN |
  |---|---|---|---|
  | short | KODEX 단기채권 | `153130` | KR7153130000 |
  | short | KODEX 국고채3년 | `114260` | KR7114260003 |
  | intermediate | KODEX 국고채10년액티브 | `471230` | KR7471230003 |
  | long | KODEX 국고채30년액티브 | `439870` | KR7439870007 |
  | long | TIGER 국고채30년스트립액티브 | `451530` | KR7451530000 |

  - **종목코드(티커)는 실재 확인**했으나 **보수율/유동성/잔존만기 등 지표는 미연동** — 각 후보 `status="후보·검증 필요·데이터 미연동"`, `data_connected=false`. **가짜 지표는 적지 않는다**(정직).

```
python -m main_mission.portfolio_os.bond_bucket --account 1
python -m main_mission.portfolio_os.bond_bucket --candidates-only --duration mixed
```

---

## 8. 국채 비중 **추천형 엔진** — `bond_recommendation.py`

> CEO 목적: 사용자가 "국채 몇%?" 숫자를 찍는 게 아니라, **시스템이 거시+계좌 목적을 분석해 후보를 제시**하고 사용자가 고른다. 로보어드바이저 표준(단기~중기 기본·금리환경별 트레이드오프·장기채=변동성 큼).
> **추천일 뿐** — `requires_user_approval=true`, `auto_applied=false`. 자동 policy/주문 0. 실제 반영은 사용자 선택 → 3안(allocation) 재생성 → **재확정(확정안=truth)**.

진입점 2종:

- `recommend(account)` — 금리 동향 기반 **단일** 비중·듀레이션 추천(rate_regime · suggested_bond_ratio_pct · duration · 전체환산).
- `bond_options(account)` — **국채 비중 후보(A/B/C/D) 추천형 엔진**(신규).

### 8.1 `bond_options` — 후보 사다리

- 후보 비중 사다리(방어자산 대비 %): **0 / 25 / 40 / 50**. `rate_regime` + **계좌 성향**으로 **동적**으로 3~4안 선택.
  - **방어형**(loss_reduction/cash_preservation/volatility_reduction/dividend · risk=low · loss_aversion≥0.6) → 0 빼고 국채 적극(`25/40/50`); 단 인상기/고금리엔 `0/25/40`(50 절제).
  - **성장형**(growth/aggressive_growth · risk=high) → 국채 낮은 쪽(`0/25/40`).
  - **중립** → `0/25/40/50`; 인상기엔 50 절제.
- 각 후보 필드: `label`(A/B/C/D), `govbond_ratio_pct`(방어 대비), `suggested_split{short,long}`, `total_breakdown`(전체환산: 순현금/단기국채/장기국채/위험 — carve 정합 `compute_breakdown` 재사용), `rationale`(왜 이 비중), `suited_when`, `rising_rate_risk`, `falling_rate_benefit`, `fx_risk`(국내=환위험0, 미국채=환위험 추가), `liquidity`, `account_fit`(계좌 목적 부합도), `confidence`, `system_recommended`.
- **거시 인지**: 인상기/고금리 → 어떤 후보든 장기 절제(long ≤ 20%); 인하기대/하락 → 장기 비중↑(자본이득). macro 실데이터 우선, 없으면 사용자 금리뷰, 둘 다 없으면 `unknown`(일반 기준).
- **system_recommended**: `rate_regime` 기준선(`_REGIME_BOND_RATIO`)에 가장 가까운 후보 1개 강조. `unknown` 이면 강조 없음(가짜 단정 0).
- **confidence**: 데이터 충실도 — macro 연동 0.75(uncertain 0.6) > 사용자 견해 0.5 > unknown 0.25. 계좌 목적 설정 시 +0.1.
- **장기국채 변동성 경고(불변)**: 최상위 `long_bond_volatility_warning` + 장기 split 있는 후보에 개별 경고 — *"금리 하락 수혜 but 가격 변동 큼 — 안전자산 단정 금지"*.

```
python -m main_mission.portfolio_os.bond_recommendation --account 1 --options
python -m main_mission.portfolio_os.bond_recommendation --account 1          # 단일 recommend
python -m main_mission.portfolio_os.bond_recommendation --account 1 --regime-only
```
