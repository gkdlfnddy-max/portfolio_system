// 서버 전용 — PostgreSQL(운영-truth) **조회 전용** 병렬 모듈 (schema=portfolio).
// portfolioDb.ts(SQLite)를 건드리지 않고 미러한다. 중앙 머지가 백엔드 스위치로 배선한다.
// 쓰기는 Python sync job 만. 웹은 KIS 를 직접 호출하지 않는다.
//
// Dual-truth 규칙: 하나의 운영 view 는 정확히 하나의 백엔드에서만 읽는다.
// PG 와 SQLite 혼합 조회는 진실원천 위반 → 호출측에서 hard-block (assertSingleBackend).
//
// DATABASE_URL(자격증명)은 절대 로그/throw 메시지에 노출하지 않는다.
import { Pool } from "pg";

// freshness 기준은 환경변수(설정), UI 하드코딩 아님. (SQLite 모듈과 동일 기준)
const FRESHNESS_SEC = Number(process.env.SYNC_FRESHNESS_SEC ?? 900);

const SCHEMA = "portfolio";

let _pool: Pool | null = null;

/** PG 경로 활성 여부: DB_BACKEND=postgres 이고 DATABASE_URL 이 존재할 때만 true. */
export function pgEnabled(): boolean {
  const backend = (process.env.DB_BACKEND ?? "sqlite").trim().toLowerCase();
  const isPg = backend === "postgres" || backend === "postgresql" || backend === "pg";
  return isPg && Boolean((process.env.DATABASE_URL ?? "").trim());
}

function pool(): Pool | null {
  if (!pgEnabled()) return null;
  if (_pool) return _pool;
  try {
    _pool = new Pool({
      connectionString: process.env.DATABASE_URL,
      // search_path=portfolio 강제 (public 운영 테이블 0 정책).
      options: `-c search_path=${SCHEMA}`,
      max: 4,
    });
    return _pool;
  } catch {
    // 자격증명/URL 미노출 — 에러 내용은 삼킨다.
    return null;
  }
}

async function query<T = any>(sql: string, params: any[] = []): Promise<T[]> {
  const p = pool();
  if (!p) return [];
  try {
    const res = await p.query(sql, params);
    return res.rows as T[];
  } catch {
    // 조회 실패 시 빈 배열 (SQLite 모듈과 동일 정책). URL 미노출.
    return [];
  }
}

// --- 타입 (portfolioDb.ts 와 형태 일치, account_id 매핑 포함) ----------------

export type PgAccountRow = {
  id: number;
  account_index: number;
  alias: string | null;
  mode: string | null;
  account_no_masked: string | null;
  has_credentials: boolean;
  token_status: string | null;
  sync_status: string | null;
  last_error: string | null;
  last_synced_at: string | null;
};

export type PgSnapshot = {
  id: number;
  account_id: number;
  cash_krw: number | null;
  total_value_krw: number | null;
  holdings_count: number | null;
  source: string | null;
  captured_at: string;
};

export type PgPosition = {
  ticker: string;
  name: string | null;
  qty: number;
  avg_price: number;
  market_value: number;
  currency: string;
};

export type PgSelectionRow = {
  id: number;
  variant: string | null;
  allocation_json: any;
  policy_version_id: number | null;
  account_snapshot_id: number | null;
  expected_drift_pct: number | null;
  expected_rebalance_total_krw: number | null;
  expected_rebalance_rounds: number | null;
  precheck_status: string | null;
  precheck_reasons_json: any;
  selected_by: string | null;
  status: string;
  selected_at: string;
};

export type PgAccountView = PgAccountRow & {
  snapshot: PgSnapshot | null;
  holdings: PgPosition[];
  steps: { key: string; label: string; desc: string; done: boolean }[];
  progress: number;
  isFresh: boolean;
};

// --- 읽기 헬퍼 (SQLite 미러) ------------------------------------------------

/** account_index → accounts.id (account_id) 매핑. */
export async function getAccountId(accountIndex: number): Promise<number | null> {
  const rows = await query<{ id: number }>(
    "SELECT id FROM accounts WHERE account_index=$1",
    [accountIndex],
  );
  return rows[0]?.id ?? null;
}

export async function getAccounts(): Promise<PgAccountRow[]> {
  return query<PgAccountRow>("SELECT * FROM accounts ORDER BY account_index");
}

