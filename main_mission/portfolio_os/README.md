# Portfolio OS — 한국투자증권 기반 승인형 포트폴리오 운영 시스템

> 본 시스템의 코어 미션 폴더.
> 자동매매 봇이 아니라, CEO 가 **투자 컨셉**을 제시하면 시스템이 근거를 수집하고
> **목표 비중 · 현금 비중 · 헷지 비중**을 계산하여 **승인형 리밸런싱**을 돕는 장기 운영 OS.
>
> 핵심 원칙: **추천 → 검토 → CEO 승인 → 주문**. 완전 자동매매는 처음부터 금지.

---

## 0. 정체성

- **사용자 = CEO / 최종 의사결정자**. 모든 주문은 CEO 승인 후에만 실행된다.
- **브로커**: 한국투자증권(KIS) Open API. 국내주식 · 국내 ETF · 미국주식 · 미국 ETF · 현금 · 환율.
- **모드**: `paper`(모의투자) 우선 → 검증 후에만 `live`(실전) 승격.
- **본질**: 자동 발행이 아니라 *지속 가능한 포트폴리오 운영 시스템* (계획 · 행동 · 현금관리 · 리스크가드 · 감사로그 · lesson loop).
- **언어**: 코어 문서·메모리·보고서는 한글, 코드·변수·DB 컬럼은 영어.

---

## 1. 의사결정 루프 (불변)

```text
투자 컨셉 입력
  → 근거 수집(research)
  → 목표 비중 변환(target weights)
  → 현재 비중 조회(balance snapshot)
  → 차이 계산(drift)
  → 리밸런싱 제안(rebalance proposal + 근거)
  → 주문 전 리스크 가드(risk guard)
  → CEO 승인(approval)
  → 주문 후보 생성 → (paper/live) 주문
  → 체결 확인(fill)
  → 성과 기록(snapshot/metrics)
  → 회고/lesson 등록
```

세부: [../../docs/portfolio/architecture.md](../../docs/portfolio/architecture.md)

---

## 2. 레이어 구조

| 레이어 | 폴더 | 책임 |
|---|---|---|
| L1 Strategy | [strategy/](strategy/) | 투자 컨셉 → 자산배분 원칙 → 리스크 한도 정의 |
| L2 Research | [research/](research/) | 시장 근거 · 섹터 흐름 · ETF/종목 후보 |
| L3 Portfolio | [portfolio/](portfolio/) | 목표 비중 · 현재 비중 · drift · 리밸런싱 계산 |
| L4 Risk | [risk/](risk/) | 현금 · 숏/레버리지 · 단일 종목 · 손실 한도 게이트 |
| L5 Broker | [broker/](broker/) | KIS API adapter · 주문/잔고/체결 · paper/live |
| L6 Data-Ops | [data_ops/](data_ops/) | DB SSOT · 거래 로그 · 스냅샷 · 성과 |

---

## 3. 절대 안전 원칙 (SSOT)

[../../docs/portfolio/safety_rules.md](../../docs/portfolio/safety_rules.md) 에 정의. 요약:

1. 완전 자동매매 금지 (기본 = 추천/승인형)
2. 현금 최소 비중 유지
3. 인버스/레버리지/숏 ETF 총합 한도
4. 단일 종목 최대 비중 한도
5. 장 시작 직후 / 장 마감 직전 주문 제한
6. 미체결/부분체결 처리
7. 환율 반영 필수
8. API 장애 시 주문 중단
9. 동일 주문 중복 실행 방지 (idempotency)
10. 모든 주문은 DB + 로그에 추적 가능

---

## 4. 데이터베이스

- DB: 로컬 SQLite (`data/portfolio.sqlite3`) — 추후 `portfolio_os_db` (PostgreSQL 15+) 승격 가능
- 마이그레이션: [migrations/](migrations/)
- 스키마: [../../docs/portfolio/db_schema.md](../../docs/portfolio/db_schema.md)
- 비밀(API key/계좌/토큰)은 `.env` (예시 [../../config/portfolio/secrets.example.env](../../config/portfolio/secrets.example.env)). **코드 하드코딩 금지.**

---

## 5. 빠른 참조

| 분류 | 경로 |
|---|---|
| 아키텍처 | [../../docs/portfolio/architecture.md](../../docs/portfolio/architecture.md) |
| 역할 정의 | [../../docs/portfolio/roles.md](../../docs/portfolio/roles.md) |
| Task Tree | [../../docs/portfolio/task_tree.md](../../docs/portfolio/task_tree.md) |
| DB 스키마 | [../../docs/portfolio/db_schema.md](../../docs/portfolio/db_schema.md) |
| API Adapter | [../../docs/portfolio/api_adapter.md](../../docs/portfolio/api_adapter.md) |
| Prehook/Posthook | [../../docs/portfolio/hook_design.md](../../docs/portfolio/hook_design.md) |
| 안전 원칙 | [../../docs/portfolio/safety_rules.md](../../docs/portfolio/safety_rules.md) |
| MVP 구현 순서 | [../../docs/portfolio/mvp_order.md](../../docs/portfolio/mvp_order.md) |
| Evidence 요약 엔진 | [../../docs/portfolio/evidence_summary.md](../../docs/portfolio/evidence_summary.md) |
| 에이전트 personas | [../../agents/portfolio/README.md](../../agents/portfolio/README.md) |
