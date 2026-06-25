# Portfolio OS — MVP 구현 순서

> 완료 기준(§9)은 "코드 몇 개"가 아니라 **루프가 돈다**는 것:
> 컨셉 → 목표 비중 → 현재 비교 → 근거 있는 제안 → 리스크 가드 → 승인 후보 → 추적.
> 아래는 그 루프를 **안전하게** 세우는 순서. 각 단계는 이전 단계 검증 후 진행.

---

## Phase 0 — 준비 (현재 단계, 이 보고서)
- [x] 아키텍처 / 역할 / task tree / DB 스키마 / API adapter / hook 설계 문서화
- [x] 에이전트 personas (agents/portfolio/)
- [x] DB 마이그레이션 001 초안 (미적용 DRAFT)
- [x] config: risk_limits.yaml + secrets.example.env
- [x] broker 인터페이스 + MockAdapter 스켈레톤
- **게이트**: CEO 가 아키텍처 승인 → Phase 1 착수

## Phase 1 — 기반 (조회 only, 주문 없음)
1. `portfolio_os_db` 생성 + migration 001 적용
2. `.env` 에 KIS 모의투자 app_key/secret/계좌 (CEO 제공)
3. `KisHttpClient`: 토큰 발급/캐시 + rate limit + 로그 마스킹
4. `KisPaperAdapter.get_balance / get_quote / get_fx_rate` 구현
5. **T1 잔고 조회 → DB balances 저장** (모의 계좌)
- **검증**: 모의 계좌 잔고가 DB 에 정확히, 통화/KRW 환산 맞게 들어감

## Phase 2 — 분류 + 통합 평가
6. T2 현재가/환율, T3 자산군 분류 (국내+미국 KRW 통합)
7. `portfolio_snapshots` 첫 스냅샷
- **검증**: cash/long/short/sector 합 = 100%, 환율 반영 확인

## Phase 3 — 목표 비중 + drift (순수 모듈, API 불필요)
8. T4 컨셉 파서: 자연어 → target_weights (LLM + 검증, 합 100%)
9. T5 drift_compute (현재 vs 목표)
- **검증**: "반도체 과열, 현금 30%, 숏 보험 수준" → 목표 비중 JSON + drift 표

## Phase 4 — 제안 + 리스크 가드 (핵심 안전)
10. T6 rebalance_propose (band 초과만 거래 + 근거)
11. T7 risk_check 게이트 (safety_rules B/C/D 전부)
- **검증(필수)**: 한도 위반 시나리오를 **단위 테스트**로 — 현금<10%, 단일종목>20%, 숏 초과 → 전부 hard-block

## Phase 5 — 승인형 주문 (paper)
12. T8 CEO 승인 UI (web admin) → approvals
13. T9 order_execute (paper) + client_order_id idempotency
14. T10 fill_confirm, T11 record
- **검증**: 모의 주문이 승인 후에만, 중복 전송 없이, audit_log 에 전부 남음

## Phase 6 — 학습 루프 + 웹 관리
15. T12 reflect_lesson + prehook/posthook 인출-기록 연결
16. Web admin: 컨셉 입력 / 제안 검토 / 승인 / 성과 대시보드
17. memory-chief lesson 승격 운영
- **검증**: 다음 사이클 prehook 이 지난 lesson 을 실제로 인출

## Phase 7 — live 승격 (CEO 승인 필수, MVP 범위 밖)
- 모의에서 N회 무사고 + 리스크 게이트 회귀 통과 → CEO 체크리스트 → `KIS_MODE=live`

---

## MVP 완료선 (이번 목표)
**Phase 1~5 + Phase 6 의 컨셉입력·제안·승인 UI**.
즉, *모의투자 계좌로 컨셉을 넣으면 근거 있는 리밸런싱 제안이 나오고, 리스크 가드를 통과한 것만 내가 승인해서 모의 주문이 나가고, 전부 추적되는* 상태.

연결: [architecture.md](architecture.md) · [task_tree.md](task_tree.md) · [safety_rules.md](safety_rules.md)
