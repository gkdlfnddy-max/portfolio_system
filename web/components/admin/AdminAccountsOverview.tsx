"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { LayoutGrid, ArrowUpRight, AlertTriangle } from "lucide-react";

// 관리자 — 전체 계좌 overview. 데이터/차단은 서버 /api/admin/dashboard(requireAdmin)가 담당.
// 비admin 호출 시 403 → "관리자 전용" 안내. 빈 데이터는 정직하게 빈상태 표시(mock 0).
type DashAccount = {
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
  latest_drift: number | null;
  drift_action: string | null;
  drift_date: string | null;
  has_daily_review: boolean;
  risk_warning: boolean;
  assigned: boolean | null;
};
type DashTotals = {
  account_count: number;
  total_value_krw: number | null;
  accounts_with_snapshot: number;
  sync_ok: number;
  live_accounts: number;
  unassigned: number | null;
  daily_review_generated: number;
  risk_warnings: number;
};
type DashResponse = {
  ok: boolean;
  generated_at: string;
  totals: DashTotals;
  accounts: DashAccount[];
};

function fmtKrw(v: number | null): string {
  if (v == null) return "—";
  return new Intl.NumberFormat("ko-KR").format(Math.round(v)) + "원";
}
function fmtPct(v: number | null): string {
  if (v == null) return "—";
  return v.toFixed(1) + "%";
}
function fmtDateTime(iso: string | null): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  return new Date(t).toLocaleString("ko-KR");
}
function modeLabel(mode: string | null): string {
  const map: Record<string, string> = { mock: "오프라인", paper: "모의투자", live: "실전" };
  return mode ? (map[mode] ?? mode) : "—";
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "warning" | "error" }) {
  const valueCls = tone === "error" ? "text-error" : tone === "warning" ? "text-warning" : "text-neutral-900";
  return (
    <div className="rounded-xl border border-neutral-100 p-3">
      <div className="text-xs text-neutral-400">{label}</div>
      <div className={`text-lg font-bold ${valueCls}`}>{value}</div>
    </div>
  );
}

