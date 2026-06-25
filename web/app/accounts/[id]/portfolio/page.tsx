"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { ArrowLeft, ArrowRight, RefreshCw, ShieldCheck, ShieldAlert } from "lucide-react";

const VLABEL: Record<string, string> = { conservative: "보수", base: "기준", aggressive: "공격" };
const won = (n: number | null | undefined) => (n == null ? "—" : Math.round(n).toLocaleString("ko-KR") + "원");

type Line = {
  kind: string; ref: string; role: string;
  current_pct: number; target_pct: number; drift: number; band: number; needs_adjust: boolean;
  direction?: string; total_adjust_pct?: number; total_adjust_krw?: number;
  this_cycle_pct?: number; this_cycle_krw?: number; remaining_pct?: number; split_rounds?: number;
};
type Decision = {
  ok: boolean; block_code?: string; error?: string;
  selected_variant?: string; total_value_krw?: number; cash_current_pct?: number; cash_target_pct?: number;
  lines?: Line[]; hedge_count?: number; hedge_total_pct?: number;
  risk?: { passed: boolean; violations: { limit: string; observed: number; threshold: number; detail: string }[] };
  provenance?: any; snapshot_at?: string; note?: string;
};

function RoleBadge({ role }: { role: string }) {
  if (role === "hedge") return <Badge className="bg-accent/10 text-accent-600">헤지(인버스)</Badge>;
  if (role === "cash") return <Badge className="bg-neutral-100 text-neutral-500">현금</Badge>;
  return <Badge className="bg-primary-50 text-primary-700">롱</Badge>;
}

