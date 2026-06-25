# Portfolio OS — 에이전트 (투자 운영 조직)

> 콘텐츠 자동화 조직이 아니라 **투자 운영 조직**. 국장/미장/글로벌장이 분리됨.
> 작업 시작 시 자신의 role + task 매핑을 먼저 확인(Role Persistence).
> 상위: [../../docs/portfolio/roles.md](../../docs/portfolio/roles.md)

```text
[1]   CEO 사용자                  최종 투자 판단·승인
[2]   strategy-executive          CEO 창구·우선순위·CEO-GATE·보고 취합
[2.5] portfolio-strategy-chief    전체 전략·anchor/tilt/현금/헷지·시장 chief 통합
[3]   chiefs
        korea-market-chief        한국장 (코스피/코스닥·국내 ETF)
        us-market-chief           미국장 (S&P/Nasdaq·반도체·미국 ETF)
        global-market-chief       글로벌 (일/중/유럽·원자재·환율·지정학)
        research-chief            자료 조사·근거·ETF 분석 (provenance)
        portfolio-chief           목표비중·drift·리밸런싱 계산
        risk-chief                현금/숏/레버리지/drawdown/블랙아웃 (hard-block)
        broker-chief              KIS API·주문/체결 (paper/live)
        data-ops-chief            DB·스냅샷·audit·order_events·backtest
        memory-lesson-chief       prehook/posthook·lesson-run·knowhow
        code-architect-chief      코드 구조·리팩토링·테스트·경계 분리
[4]   analysts                    sector / ETF / macro / risk / execution / evidence
```

| 역할 | 파일 |
|---|---|
| strategy-executive | [strategy-executive.md](strategy-executive.md) |
| portfolio-strategy-chief | [portfolio-strategy-chief.md](portfolio-strategy-chief.md) |
| korea-market-chief | [korea-market-chief.md](korea-market-chief.md) |
| us-market-chief | [us-market-chief.md](us-market-chief.md) |
| global-market-chief | [global-market-chief.md](global-market-chief.md) |
| research-chief | [research-chief.md](research-chief.md) |
| portfolio-chief | [portfolio-chief.md](portfolio-chief.md) |
| risk-chief | [risk-chief.md](risk-chief.md) |
| broker-chief | [broker-chief.md](broker-chief.md) |
| data-ops-chief | [data-ops-chief.md](data-ops-chief.md) |
| memory-lesson-chief | [memory-lesson-chief.md](memory-lesson-chief.md) |
| code-architect-chief | [code-architect-chief.md](code-architect-chief.md) |
| analysts | [analysts/](analysts/) |

모델: 전략/리스크 판단 opus · 시장분석/조사/계산 sonnet · 분류/로그 haiku.
