// 서버 전용 — 웹 API/페이지가 운영 truth를 *조회만* 한다.
// 쓰기는 Python sync job 만. 웹은 KIS 를 직접 호출하지 않는다.
//
// Track C — 백엔드 스위치: DB_BACKEND=postgres 이면 운영-view 읽기를 pgDb(PG)로 위임,
// 그 외(기본)는 node:sqlite 경로. **한 read = 단일 백엔드** (PG+SQLite 혼합 금지).
//   · 전환 대상(운영 view): getAccounts / getAccountView / getCurrentSelection
//     (+ pgDb 의 getLatestSnapshot / getPositions 는 getAccountView 내부에서 사용)
//   · 비전환(엔진 미러 = SQLite 고정): getProfile / getLatestPolicy / getUniverse /
//     getSelectionHistory / getProfileHistory / getLatestDecision
//     → 이들은 Python compute store(SQLite)의 미러라 PG 로 옮기지 않는다.
import { DatabaseSync } from "node:sqlite";
import path from "path";
import fs from "fs";
import * as pgDb from "./pgDb";

/** PG 경로 활성 여부 (DB_BACKEND=postgres + DATABASE_URL 존재). pgDb 와 동일 기준. */
function usePostgres(): boolean {
  return pgDb.pgEnabled();
}

const DB_PATH = path.resolve(process.cwd(), "..", "data", "portfolio.sqlite3");

// freshness 기준은 환경변수(설정), UI 하드코딩 아님.
const FRESHNESS_SEC = Number(process.env.SYNC_FRESHNESS_SEC ?? 900);

function open(): DatabaseSync | null {
  try {
    if (!fs.existsSync(DB_PATH)) return null;
    return new DatabaseSync(DB_PATH, { readOnly: true });
  } catch {
    return null;
  }
}

function query<T = any>(sql: string, ...params: any[]): T[] {
  const db = open();
  if (!db) return [];
  try {
    return db.prepare(sql).all(...params) as T[];
  } catch {
    return [];
  } finally {
    db.close();
  }
}

export type AccountRow = {
  account_index: number;
  alias: string | null;
  mode: string | null;
  account_no_masked: string | null;
  has_credentials: number;
  token_status: string | null;
  sync_status: string | null;
  last_error: string | null;
  last_synced_at: string | null;
  broker: string; // kis | kiwoom (없으면 'kis' 기본) — 스냅샷 source 에서 유도(스키마 무변경)
};

// 스냅샷 source(예: "kiwoom_paper" | "kis_live")에서 broker prefix 만 추출. 없으면 'kis'.
function brokerFromSource(source: string | null | undefined): string {
  const head = String(source ?? "").split("_")[0]?.trim().toLowerCase();
  return head === "kiwoom" ? "kiwoom" : "kis";
}

// 계좌별 최신 스냅샷 source → broker 맵 (SQLite 직접 조회, 가벼움). schema.sql 무변경.
function brokerMapSqlite(): Map<number, string> {
  const rows = query<{ account_index: number; source: string | null }>(
    "SELECT s.account_index AS account_index, s.source AS source FROM account_snapshots s "
      + "JOIN (SELECT account_index, MAX(id) AS mid FROM account_snapshots GROUP BY account_index) m "
      + "ON s.id = m.mid",
  );
  const map = new Map<number, string>();
  for (const r of rows) map.set(r.account_index, brokerFromSource(r.source));
  return map;
}

export type Holding = {
  ticker: string;
  name: string | null;
  qty: number;
  avg_price: number;
  market_value: number;
  currency: string;
};

export type Snapshot = {
  id: number;
  cash_krw: number | null;
  total_value_krw: number | null;
  holdings_count: number | null;
  source: string | null;
  captured_at: string;
};

export type SyncStep = { key: string; label: string; desc: string; done: boolean };

export type AccountView = AccountRow & {
  snapshot: Snapshot | null;
  holdings: Holding[];
  steps: SyncStep[];
  progress: number;
  isFresh: boolean;
};