/** account_index → broker(최신 스냅샷 source prefix). 없으면 맵에 미포함(호출측이 'kis' 기본). */
export async function brokerMap(): Promise<Map<number, string>> {
  const rows = await query<{ account_index: number; source: string | null }>(
    "SELECT a.account_index AS account_index, s.source AS source FROM account_snapshots s " +
      "JOIN accounts a ON a.id = s.account_id " +
      "JOIN (SELECT account_id, MAX(id) AS mid FROM account_snapshots GROUP BY account_id) m " +
      "ON s.id = m.mid",
  );
  const map = new Map<number, string>();
  for (const r of rows) {
    const head = String(r.source ?? "").split("_")[0]?.trim().toLowerCase();
    map.set(r.account_index, head === "kiwoom" ? "kiwoom" : "kis");
  }
  return map;
}

/** 최신 account_snapshot (account_index 기준). */
export async function getLatestSnapshot(accountIndex: number): Promise<PgSnapshot | null> {
  const rows = await query<PgSnapshot>(
    "SELECT s.id, s.account_id, s.cash_krw, s.total_value_krw, s.holdings_count, s.source, s.captured_at " +
      "FROM account_snapshots s JOIN accounts a ON a.id=s.account_id " +
      "WHERE a.account_index=$1 ORDER BY s.id DESC LIMIT 1",
    [accountIndex],
  );
  return rows[0] ?? null;
}

/** 스냅샷의 보유종목 (position_snapshots). */
export async function getPositions(snapshotId: number): Promise<PgPosition[]> {
  return query<PgPosition>(
    "SELECT ticker, name, qty, avg_price, market_value, currency " +
      "FROM position_snapshots WHERE account_snapshot_id=$1 ORDER BY id",
    [snapshotId],
  );
}

/** 현재 활성 선택된 자산배분 (selected_allocations). */
export async function getCurrentSelection(accountIndex: number): Promise<PgSelectionRow | null> {
  const rows = await query<PgSelectionRow>(
    "SELECT sa.id, sa.variant, sa.allocation_json, sa.policy_version_id, sa.account_snapshot_id, " +
      "sa.expected_drift_pct, sa.expected_rebalance_total_krw, sa.expected_rebalance_rounds, " +
      "sa.precheck_status, sa.precheck_reasons_json, sa.selected_by, sa.status, sa.selected_at " +
      "FROM selected_allocations sa JOIN accounts a ON a.id=sa.account_id " +
      "WHERE a.account_index=$1 AND sa.status='active' ORDER BY sa.id DESC LIMIT 1",
    [accountIndex],
  );
  return rows[0] ?? null;
}

function freshnessSeconds(iso: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (Date.now() - t) / 1000;
}

/** 계좌 상세 view (SQLite getAccountView 미러). 진행도는 DB 상태 기반 계산. */
export async function getAccountView(accountIndex: number): Promise<PgAccountView | null> {
  const accs = await query<PgAccountRow>("SELECT * FROM accounts WHERE account_index=$1", [
    accountIndex,
  ]);
  const acc = accs[0];
  if (!acc) return null;

  const snapshot = await getLatestSnapshot(accountIndex);
  const holdings = snapshot ? await getPositions(snapshot.id) : [];

  const ageSec = freshnessSeconds(acc.last_synced_at);
  const isFresh = ageSec !== null && ageSec <= FRESHNESS_SEC;

  const credentials = acc.has_credentials === true;
  const tokenOk = acc.token_status === "ok";
  const balanceSynced = snapshot !== null && acc.sync_status === "ok";
  const ready = credentials && tokenOk && balanceSynced && isFresh;
  const steps = [
    { key: "credentials", label: "자격증명 저장", desc: ".env 에 APP Key/Secret 저장", done: credentials },
    { key: "token", label: "KIS 토큰 검증", desc: "OAuth 토큰 발급 성공", done: tokenOk },
    { key: "balance", label: "잔고 동기화", desc: "보유 종목·예수금 DB 저장", done: balanceSynced },
    { key: "ready", label: "관리 준비 완료", desc: "최신 스냅샷 + 리밸런싱 제안 가능", done: ready },
  ];
  const progress = Math.round((steps.filter((s) => s.done).length / steps.length) * 100);

  return { ...acc, snapshot, holdings, steps, progress, isFresh };
}

// --- Dual-truth 가드 -------------------------------------------------------
// 한 운영 view 가 두 백엔드를 동시에 참조하면 진실원천 위반 → hard-block.

export class DualTruthError extends Error {}

export function assertSingleBackend(usedBackends: Set<string>): "sqlite" | "postgres" {
  const clean = [...usedBackends].filter((b) => b === "sqlite" || b === "postgres");
  if (clean.length !== 1) {
    throw new DualTruthError(
      `운영 view 는 단일 백엔드만 허용 — 발견: [${clean.join(", ") || "none"}]. PG+SQLite 혼합 조회 금지.`,
    );
  }
  return clean[0] as "sqlite" | "postgres";
}
