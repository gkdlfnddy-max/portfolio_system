"""SQLite -> PostgreSQL ETL **dry-run 검증 스크립트 (READ-ONLY).**

Track 4 — PG 정렬 ETL 설계의 자증 도구.

⚠️ 본 스크립트는 **읽기 전용**이다. SQLite/PG 양쪽 모두에 대해 오직 `SELECT`
(및 카탈로그 조회)만 수행한다. **INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/TRUNCATE
/RENAME 을 일절 포함하지 않는다.** (검증: 본 파일 grep 으로 해당 키워드 부재 확인)

산출:
  1. SQLite vs PG row count 비교 (ETL 후보 테이블)
  2. account_index -> accounts.id 매칭률 (고아 account_index 탐지)
  3. ETL 대상 행수 (SQLite 전용 운영 데이터)
  4. decisions payload(JSON) 파싱 가능률 + portfolio_decisions 정규화 추출 가능률
  5. 충돌/고아 후보 카운트

보안:
  - DATABASE_URL / 비밀번호를 절대 출력하지 않는다 (backend.require_database_url 만 사용, 값 미표시).
  - PG 미접속/psycopg2 미설치 시에도 SQLite 단독 섹션은 동작한다.

실행:
  python -m main_mission.portfolio_os.store.etl_dryrun
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Optional

from . import backend
from .db import db_path

try:  # PG 는 선택적 (없으면 SQLite 단독 리포트)
    import psycopg2
    _PG_DRIVER = True
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore
    _PG_DRIVER = False

PG_SCHEMA = "portfolio"

# --- ETL 후보 매핑 (SQLite 테이블 -> PG canonical 테이블) -----------------
# (sqlite_table, pg_table_or_None, note)
#   pg_table=None  => PG 에 대응 테이블 미존재 (신설 필요)
TABLE_MAP: list[tuple[str, Optional[str], str]] = [
    # 동일 이름
    ("target_allocations", "target_allocations", "동일 이름, account_index->account_id"),
    ("account_snapshots", "account_snapshots", "동일 이름, account_index->account_id"),
    ("portfolio_policies", "portfolio_policies", "동일 이름"),
    ("rebalance_plans", "rebalance_plans", "동일 이름"),
    ("rebalance_plan_steps", "rebalance_plan_steps", "동일 이름"),
    ("scheduled_order_plans", "scheduled_order_plans", "동일 이름"),
    ("scheduled_order_steps", "scheduled_order_steps", "동일 이름"),
    ("lessons", "lessons", "동일 이름"),
    ("lesson_candidates", "lesson_candidates", "동일 이름"),
    ("agent_memories", "agent_memories", "동일 이름"),
    ("evidence_documents", "evidence_documents", "동일 이름"),
    ("consultations", "consultations", "동일 이름"),
    ("field_consultations", "field_consultations", "동일 이름"),
    ("field_advice_events", "field_advice_events", "동일 이름"),
    ("daily_portfolio_reviews", "daily_portfolio_reviews", "동일 이름"),
    ("market_context_snapshots", "market_context_snapshots", "동일 이름"),
    # rename (개념 동일, 이름/구조 차이) — R1/R2 위험 구간
    ("investor_profile", "investor_profiles", "단->복수, doc->doc_json, PK 구조차"),
    ("investor_profile_history", "investor_profile_versions", "snapshot->snapshot_json"),
    ("allocation_selections", "selected_allocations", "*_json 캐스팅, policy_version->policy_version_id"),
    ("decisions", "portfolio_decisions", "payload 통짜 -> 정규화 ETL (무손실 변환 로직)"),
    ("task_memory_links", "task_memories", "FK bigint, account_id 보강"),
    # SQLite 전용 (PG 미존재) — 신설 필요
    ("tasks", None, "PG 미존재 -> 신설 필요"),
    ("advice_items", None, "PG 미존재 -> 신설 필요"),
    ("agent_memory_scope", None, "PG 미존재 -> 신설 필요"),
    ("analysis_requests", None, "PG 미존재 -> 신설 필요"),
    ("growth_reports", None, "PG 미존재 -> 신설 필요"),
    ("task_failure_patterns", None, "PG 미존재 -> 신설 필요"),
    ("task_regression_tests", None, "PG 미존재 -> 신설 필요"),
    ("universe_instruments", None, "PG 미존재 -> 신설 필요 (소전제 유니버스)"),
    ("sync_events", None, "PG 미존재 -> 신설 필요 (freshness 근거)"),
    ("audit_logs", None, "PG 미존재 -> 신설 필수 (CLAUDE.md 13조)"),
]

# decisions.payload 에서 portfolio_decisions 정규화 컬럼으로 추출할 키
DECISION_NORMALIZE_KEYS = {
    "drift_pct": ("cash_current_pct", "cash_target_pct"),  # |current-target| 로 산출(설계 규칙)
    "passed": "ok",
    "risk_reasons": "risk",
}


def _fmt(n) -> str:
    return f"{n:>8}" if isinstance(n, int) else f"{str(n):>8}"


# --- SQLite 읽기 헬퍼 (SELECT only) --------------------------------------

def _sqlite_conn() -> sqlite3.Connection:
    p = db_path()
    # uri read-only 모드 — 쓰기 자체를 OS 레벨에서 차단
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_count(conn: sqlite3.Connection, table: str) -> Optional[int]:
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    except sqlite3.OperationalError:
        return None  # 테이블 없음


def _sqlite_has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        return col in cols
    except sqlite3.OperationalError:
        return False


# --- PG 읽기 헬퍼 (SELECT only) ------------------------------------------

def _pg_connect():
    if not _PG_DRIVER or not backend.is_postgres():
        return None
    try:
        url = backend.require_database_url()  # 값 미표시
    except Exception:
        return None
    try:
        # default_transaction_read_only=on — 세션 자체를 읽기전용으로 강제
        conn = psycopg2.connect(
            url,
            options=f"-c search_path={PG_SCHEMA} -c default_transaction_read_only=on",
        )
        return conn
    except Exception as e:  # URL 미노출
        print(f"  [PG 접속 실패: {type(e).__name__} — URL 미표시]")
        return None


def _pg_count(conn, table: str) -> Optional[int]:
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {PG_SCHEMA}."{table}"')
            return cur.fetchone()[0]
    except Exception:
        conn.rollback()
        return None


def _pg_account_index_to_id(conn) -> dict[int, int]:
    out: dict[int, int] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT account_index, id FROM {PG_SCHEMA}.accounts")
            for idx, _id in cur.fetchall():
                out[idx] = _id
    except Exception:
        conn.rollback()
    return out


# --- 리포트 섹션 ---------------------------------------------------------

def section_rowcounts(scon, pcon) -> None:
    print("\n=== [1] Row count 비교 (SQLite -> PG canonical) ===")
    print(f"{'SQLite table':28} {'PG table':26} {'SQLite':>8} {'PG':>8}  note")
    print("-" * 110)
    for s_tab, p_tab, note in TABLE_MAP:
        s_n = _sqlite_count(scon, s_tab)
        p_n = _pg_count(pcon, p_tab) if (pcon and p_tab) else ("-" if p_tab is None else "n/a")
        print(f"{s_tab:28} {str(p_tab or '(신설필요)'):26} {_fmt(s_n if s_n is not None else 'MISS')} {_fmt(p_n)}  {note}")


def section_account_match(scon, pcon) -> None:
    print("\n=== [2] account_index -> accounts.id 매칭률 (고아 FK 탐지) ===")
    # SQLite accounts
    s_idx = sorted(r[0] for r in scon.execute("SELECT account_index FROM accounts"))
    print(f"SQLite accounts account_index : {s_idx}")
    if pcon:
        amap = _pg_account_index_to_id(pcon)
        print(f"PG accounts account_index->id : {amap}")
    else:
        amap = {}
        print("PG accounts account_index->id : (PG 미접속 — 매칭률 산출 불가)")

    # 자식 테이블의 account_index 분포 + 매칭 여부
    child_tables = [
        "target_allocations", "tasks", "field_consultations", "decisions",
        "investor_profile", "investor_profile_history", "allocation_selections",
        "rebalance_plans", "account_snapshots", "portfolio_policies",
    ]
    print(f"\n{'child table':28} {'distinct account_index':32} {'matched':>9} {'orphan':>8}")
    print("-" * 90)
    total_rows = 0
    orphan_rows = 0
    for t in child_tables:
        if not _sqlite_has_col(scon, t, "account_index"):
            continue
        rows = scon.execute(
            f'SELECT account_index, COUNT(*) FROM "{t}" GROUP BY account_index'
        ).fetchall()
        distinct = [r[0] for r in rows]
        if not amap:  # PG 미접속 — 매칭 판단 보류
            print(f"{t:28} {str(distinct):32} {'(no PG)':>9} {'(no PG)':>8}")
            continue
        matched_cnt = 0
        orphan_cnt = 0
        orphan_idx = []
        for idx, cnt in rows:
            total_rows += cnt
            if idx in amap:
                matched_cnt += cnt
            else:
                orphan_cnt += cnt
                orphan_rows += cnt
                orphan_idx.append(idx)
        flag = f"  <- 고아 account_index={orphan_idx}" if orphan_idx else ""
        print(f"{t:28} {str(distinct):32} {matched_cnt:>9} {orphan_cnt:>8}{flag}")

    if amap:
        rate = (1 - orphan_rows / total_rows) * 100 if total_rows else 100.0
        print(f"\n총 자식행={total_rows}  매칭={total_rows - orphan_rows}  고아={orphan_rows}  "
              f"=> account_index 매칭률 = {rate:.1f}%")
        if orphan_rows:
            print("  ⚠️ 고아 행 존재 — ETL 시 abort 또는 격리 대상 (부분 적재 금지, R3).")


def section_decisions_etl(scon, pcon) -> None:
    print("\n=== [3] decisions -> portfolio_decisions 정규화 ETL 검증 ===")
    rows = scon.execute("SELECT id, account_index, payload, created_at FROM decisions").fetchall()
    total = len(rows)
    parse_ok = 0
    norm_ok = 0
    drift_extractable = 0
    bad_ids = []
    for r in rows:
        try:
            d = json.loads(r["payload"])
        except Exception:
            bad_ids.append(r["id"])
            continue
        parse_ok += 1
        # 정규화 추출 규칙 검증 (실제 write 아님 — 가능 여부만 평가)
        has_passed = "ok" in d
        has_risk = "risk" in d
        cur_pct = d.get("cash_current_pct")
        tgt_pct = d.get("cash_target_pct")
        if cur_pct is not None and tgt_pct is not None:
            drift_extractable += 1
        if has_passed and has_risk:
            norm_ok += 1
    print(f"  decisions 총 {total}행")
    print(f"  payload JSON 파싱 성공 : {parse_ok}/{total}"
          + (f"  (실패 id={bad_ids})" if bad_ids else "  (실패 0)"))
    print(f"  passed+risk 정규화 추출 가능 : {norm_ok}/{total}")
    print(f"  drift_pct 산출 가능(cash_current/target 존재) : {drift_extractable}/{total}")
    print("  정규화 매핑 규칙:")
    print("    payload(통짜)            -> payload_json (jsonb, 무손실 보존)")
    print("    abs(cash_current_pct-cash_target_pct) -> drift_pct (없으면 NULL)")
    print("    payload.ok               -> passed (bool)")
    print("    payload.risk             -> risk_reasons_json (jsonb)")
    print("    account_index            -> account_id (accounts.id FK)")
    print("    created_at(TEXT ISO)     -> created_at (timestamptz)")
    print("    selected_allocation_id/account_snapshot_id : ETL 시 매핑테이블로 재배선(없으면 NULL)")


def section_etl_targets(scon, pcon) -> None:
    print("\n=== [4] ETL 대상 행수 합계 (SQLite 전용 + 이관 대상) ===")
    create_needed = 0
    migrate_rows = 0
    for s_tab, p_tab, _ in TABLE_MAP:
        n = _sqlite_count(scon, s_tab) or 0
        migrate_rows += n
        if p_tab is None and n > 0:
            create_needed += 1
    print(f"  ETL 대상 SQLite 총 행수(후보 테이블) : {migrate_rows}")
    print(f"  PG 신설 필요(행수>0) 테이블 수        : {create_needed}")


def main() -> int:
    print("=" * 110)
    print("SQLite -> PostgreSQL ETL DRY-RUN (READ-ONLY · write 0 · cutover 0)")
    print("=" * 110)
    print(f"DB_BACKEND = {backend.current_backend()}  |  psycopg2 = {_PG_DRIVER}")

    with closing(_sqlite_conn()) as scon:
        pcon = _pg_connect()
        try:
            if pcon is None:
                print("  (PG 미접속 — SQLite 단독 섹션만 출력. 매칭률 PG 필요 항목은 보류)")
            section_rowcounts(scon, pcon)
            section_account_match(scon, pcon)
            section_decisions_etl(scon, pcon)
            section_etl_targets(scon, pcon)
        finally:
            if pcon is not None:
                pcon.close()

    print("\n" + "=" * 110)
    print("DRY-RUN 완료 — DB write 0건 (INSERT/UPDATE/DELETE/DDL 없음). 실제 cutover 는 CEO 승인 후 별도.")
    print("=" * 110)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