export async function getAccounts(): Promise<AccountRow[]> {
  if (usePostgres()) {
    const rows = await pgDb.getAccounts();
    const bmap = await pgDb.brokerMap();
    // PG → SQLite 반환 shape 정규화 (has_credentials boolean→number). broker 유도(없으면 'kis').
    return rows.map((r) => ({
      account_index: r.account_index,
      alias: r.alias,
      mode: r.mode,
      account_no_masked: r.account_no_masked,
      has_credentials: r.has_credentials ? 1 : 0,
      token_status: r.token_status,
      sync_status: r.sync_status,
      last_error: r.last_error,
      last_synced_at: r.last_synced_at,
      broker: bmap.get(r.account_index) ?? "kis",
    }));
  }
  const rows = query<Omit<AccountRow, "broker">>("SELECT * FROM accounts ORDER BY account_index");
  const bmap = brokerMapSqlite();
  return rows.map((r) => ({ ...r, broker: bmap.get(r.account_index) ?? "kis" }));
}

export type UniverseRow = {
  ticker: string;
  market: string;
  name: string | null;
  asset_class: string | null; // 업종(sector)
  currency: string;
  target_weight_pct: number;
  last_price: number | null;
  verified_at: string | null;
};

export type ProfileRow = {
  account_index: number;
  posture_text: string | null;
  risk_tolerance: string | null;
  short_policy: string | null;
  cash_min_pct: number | null;
  cash_max_pct: number | null;
  horizon: string | null;
  interests_text: string | null;
  views_text: string | null;
  individual_cap_pct: number | null;
  individual_count: number | null;
  region_pref: string | null;
  rebalance_pace: string | null;
  region_targets: string | null;     // JSON {지역:비중}
  bond_target_pct: number | null;    // 채권 목표(현금과 별도 방어자산)
  bond_duration_pref: string | null; // short|intermediate|long|mixed
  doc: string | null;
  refined_by: string | null;
  updated_at: string | null;
};

export function getProfile(index: number): ProfileRow | null {
  return query<ProfileRow>("SELECT * FROM investor_profile WHERE account_index=?", index)[0] ?? null;
}

export function getLatestPolicy(index: number): { version: number; policy: any; created_at: string } | null {
  const row = query<{ version: number; policy: string; created_at: string }>(
    "SELECT version, policy, created_at FROM portfolio_policies WHERE account_index=? ORDER BY version DESC LIMIT 1",
    index,
  )[0];
  if (!row) return null;
  try { return { version: row.version, policy: JSON.parse(row.policy), created_at: row.created_at }; }
  catch { return null; }
}

export type SelectionRow = {
  id: number; variant: string | null; allocation: string; policy_version: number | null;
  account_snapshot_id: number | null; expected_drift_pct: number | null;
  expected_rebalance_total_krw: number | null; expected_rebalance_rounds: number | null;
  precheck_status: string | null; precheck_reasons: string | null; selected_by: string | null;
  status: string; selected_at: string;
};

export async function getCurrentSelection(index: number): Promise<SelectionRow | null> {
  if (usePostgres()) {
    const r = await pgDb.getCurrentSelection(index);
    if (!r) return null;
    // PG → SQLite SelectionRow shape 정규화 (jsonb→string, *_id/_json 컬럼명 정렬).
    return {
      id: r.id,
      variant: r.variant,
      allocation: r.allocation_json == null ? "" : JSON.stringify(r.allocation_json),
      policy_version: r.policy_version_id,
      account_snapshot_id: r.account_snapshot_id,
      expected_drift_pct: r.expected_drift_pct,
      expected_rebalance_total_krw: r.expected_rebalance_total_krw,
      expected_rebalance_rounds: r.expected_rebalance_rounds,
      precheck_status: r.precheck_status,
      precheck_reasons:
        r.precheck_reasons_json == null ? null : JSON.stringify(r.precheck_reasons_json),
      selected_by: r.selected_by,
      status: r.status,
      selected_at: r.selected_at,
    };
  }
  return query<SelectionRow>(
    "SELECT * FROM allocation_selections WHERE account_index=? AND status='active' ORDER BY id DESC LIMIT 1", index,
  )[0] ?? null;
}

export function getSelectionHistory(index: number, limit = 20): SelectionRow[] {
  return query<SelectionRow>(
    "SELECT * FROM allocation_selections WHERE account_index=? ORDER BY id DESC LIMIT ?", index, limit,
  );
}