export default function AccountPortfolioPage() {
  const { id } = useParams() as { id: string };
  const [d, setD] = useState<Decision | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const compute = useCallback(async () => {
    const r = await fetch(`/api/accounts/${id}/decision`, { method: "POST" });
    setD(await r.json().then((j) => j.decision ?? j));
  }, [id]);
  useEffect(() => { (async () => { await compute(); setLoading(false); })(); }, [compute]);

  const recompute = async () => { setBusy(true); await compute(); setBusy(false); };

  const blocked = d && d.ok === false;
  const adjusting = (d?.lines ?? []).filter((l) => l.needs_adjust);

  return (
    <div className="max-w-4xl mx-auto px-5 py-10 space-y-6">
      <Link href={`/accounts/${id}`} className="text-sm text-neutral-500 flex items-center gap-1 hover:text-primary">
        <ArrowLeft className="w-4 h-4" /> 계좌 화면
      </Link>

      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-neutral-900">포트폴리오 비중 관리</h1>
          <p className="text-sm text-neutral-500 mt-1">
            <b>확정한 목표 포트폴리오(selected allocation)</b> 기준으로만 drift·분할 계획·리스크를 계산합니다.
          </p>
        </div>
        <div className="flex gap-2">
          <Link href={`/accounts/${id}/allocation`}><Button size="sm" variant="outline">목표안 보기/변경</Button></Link>
          <Button size="sm" onClick={recompute} disabled={busy}><RefreshCw className={`w-4 h-4 ${busy ? "animate-spin" : ""}`} /> 다시 계산</Button>
        </div>
      </div>

      {loading ? (
        <div className="text-sm text-neutral-400 py-10 text-center">불러오는 중…</div>
      ) : blocked ? (
        <Card>
          <CardBody className="text-center py-8 space-y-3">
            <ShieldAlert className="w-8 h-8 text-warning mx-auto" />
            <p className="text-sm text-neutral-600">{d?.error}</p>
            {d?.block_code === "no_selection" && (
              <Link href={`/accounts/${id}/allocation`}><Button>목표 포트폴리오 확정하러 가기 <ArrowRight className="w-4 h-4" /></Button></Link>
            )}
            {d?.block_code === "stale_snapshot" && (
              <Link href={`/accounts/${id}`}><Button variant="outline">계좌 동기화하러 가기</Button></Link>
            )}
            <p className="text-[11px] text-neutral-400">확정 목표가 없거나 데이터가 오래되면 의사결정을 생성하지 않습니다 (hard-block).</p>
          </CardBody>
        </Card>
      ) : d?.ok ? (
        <>
          {/* 확정 목표 배너 */}
          <Card className="border-primary-200">
            <CardBody className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-sm">
                <span className="text-neutral-400">현재 사용 중인 목표안: </span>
                <b className="text-primary">{VLABEL[d.selected_variant ?? ""] ?? d.selected_variant}</b> ·
                현금 {d.cash_current_pct}% → {d.cash_target_pct}% ·
                헤지 {d.hedge_count}건({d.hedge_total_pct}%)
              </div>
              <span className="text-[11px] text-neutral-400">selected #{d.provenance?.selected_allocation_id} · policy v{d.provenance?.policy_version}</span>
            </CardBody>
          </Card>

          {/* 구성 vs 목표 + drift */}
          <Card>
            <CardHeader><CardTitle>현재 vs 목표 구성 (확정 목표 기준)</CardTitle></CardHeader>
            <CardBody>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-neutral-400 text-xs border-b border-neutral-100">
                    <th className="text-left py-1.5">구성</th><th className="text-left">역할</th>
                    <th className="text-right">현재</th><th className="text-right">목표</th><th className="text-right">drift</th><th className="text-center">판정</th>
                  </tr>
                </thead>
                <tbody>
                  {(d.lines ?? []).map((l) => (
                    <tr key={l.ref} className="border-b border-neutral-50">
                      <td className="py-1.5">{l.ref}</td>
                      <td><RoleBadge role={l.role} /></td>
                      <td className="text-right tabular-nums text-neutral-500">{l.current_pct}%</td>
                      <td className="text-right tabular-nums font-medium">{l.target_pct}%</td>
                      <td className={`text-right tabular-nums ${l.drift > 0 ? "text-error" : l.drift < 0 ? "text-success" : "text-neutral-400"}`}>{l.drift > 0 ? "+" : ""}{l.drift}</td>
                      <td className="text-center">{l.needs_adjust ? <Badge className="bg-warning/10 text-warning">조정</Badge> : <span className="text-xs text-neutral-300">유지</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </CardBody>
          </Card>

          {/* 분할 계획 (이번 회차) */}
          <Card>
            <CardHeader><CardTitle>분할 리밸런싱 계획 (이번 회차)</CardTitle></CardHeader>
            <CardBody>
              {adjusting.length === 0 ? (
                <div className="text-sm text-neutral-400 text-center py-4">밴드를 넘는 항목 없음 — 조정 불필요.</div>
              ) : (
                <div className="space-y-3">
                  {adjusting.map((l) => (
                    <div key={l.ref} className="rounded-xl border border-neutral-200 p-3">
                      <div className="flex items-center gap-2">
                        <Badge className={l.direction === "매수" ? "bg-primary-50 text-primary-700" : "bg-accent/10 text-accent-600"}>{l.direction}</Badge>
                        <span className="font-medium text-sm">{l.ref}</span>
                        <RoleBadge role={l.role} />
                      </div>
                      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mt-2 text-xs">
                        <div><div className="text-neutral-400">전체 조정</div><div className="font-semibold tabular-nums">{l.total_adjust_pct}% · {won(l.total_adjust_krw)}</div></div>
                        <div><div className="text-neutral-400">이번 회차</div><div className="font-semibold tabular-nums text-primary">{l.this_cycle_pct}% · {won(l.this_cycle_krw)}</div></div>
                        <div><div className="text-neutral-400">남은 조정</div><div className="font-semibold tabular-nums">{l.remaining_pct}%</div></div>
                        <div><div className="text-neutral-400">분할 회차</div><div className="font-semibold tabular-nums">{l.split_rounds}회</div></div>
                      </div>
                      <div className="text-[11px] text-neutral-400 mt-1.5">지정가보다 불리하면 이번 회차 보류 → 다음 사이클 재평가</div>
                    </div>
                  ))}
                </div>
              )}
              <p className="text-[11px] text-neutral-400 mt-3">{d.note}</p>
            </CardBody>
          </Card>

          {/* 리스크 게이트 */}
          <Card>
            <CardHeader><CardTitle>리스크 게이트 (잘못된 이동 방지)</CardTitle></CardHeader>
            <CardBody>
              <div className={`rounded-xl p-4 flex items-center gap-3 ${d.risk?.passed ? "bg-success/5 border border-success/20" : "bg-error/5 border border-error/20"}`}>
                {d.risk?.passed ? (
                  <><ShieldCheck className="w-6 h-6 text-success" /><span className="font-semibold text-success">통과 — 승인 단계로 진행 가능</span></>
                ) : (
                  <><ShieldAlert className="w-6 h-6 text-error" /><span className="font-semibold text-error">차단 — {d.risk?.violations.length}건</span></>
                )}
              </div>
              {(d.risk?.violations ?? []).map((v) => (
                <div key={v.limit} className="text-xs text-error mt-2">✗ {v.detail} — {v.limit}: 관측 {v.observed} / 한도 {v.threshold}</div>
              ))}
            </CardBody>
          </Card>

          <p className="text-xs text-neutral-400">
            확정목표 #{d.provenance?.selected_allocation_id} · policy v{d.provenance?.policy_version} · snapshot #{d.provenance?.account_snapshot_id} · price #{d.provenance?.price_snapshot_id ?? "—"} ·
            pace {d.provenance?.pace} · 스냅샷 {d.snapshot_at ? new Date(d.snapshot_at).toLocaleString("ko-KR") : "—"} · 전부 DB 저장값 · 주문은 승인 후 · 실전 차단
          </p>
        </>
      ) : (
        <div className="text-sm text-neutral-400 py-10 text-center">데이터가 없습니다. “다시 계산”을 눌러주세요.</div>
      )}
    </div>
  );
}
