# Portfolio OS — 데이터 아키텍처 (운영 truth 중심)

> CEO 원칙 (2026-06-20): **웹은 조회 전용.** 수집·해석·저장은 백엔드/DB. 하드코딩 0.

---

## 1. 최종 데이터 흐름

```text
KIS OpenAPI / 외부 소스(DART·뉴스)
   │  (수집은 백엔드만)
   ▼
Backend Sync Job (Python)            main_mission/portfolio_os/broker/sync_job.py
   │  토큰·잔고·시세를 가져와 저장 (읽기 전용 수집, 주문 없음)
   ▼
운영 truth DB                        로컬: SQLite(data/portfolio.sqlite3) · 승격: PostgreSQL
   │  accounts / account_snapshots / holdings / quotes / sync_events / orders / audit_logs
   ▼
Web API (조회 전용, node:sqlite)     web/lib/server/portfolioDb.ts, web/app/api/accounts/**
   │  KIS 직접 호출 금지. DB 만 SELECT.
   ▼
Web UI (저장된 truth 렌더)           web/app/**, web/components/AccountSync.tsx
```

- **웹의 유일한 쓰기 트리거**: `POST /api/accounts/[id]/sync` → 백엔드 sync job 실행(= DB 갱신). 화면은 그 후 `GET`으로 DB 재조회.
- 웹은 KIS·DART·뉴스를 직접 호출하지 않는다.

---

## 2. 계층별 책임

| 계층 | 책임 | 위치 |
|---|---|---|
| Sync Job | KIS/외부 → DB 저장. freshness·source·오류 기록 | `broker/sync_job.py`, `broker/account_status.py` |
| 운영 truth (RDB) | 금액·잔고·주문·체결 등 **정합성 필요 데이터의 기준** | SQLite(now) / PostgreSQL(later) |
| Web API | DB SELECT only (`node:sqlite`) | `lib/server/portfolioDb.ts` |
| Web UI | DB 응답 렌더. 진행도·CTA·잔고 전부 DB 파생 | `components/AccountSync.tsx` 등 |

---

## 3. RDB = 운영 truth (정합성 기준)

저장 항목(현재 구현 ✅ / 예정 ⏳):
- ✅ 계좌 메타(`accounts`: alias, mode, masked 계좌번호, has_credentials, token_status, sync_status, last_synced_at, last_error)
- ✅ 잔고 스냅샷(`account_snapshots`: cash_krw, total_value_krw, source, captured_at, is_stale)
- ✅ 보유종목(`holdings`)
- ✅ 동기화 이력(`sync_events`: status/stage/error/시각 → freshness)
- ✅ 주문 원장(`orders`: idempotency·상태머신) · 감사로그(`audit_logs`)
- ⏳ 현재가 스냅샷(`quotes`) 적재 · 목표비중 · drift · AI 제안 · 승인/거절 · 체결/취소

> 자격증명(키/시크릿/토큰/평문 계좌번호)은 **DB 에 저장하지 않는다** — `.env` 전용. DB 는 마스킹값만.

---

## 4. Vector DB (의미 검색) — 설계, 로컬 미구현

용도: 뉴스·공시(DART)·리포트·종목 설명·전략 근거·과거 제안 reasoning·승인/거절 사유·리스크 근거 문서의 **임베딩 저장 + 유사도 검색**. AI 제안 시 "왜 이 제안인가"를 근거 문서로 설명.

- **정합성 데이터(금액·잔고·주문·체결)는 절대 Vector 가 아니라 RDB 기준.** Vector 는 근거/설명 보조.
- 로컬 SQLite 에는 pgvector 가 없어 **PostgreSQL 승격 시 구현**(`memory_embeddings` 등 기존 설계 docs/vector_collection_strategy 참조). 그 전까지는 미적재.

## 5. Graph Index (관계) — 설계, 로컬 미구현

용도: `계좌→보유종목→섹터→테마→뉴스→리스크`, `종목→실적→배당→공시→주가이벤트`, `제안→근거→리스크게이트→승인→주문→체결`, 종목 상관/ETF 중복노출/국가·통화·환율 노출. "왜 이 계좌가 이 리스크에 노출되는가" 설명.

- PostgreSQL recursive CTE + `*_links` 테이블 또는 그래프 엔진으로 승격 시 구현. 로컬 미적재.

---

## 6. 하드코딩 정책 (운영 코드 0)

모든 값은 ① DB ② 환경변수 ③ 설정 테이블 ④ 공식 API 응답 ⑤ 검증된 sync 결과 ⑥ (테스트 코드 내) fixture 중 하나에서 온다.

- 진행도 100% 도 DB 상태(자격증명·token_status·snapshot·sync_status·freshness)로 **계산**. 고정 표시 금지.
- freshness 기준은 `SYNC_FRESHNESS_SEC`(env).

### 현재 상태
- ✅ 계좌 경로(랜딩·계좌상세·잔고·진행도): 완전 DB 파생, mock 제거.
- ⏳ `/portfolio` 의사결정 페이지: 아직 `lib/portfolio/mock.ts`(CURRENT_PORTFOLIO/TOTAL_VALUE_KRW) 사용 → **DB 스냅샷 기반으로 전환 예정(다음 단계)**.

---

## 7. 남은 단계

1. `/portfolio` 를 계좌 스냅샷(DB) 기반으로: 총평가액·현금비중·종목비중·drift·제안가능여부를 DB 에서 계산.
2. `quotes`·목표비중·제안·승인·체결 테이블 적재 + 웹 조회.
3. PostgreSQL 승격 → Vector/Graph 실구현.
4. DART/뉴스 sync job → 이벤트·공시·실적 일정 DB 적재. (요약/정리 엔진은 [evidence_summary.md](evidence_summary.md) — 현재 수동 입력 + ingestion stub, 실 커넥터 미연동.)