export type ProfileHistoryRow = { id: number; snapshot: string; source: string | null; created_at: string };

export function getProfileHistory(index: number, limit = 20): ProfileHistoryRow[] {
  return query<ProfileHistoryRow>(
    "SELECT id, snapshot, source, created_at FROM investor_profile_history WHERE account_index=? ORDER BY id DESC LIMIT ?",
    index, limit,
  );
}

export function getLatestDecision(index: number): any | null {
  const rows = query<{ payload: string; created_at: string }>(
    "SELECT payload, created_at FROM decisions WHERE account_index=? ORDER BY id DESC LIMIT 1",
    index,
  );
  if (!rows[0]) return null;
  try {
    return { ...JSON.parse(rows[0].payload), saved_at: rows[0].created_at };
  } catch {
    return null;
  }
}

export function getUniverse(index: number): UniverseRow[] {
  return query<UniverseRow>(
    "SELECT ticker, market, name, asset_class, currency, target_weight_pct, last_price, verified_at "
      + "FROM universe_instruments WHERE account_index=? AND is_active=1 ORDER BY id",
    index,
  );
}

// ---------------------------------------------------------------------------
// Track K — Dashboard/History 추이: 조언 이력 / drift 이력 (SQLite 미러 직접 조회).
// 노출(net/gross/테마/hedge) 계산은 Python series() 가 SSOT (decision.py 정의와 동일).
//   → 웹은 그 결과를 history-series API 로 받는다. 여기 함수는 가벼운 직접 조회용(SQLite 고정).
// 계좌 격리: 모든 조회는 account_index 조건. PG 경로 비대상(엔진 미러 = SQLite).
// ---------------------------------------------------------------------------
export type AdviceEventRow = {
  id: number;
  field_consultation_id: number | null;
  field_name: string | null;
  user_action: string;            // applied|edited|ignored|saved
  detail: string | null;
  agent_name: string | null;
  advice_type: string | null;
  evidence_ids: string | null;    // CSV 또는 JSON 배열
  lesson_ids: string | null;
  created_at: string;
};

/** 조언 적용/수정/무시/저장 이력(최신순) — field_advice_events + field_consultations 조인. */
export function getAdviceHistory(index: number, limit = 50): AdviceEventRow[] {
  const lim = Math.max(1, Math.min(limit, 500));
  return query<AdviceEventRow>(
    "SELECT e.id, e.field_consultation_id, e.field_name, e.user_action, e.detail, e.created_at, "
      + "c.agent_name, c.advice_type, c.evidence_ids, c.lesson_ids "
      + "FROM field_advice_events e "
      + "LEFT JOIN field_consultations c ON c.id = e.field_consultation_id "
      + "WHERE e.account_index=? ORDER BY e.id DESC LIMIT ?",
    index, lim,
  );
}

export type DriftPoint = { review_date: string; drift_score: number | null; action_decision: string | null };

/** drift 점검 이력(오래된→최신) — daily_portfolio_reviews. 차트의 drift 라인 보조 조회용. */
export function getDriftHistory(index: number, limit = 60): DriftPoint[] {
  const lim = Math.max(1, Math.min(limit, 365));
  const rows = query<DriftPoint>(
    "SELECT review_date, drift_score, action_decision FROM daily_portfolio_reviews "
      + "WHERE account_index=? ORDER BY review_date DESC, id DESC LIMIT ?",
    index, lim,
  );
  return rows.reverse(); // 오름차순(차트 축 정합)
}

// ---------------------------------------------------------------------------
// Track 2 — Admin overview 보조 집계(가벼운 직접 조회, SQLite 미러 고정).
//   계좌별 최신 daily review 1행(있으면) — "Daily Review 생성 여부" + 위험 경고 신호용.
//   risk_passed=0 또는 drift_score 가 임계(기본 30%) 이상이면 위험 경고로 본다.
//   임계는 환경변수(설정)로, UI 하드코딩 아님.
// ---------------------------------------------------------------------------
const RISK_DRIFT_WARN_PCT = Number(process.env.RISK_DRIFT_WARN_PCT ?? 30);

export type LatestReviewRow = {
  account_index: number;
  review_date: string | null;
  drift_score: number | null;
  risk_passed: number | null;
  action_decision: string | null;
};

