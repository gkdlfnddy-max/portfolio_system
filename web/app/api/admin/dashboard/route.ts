import { NextResponse } from "next/server";
import { requireAdmin, isDenied } from "@/lib/auth/rbac";
import { getAccounts, getAccountView, getDriftHistory, getLatestReview, isRiskWarning } from "@/lib/server/portfolioDb";
import { q } from "@/lib/auth/db";

export const dynamic = "force-dynamic";

// Track 2 — admin 전체 계좌 overview(가벼운 집계). admin 전용(requireAdmin → 비admin 403).
// 운영 truth(DB)만 읽는다. KIS 직접 호출/mock 없음 — snapshot 없으면 null(빈상태).
// 계좌별 가벼운 요약 + 전체 집계(동기화정상·권한미할당·실전·DailyReview·위험경고).
export type AdminDashboardAccount = {
  account_index: number;
  alias: string | null;
  mode: string | null;
  sync_status: string | null;
  is_fresh: boolean;
  last_synced_at: string | null;
  total_value_krw: number | null;
  cash_krw: number | null;
  holdings_count: number | null;
  captured_at: string | null;
  // drift: daily_portfolio_reviews 최신 점검값(있으면). 없으면 null.
  latest_drift: number | null;
  drift_action: string | null;
  drift_date: string | null;
  has_daily_review: boolean;   // 최신 daily review 존재 여부
  risk_warning: boolean;       // risk 미통과 또는 drift 임계 초과
  assigned: boolean | null;    // user_account_access 권한 행 존재. PG 미연결이면 null.
};

export type AdminDashboardResponse = {
  ok: true;
  generated_at: string;
  totals: {
    account_count: number;
    total_value_krw: number | null; // 집계 가능한 계좌들의 총자산 합(없으면 null)
    accounts_with_snapshot: number;
    sync_ok: number;            // sync_status=ok 인 계좌 수
    live_accounts: number;      // mode=live 계좌 수
    unassigned: number | null;  // 권한 미할당(접근 행 없음) 계좌 수. PG 미연결이면 null.
    daily_review_generated: number; // 최신 daily review 가 있는 계좌 수
    risk_warnings: number;      // 위험 경고 계좌 수
  };
  accounts: AdminDashboardAccount[];
};

// 인증 DB(PG)에서 계좌별 권한 할당 집합을 가져온다. PG 미연결/오류면 null(degrade).
async function loadAuthSets(): Promise<{ assigned: Set<number> | null }> {
  let assigned: Set<number> | null = null;
  try {
    const r = await q<{ account_index: number }>(
      "SELECT DISTINCT account_index FROM portfolio.user_account_access",
    );
    assigned = new Set(r.rows.map((x) => Number(x.account_index)));
  } catch { assigned = null; }
  return { assigned };
}

export async function GET() {
  // admin 전용 — 비admin 은 requireAdmin 이 403(FORBIDDEN) 반환.
  const admin = await requireAdmin();
  if (isDenied(admin)) return admin;

  const list = await getAccounts();
  const { assigned } = await loadAuthSets();

  const accounts: AdminDashboardAccount[] = [];
  let sumValue = 0;
  let haveAnyValue = false;
  let withSnapshot = 0;
  let syncOk = 0;
  let liveCount = 0;
  let unassigned = 0;
  let reviewCount = 0;
  let riskCount = 0;

  for (const a of list) {
    const view = await getAccountView(a.account_index);
    const snap = view?.snapshot ?? null;
    if (snap) withSnapshot += 1;
    if (snap?.total_value_krw != null) {
      sumValue += snap.total_value_krw;
      haveAnyValue = true;
    }
    if (a.sync_status === "ok") syncOk += 1;
    if (a.mode === "live") liveCount += 1;

    // 최신 daily review(있으면) — drift + 위험 경고 + 생성 여부.
    const review = getLatestReview(a.account_index);
    const hasReview = review !== null;
    if (hasReview) reviewCount += 1;
    const warn = isRiskWarning(review);
    if (warn) riskCount += 1;
    // drift 는 review 우선, 없으면 별도 drift 이력 1행(호환).
    let latestDrift: number | null = review?.drift_score ?? null;
    let driftAction: string | null = review?.action_decision ?? null;
    let driftDate: string | null = review?.review_date ?? null;
    if (latestDrift == null) {
      const drift = getDriftHistory(a.account_index, 1);
      const d = drift.length > 0 ? drift[drift.length - 1] : null;
      latestDrift = d?.drift_score ?? null;
      driftAction = d?.action_decision ?? null;
      driftDate = d?.review_date ?? null;
    }

    const isAssigned = assigned === null ? null : assigned.has(a.account_index);
    if (isAssigned === false) unassigned += 1;

    accounts.push({
      account_index: a.account_index,
      alias: a.alias,
      mode: a.mode,
      sync_status: a.sync_status,
      is_fresh: view?.isFresh ?? false,
      last_synced_at: a.last_synced_at,
      total_value_krw: snap?.total_value_krw ?? null,
      cash_krw: snap?.cash_krw ?? null,
      holdings_count: snap?.holdings_count ?? null,
      captured_at: snap?.captured_at ?? null,
      latest_drift: latestDrift,
      drift_action: driftAction,
      drift_date: driftDate,
      has_daily_review: hasReview,
      risk_warning: warn,
      assigned: isAssigned,
    });
  }

  const body: AdminDashboardResponse = {
    ok: true,
    generated_at: new Date().toISOString(),
    totals: {
      account_count: list.length,
      total_value_krw: haveAnyValue ? sumValue : null,
      accounts_with_snapshot: withSnapshot,
      sync_ok: syncOk,
      live_accounts: liveCount,
      unassigned: assigned === null ? null : unassigned,
      daily_review_generated: reviewCount,
      risk_warnings: riskCount,
    },
    accounts,
  };
  return NextResponse.json(body);
}