export function AdminAccountsOverview() {
  const [data, setData] = useState<DashResponse | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const res = await fetch("/api/admin/dashboard", { cache: "no-store" });
      if (res.status === 403 || res.status === 401) {
        setForbidden(true);
        setData(null);
        return;
      }
      const j = (await res.json().catch(() => ({}))) as Partial<DashResponse>;
      if (res.ok && j.ok) {
        setForbidden(false);
        setData(j as DashResponse);
      } else {
        setErr((j as any)?.error ?? "계좌 현황을 불러오지 못했습니다.");
        setData(null);
      }
    } catch {
      setErr("네트워크 오류가 발생했습니다.");
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (forbidden) {
    return (
      <Card>
        <CardBody className="py-10 text-center space-y-2">
          <AlertTriangle className="w-8 h-8 text-error mx-auto" />
          <div className="text-base font-semibold text-neutral-900">관리자 전용</div>
          <div className="text-sm text-neutral-400">
            이 화면은 관리자만 볼 수 있습니다. 접근 권한은 서버에서 차단됩니다.
          </div>
        </CardBody>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <LayoutGrid className="w-5 h-5 text-primary" /> 계좌 현황
        </CardTitle>
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          새로고침
        </Button>
      </CardHeader>
      <CardBody className="space-y-4">
        {err && <div className="text-sm text-error">{err}</div>}

        {data && (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
            <Stat label="전체 계좌 수" value={String(data.totals.account_count)} />
            <Stat label="총자산 합계" value={fmtKrw(data.totals.total_value_krw)} />
            <Stat label="동기화 정상" value={`${data.totals.sync_ok} / ${data.totals.account_count}`} />
            <Stat label="스냅샷 보유" value={`${data.totals.accounts_with_snapshot} / ${data.totals.account_count}`} />
            <Stat label="실전(live) 계좌" value={String(data.totals.live_accounts)} tone={data.totals.live_accounts > 0 ? "error" : undefined} />
            <Stat
              label="권한 미할당"
              value={data.totals.unassigned == null ? "—" : String(data.totals.unassigned)}
              tone={(data.totals.unassigned ?? 0) > 0 ? "warning" : undefined}
            />
            <Stat label="Daily Review 생성" value={`${data.totals.daily_review_generated} / ${data.totals.account_count}`} />
            <Stat
              label="위험 경고"
              value={String(data.totals.risk_warnings)}
              tone={data.totals.risk_warnings > 0 ? "error" : undefined}
            />
          </div>
        )}

        {loading && !data ? (
          <div className="text-sm text-neutral-400">불러오는 중…</div>
        ) : data && data.accounts.length === 0 ? (
          <div className="text-sm text-neutral-400 py-6 text-center">
            관리 중인 계좌가 없습니다. 계좌를 추가하면 여기에 표시됩니다.
          </div>
        ) : data ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-neutral-400 border-b border-neutral-100">
                  <th className="py-2 pr-3 font-medium">계좌</th>
                  <th className="py-2 pr-3 font-medium">모드</th>
                  <th className="py-2 pr-3 font-medium">동기화</th>
                  <th className="py-2 pr-3 font-medium text-right">총자산</th>
                  <th className="py-2 pr-3 font-medium text-right">현금</th>
                  <th className="py-2 pr-3 font-medium text-right">보유</th>
                  <th className="py-2 pr-3 font-medium text-right">drift</th>
                  <th className="py-2 pr-3 font-medium text-center">점검</th>
                  <th className="py-2 pr-3 font-medium text-center">위험</th>
                  <th className="py-2 pr-3 font-medium text-center">권한</th>
                  <th className="py-2 pr-3 font-medium">최근 동기화</th>
                  <th className="py-2 pr-0 font-medium text-right">상세</th>
                </tr>
              </thead>
              <tbody>
                {data.accounts.map((a) => (
                  <tr key={a.account_index} className="border-b border-neutral-50 last:border-0">
                    <td className="py-2.5 pr-3">
                      <div className="font-semibold text-neutral-900">{a.alias || `계좌 #${a.account_index}`}</div>
                      <div className="text-xs text-neutral-400">#{a.account_index}</div>
                    </td>
                    <td className="py-2.5 pr-3">
                      <Badge className="bg-neutral-100 text-neutral-700">{modeLabel(a.mode)}</Badge>
                    </td>
                    <td className="py-2.5 pr-3">
                      {a.sync_status === "ok" ? (
                        <Badge className={a.is_fresh ? "bg-primary-50 text-primary-700" : "bg-warning/10 text-warning"}>
                          {a.is_fresh ? "최신" : "오래됨"}
                        </Badge>
                      ) : (
                        <Badge className="bg-error/10 text-error">{a.sync_status ?? "미동기화"}</Badge>
                      )}
                    </td>
                    <td className="py-2.5 pr-3 text-right tabular-nums">{fmtKrw(a.total_value_krw)}</td>
                    <td className="py-2.5 pr-3 text-right tabular-nums">{fmtKrw(a.cash_krw)}</td>
                    <td className="py-2.5 pr-3 text-right tabular-nums">{a.holdings_count ?? "—"}</td>
                    <td className="py-2.5 pr-3 text-right tabular-nums">{fmtPct(a.latest_drift)}</td>
                    <td className="py-2.5 pr-3 text-center">
                      {a.has_daily_review ? (
                        <Badge className="bg-primary-50 text-primary-700">생성</Badge>
                      ) : (
                        <span className="text-xs text-neutral-300">—</span>
                      )}
                    </td>
                    <td className="py-2.5 pr-3 text-center">
                      {a.risk_warning ? (
                        <Badge className="bg-error/10 text-error inline-flex items-center gap-1">
                          <AlertTriangle className="w-3 h-3" /> 경고
                        </Badge>
                      ) : (
                        <span className="text-xs text-neutral-300">—</span>
                      )}
                    </td>
                    <td className="py-2.5 pr-3 text-center">
                      {a.assigned == null ? (
                        <span className="text-xs text-neutral-300">—</span>
                      ) : a.assigned ? (
                        <Badge className="bg-neutral-100 text-neutral-600">할당</Badge>
                      ) : (
                        <Badge className="bg-warning/10 text-warning">미할당</Badge>
                      )}
                    </td>
                    <td className="py-2.5 pr-3 text-xs text-neutral-500">{fmtDateTime(a.last_synced_at)}</td>
                    <td className="py-2.5 pr-0 text-right">
                      <Link
                        href={`/accounts/${a.account_index}`}
                        className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                      >
                        열기 <ArrowUpRight className="w-3 h-3" />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}