/** 한 계좌의 최신 daily review 1행(없으면 null) — admin 집계용. SQLite 미러 직접 조회. */
export function getLatestReview(index: number): LatestReviewRow | null {
  const row = query<LatestReviewRow>(
    "SELECT account_index, review_date, drift_score, risk_passed, action_decision "
      + "FROM daily_portfolio_reviews WHERE account_index=? ORDER BY review_date DESC, id DESC LIMIT 1",
    index,
  )[0];
  return row ?? null;
}

/** review 행이 위험 경고에 해당하는지(risk 미통과 또는 drift 임계 초과). */
export function isRiskWarning(r: LatestReviewRow | null): boolean {
  if (!r) return false;
  if (r.risk_passed === 0) return true;
  if (r.drift_score != null && Math.abs(r.drift_score) >= RISK_DRIFT_WARN_PCT) return true;
  return false;
}

function freshnessSeconds(iso: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (Date.now() - t) / 1000;
}

export async function getAccountView(index: number): Promise<AccountView | null> {
  if (usePostgres()) {
    const v = await pgDb.getAccountView(index);
    if (!v) return null;
    // PG → SQLite AccountView shape 정규화. steps/progress/isFresh 는 pgDb 가 동일 로직으로 계산.
    return {
      account_index: v.account_index,
      alias: v.alias,
      mode: v.mode,
      account_no_masked: v.account_no_masked,
      has_credentials: v.has_credentials ? 1 : 0,
      token_status: v.token_status,
      sync_status: v.sync_status,
      last_error: v.last_error,
      last_synced_at: v.last_synced_at,
      broker: brokerFromSource(v.snapshot?.source),
      snapshot: v.snapshot
        ? {
            id: v.snapshot.id,
            cash_krw: v.snapshot.cash_krw,
            total_value_krw: v.snapshot.total_value_krw,
            holdings_count: v.snapshot.holdings_count,
            source: v.snapshot.source,
            captured_at: v.snapshot.captured_at,
          }
        : null,
      holdings: v.holdings.map((h) => ({
        ticker: h.ticker,
        name: h.name,
        qty: h.qty,
        avg_price: h.avg_price,
        market_value: h.market_value,
        currency: h.currency,
      })),
      steps: v.steps,
      progress: v.progress,
      isFresh: v.isFresh,
    };
  }
  const acc = query<Omit<AccountRow, "broker">>("SELECT * FROM accounts WHERE account_index=?", index)[0];
  if (!acc) return null;
  const snapshot = query<Snapshot>(
    "SELECT id, cash_krw, total_value_krw, holdings_count, source, captured_at "
      + "FROM account_snapshots WHERE account_index=? ORDER BY id DESC LIMIT 1",
    index,
  )[0] ?? null;
  const holdings = snapshot
    ? query<Holding>(
        "SELECT ticker, name, qty, avg_price, market_value, currency FROM holdings WHERE snapshot_id=?",
        snapshot.id,
      )
    : [];

  const ageSec = freshnessSeconds(acc.last_synced_at);
  const isFresh = ageSec !== null && ageSec <= FRESHNESS_SEC;

  // 진행도 = DB 상태 기반 계산 (하드코딩 아님)
  const credentials = acc.has_credentials === 1;
  const tokenOk = acc.token_status === "ok";
  const balanceSynced = snapshot !== null && acc.sync_status === "ok";
  const ready = credentials && tokenOk && balanceSynced && isFresh;
  const steps: SyncStep[] = [
    { key: "credentials", label: "자격증명 저장", desc: ".env 에 APP Key/Secret 저장", done: credentials },
    { key: "token", label: "KIS 토큰 검증", desc: "OAuth 토큰 발급 성공", done: tokenOk },
    { key: "balance", label: "잔고 동기화", desc: "보유 종목·예수금 DB 저장", done: balanceSynced },
    { key: "ready", label: "관리 준비 완료", desc: "최신 스냅샷 + 리밸런싱 제안 가능", done: ready },
  ];
  const progress = Math.round((steps.filter((s) => s.done).length / steps.length) * 100);

  return { ...acc, broker: brokerFromSource(snapshot?.source), snapshot, holdings, steps, progress, isFresh };
}
