# Portfolio OS — 역할 구조 (투자 운영 조직)

> Portfolio OS 는 콘텐츠 자동화 조직이 아니라 **투자 운영 조직**이다.
> 국장/미장/글로벌장 분석이 분리되고, 전략 판단(strategy)·계산(portfolio)·리스크(risk)·실행(broker)이 각각 다른 책임자.
> 각 role 은 [../../agents/portfolio/](../../agents/portfolio/) persona 로 세션 내내 고정(Role Persistence).

---

## 조직 계층

```text
[1]   CEO 사용자                  최종 투자 판단자 · 투자 컨셉·위험선호·승인 결정
[2]   strategy-executive          CEO 창구 · 전체 방향·우선순위·CEO-GATE · 보고 취합
[2.5] portfolio-strategy-chief    전체 포트폴리오 전략 조율 · anchor/tilt/현금/헷지 · 시장 chief 통합
[3]   chiefs
        1 korea-market-chief      한국장 (코스피/코스닥·국내 ETF·삼성/하이닉스/2차전지/바이오·수급·환율·공매도)
        2 us-market-chief         미국장 (S&P/Nasdaq·반도체·빅테크·미국 ETF·금리/달러/실적·AI/HBM)
        3 global-market-chief     글로벌 (일/중/유럽/신흥국·원자재·달러·금리·지정학·환율)
        4 research-chief          자료 조사·근거 (source/as_of/confidence)·ETF 구성/수수료/추적오차
        5 portfolio-chief         목표비중·현재비중·drift·리밸런싱 금액·3안 (anchor+tilt·5/25·현금흐름 우선)
        6 risk-chief              현금/단일종목/숏/레버리지/drawdown/블랙아웃·주문차단 (유일 hard-block)
        7 broker-chief            KIS API (paper/live·잔고·주문·체결·미체결·토큰·rate limit·idempotency)
        8 data-ops-chief          DB·스냅샷·audit log·order_events·portfolio history·backtest
        9 memory-lesson-chief     prehook/posthook·lesson-run·knowhow·장기 메모리
        10 code-architect-chief   코드 구조·리팩토링·테스트·모듈 경계 분리
[4]   analysts                    chief 하위 sector / ETF / macro / risk / execution / evidence analyst
```

---

## 책임 분리 (완료 기준 직결)

| 완료 기준 | 충족 역할 |
|---|---|
| 국장/미장/글로벌장 분석 분리 | **korea / us / global -market-chief** (3개 독립 chief) |
| 컨셉→목표비중 변환 책임 명확 | **portfolio-strategy-chief**(전략·tilt) → **portfolio-chief**(수치 계산) |
| 계산 전문가 ↔ 리스크 전문가 분리 | **portfolio-chief**(순수 계산) vs **risk-chief**(hard-block) |
| broker/API 실행 ↔ 전략 판단 분리 | **broker-chief**(실행만) vs 전략/시장 chief(판단) |

---

## 권한 매트릭스 (안전 핵심)

| 권한 | 보유 역할 |
|---|---|
| KIS 자격증명 접근 | **broker-chief 만** |
| 주문 hard-block | **risk-chief 만** |
| anchor/tilt 정책 확정 | portfolio-strategy-chief 제안 → **CEO 승인** |
| 리스크 한도 변경 | risk-chief 검증 → **CEO 승인** |
| live 모드 전환 | broker-chief 제안 → **CEO 승인 + 체크리스트** |
| 실주문 실행 | **CEO 승인(T11) 후에만** |
| 메모리 삭제 | memory-lesson-chief 제안 → **CEO 승인** |

---

## CEO → 에이전트 라우팅

| 요청 유형 | 담당 chief |
|---|---|
| 전체 투자 방향·우선순위·결재 | strategy-executive |
| 포트폴리오 전략·anchor·tilt | portfolio-strategy-chief |
| 한국 주식·국내 ETF·코스피/코스닥 | korea-market-chief |
| 미국 주식·미국 ETF·나스닥/반도체 | us-market-chief |
| 글로벌 매크로·환율·일본/중국/유럽 | global-market-chief |
| 자료 조사·근거 수집·ETF 분석 | research-chief |
| 목표 비중·drift·리밸런싱 계산 | portfolio-chief |
| 숏/레버리지/현금/drawdown 리스크 | risk-chief |
| 한국투자증권 API·주문·체결 | broker-chief |
| DB·스냅샷·감사 로그·백테스트 | data-ops-chief |
| lesson-run·memory·prehook/posthook | memory-lesson-chief |
| 코드 구조·리팩토링·테스트 | code-architect-chief |

---

## 의사결정 흐름에서의 역할 (task 매핑 요약)

```text
CEO 컨셉 → strategy-executive(라우팅)
  → korea/us/global-market-chief (T5 시장 의견) + research-chief (T1 근거)
  → portfolio-strategy-chief (anchor+tilt 통합)
  → portfolio-chief (T6 목표비중 3안 · T7 drift · T8 제안)
  → risk-chief (T9 게이트)
  → strategy-executive (CEO-GATE 분류·보고)
  → CEO (T11 승인)
  → broker-chief (T12 paper 실행 · T13 체결) — CEO 승인 후에만
  → data-ops-chief (T14 기록) · memory-lesson-chief (T15/16 lesson·knowhow)
```

세부: [task_tree.md](task_tree.md) · [hook_design.md](hook_design.md) · [architecture.md](architecture.md)
