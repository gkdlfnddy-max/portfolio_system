"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { ArrowLeft, ArrowRight, RefreshCw, Check, ShieldCheck, ShieldAlert, AlertTriangle } from "lucide-react";
import { DonutChart, DonutLegend } from "@/components/DonutChart";
import { StrategySummary } from "@/components/StrategySummary";
import { toBuckets } from "@/lib/allocation/buckets";

const LABEL: Record<string, string> = { conservative: "보수", base: "기준", aggressive: "공격" };
const won = (n: number | null | undefined) => (n == null ? "—" : Math.round(n).toLocaleString("ko-KR") + "원");

type Row = { kind: string; ref: string | null; weight_pct: number };
type Variant = {
  rows: Row[];
  precheck: { status: string; reasons: { level: string; msg: string }[] };
  estimate: { expected_drift_pct: number; expected_rebalance_total_krw: number; expected_rebalance_rounds: number; current_cash_pct: number; target_cash_pct: number };
};
type Options = {
  ok: boolean; proposal_id: string; policy_version: number | null;
  variants: Record<string, Variant>;
  selected: any | null;
};

function PreBadge({ s }: { s: string }) {
  if (s === "block") return <Badge className="bg-error/10 text-error flex items-center gap-1"><ShieldAlert className="w-3.5 h-3.5" /> 한도 위반</Badge>;
  if (s === "warn") return <Badge className="bg-warning/10 text-warning flex items-center gap-1"><AlertTriangle className="w-3.5 h-3.5" /> 주의</Badge>;
  return <Badge className="bg-success/10 text-success flex items-center gap-1"><ShieldCheck className="w-3.5 h-3.5" /> 한도 내</Badge>;
}

export default function AllocationPage() {
  const { id } = useParams() as { id: string };
  const [o, setO] = useState<Options | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await fetch(`/api/accounts/${id}/allocation`, { cache: "no-store" });
    setO(await r.json());
  }, [id]);
  useEffect(() => { (async () => { await load(); setLoading(false); })(); }, [load]);

  const act = async (action: string, extra: any = {}) => {
    setBusy(action + (extra.variant ?? "")); setMsg(null);
    try {
      const r = await fetch(`/api/accounts/${id}/allocation`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ action, ...extra }),
      });
      const j = await r.json();
      if (!j.ok) setMsg(j.error ?? "실패");
      else if (action === "select") setMsg(`확정됨 — ${LABEL[extra.variant] ?? extra.variant} 안 (drift ${j.estimate?.expected_drift_pct}% · ${j.estimate?.expected_rebalance_rounds}회 분할)`);
    } catch (e: any) { setMsg(e?.message ?? "실패"); }
    await load(); setBusy(null);
  };

  const selectVariant = (variant: string, status: string) =>
    act("select", { proposal_id: o?.proposal_id, variant, user_override: status === "block" ? 1 : 0 });

  return (
    <div className="max-w-5xl mx-auto px-5 py-10 space-y-6">
      <Link href={`/accounts/${id}/strategy`} className="text-sm text-neutral-500 flex items-center gap-1 hover:text-primary">
        <ArrowLeft className="w-4 h-4" /> 운용 전략
      </Link>

      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-neutral-900">목표 포트폴리오 확정 (3안 중 선택)</h1>
          <p className="text-sm text-neutral-500 mt-1">
            대전제·중전제로 만든 <b>보수/기준/공격</b> 3안입니다. “수익 좋아 보이는 안”이 아니라
            <b> 내 정책 한도 안에서 안전한 안</b>을 골라 이 계좌의 공식 목표비중으로 확정하세요.
          </p>
        </div>
        <Button size="sm" variant="outline" onClick={() => act("generate")} disabled={!!busy}>
          <RefreshCw className={`w-4 h-4 ${busy === "generate" ? "animate-spin" : ""}`} /> 3안 다시 생성
        </Button>
      </div>

      {msg && <div className="rounded-lg bg-primary-50 border border-primary-100 p-3 text-sm text-primary-700">{msg}</div>}

      {o?.selected && (
        <Card className="border-primary-200">
          <CardBody className="flex flex-wrap items-center justify-between gap-3">
            <div className="text-sm">
              <span className="text-neutral-400">현재 확정: </span>
              <b className="text-primary">{LABEL[o.selected.variant] ?? o.selected.variant}</b> 안 ·
              예상 조정 {o.selected.expected_drift_pct}% · {o.selected.expected_rebalance_rounds}회 분할 ·
              pre-check <b>{o.selected.precheck_status}</b> · {new Date(o.selected.selected_at).toLocaleString("ko-KR")}
            </div>
            <div className="flex gap-2">
              <Link href={`/accounts/${id}/selection`}><Button size="sm">종목·ETF 선정 <ArrowRight className="w-4 h-4" /></Button></Link>
              <Link href={`/accounts/${id}/portfolio`}><Button size="sm" variant="outline">포트폴리오에서 보기 <ArrowRight className="w-4 h-4" /></Button></Link>
              <Button size="sm" variant="ghost" onClick={() => act("cancel")} disabled={!!busy}>선택 취소</Button>
            </div>
          </CardBody>
        </Card>
      )}

      {loading ? (
        <div className="text-sm text-neutral-400 py-10 text-center">불러오는 중…</div>
      ) : !o?.ok ? (
        <Card><CardBody className="text-center py-10 text-sm text-neutral-500">
          3안을 만들 수 없습니다. 먼저 <Link href={`/accounts/${id}/strategy`} className="text-primary">운용 전략</Link>을 입력하세요.
        </CardBody></Card>
      ) : (
        <div className="grid md:grid-cols-3 gap-4">
          {["conservative", "base", "aggressive"].map((v) => {
            const vd = o.variants[v];
            if (!vd) return null;
            const isSel = o.selected?.variant === v;
            const cash = vd.rows.find((r) => r.kind === "cash")?.weight_pct ?? 0;
            const bonds = vd.rows.filter((r) => r.kind === "bond");
            const anchors = vd.rows.filter((r) => r.kind === "anchor");
            const tilts = vd.rows.filter((r) => r.kind === "tilt");
            const hedges = vd.rows.filter((r) => r.kind === "hedge");
            const bondTotal = Math.round(bonds.reduce((a, b) => a + b.weight_pct, 0) * 10) / 10;
            const pureCash = cash;                                  // 순현금
            const defensive = Math.round((pureCash + bondTotal) * 10) / 10;  // 방어 = 순현금 + 채권
            const riskAssets = Math.round((100 - defensive) * 10) / 10;       // 위험 = 100 - 방어
            return (
              <Card key={v} className={isSel ? "border-primary ring-1 ring-primary/30" : ""}>
                <CardHeader className="flex-row items-center justify-between">
                  <CardTitle className="flex items-center gap-2">{LABEL[v]}{isSel && <Check className="w-4 h-4 text-primary" />}</CardTitle>
                  <PreBadge s={vd.precheck.status} />
                </CardHeader>
                <CardBody className="space-y-3">
                  {/* 핵심 전략 요약(Track2) — 숫자만이 아니라 이 안의 철학 */}
                  <StrategySummary accountId={Number(id)} variant={v} />
                  {/* 비중 구조 도넛(Track3) — 차트·숫자 동일 source(buckets) */}
                  <div className="flex flex-col items-center gap-2">
                    <DonutChart buckets={toBuckets(vd.rows)} />
                    <DonutLegend buckets={toBuckets(vd.rows)} />
                  </div>
                  {/* 상세 비중: 방어자산 = 순현금 + 채권/국채 (채권 0%도 표시) */}
                  <div className="rounded-lg bg-sky-50/60 border border-sky-100 p-2 text-sm">
                    <div className="flex justify-between font-medium text-neutral-800"><span>방어자산</span><span className="tabular-nums">{defensive}%</span></div>
                    <div className="flex justify-between text-xs text-neutral-500 mt-0.5"><span>· 순현금 (즉시 매수여력)</span><span className="tabular-nums">{pureCash}%</span></div>
                    <div className="flex justify-between text-xs text-sky-600"><span>· 채권/국채 (금리·경기 대응)</span><span className="tabular-nums">{bondTotal}%</span></div>
                  </div>
                  {/* 위험자산 = 코어 ETF + 테마 + 헤지 */}
                  <div className="space-y-1.5 text-sm">
                    <div className="flex justify-between font-medium text-neutral-800"><span>위험자산</span><span className="tabular-nums">{riskAssets}%</span></div>
                    {anchors.map((a) => (
                      <div key={a.ref} className="flex justify-between"><span className="text-neutral-500">🌐 {a.ref}</span><span className="tabular-nums">{a.weight_pct}%</span></div>
                    ))}
                    {tilts.map((t) => (
                      <div key={t.ref} className="flex justify-between"><span className="text-neutral-500">📈 {t.ref}</span><span className="tabular-nums">{t.weight_pct}%</span></div>
                    ))}
                    {hedges.map((h) => (
                      <div key={h.ref} className="flex justify-between"><span className="text-accent-600">🛡 {h.ref}</span><span className="tabular-nums text-accent-600">{h.weight_pct}%</span></div>
                    ))}
                  </div>
                  <p className="text-[10px] text-neutral-400">방어자산 = 순현금 + 채권/국채 · 위험자산 = 100 − 방어. 채권은 현금밴드에 더해지지 않고 방어 안에서 순현금과 나뉩니다. <b>헤지(인버스)는 위험자산 안에서 차감</b>되어 롱(테마/코어)을 상쇄합니다 — 현금/채권(방어)에서 빼지 않습니다. (글로벌 코어 ETF = 테마 변동성을 받쳐주는 중심 자산)</p>
                  {/* 예상치 */}
                  <div className="rounded-lg bg-neutral-50 p-2.5 text-xs space-y-1">
                    <div className="flex justify-between"><span className="text-neutral-400">예상 조정량(drift)</span><span className="tabular-nums">{vd.estimate.expected_drift_pct}%</span></div>
                    <div className="flex justify-between"><span className="text-neutral-400">리밸런싱 총액</span><span className="tabular-nums">{won(vd.estimate.expected_rebalance_total_krw)}</span></div>
                    <div className="flex justify-between"><span className="text-neutral-400">분할 회차</span><span className="tabular-nums">{vd.estimate.expected_rebalance_rounds}회</span></div>
                  </div>
                  {/* pre-check 사유 */}
                  {vd.precheck.reasons.filter((r) => r.level !== "info").length > 0 && (
                    <ul className="space-y-0.5">
                      {vd.precheck.reasons.filter((r) => r.level !== "info").map((r, i) => (
                        <li key={i} className={`text-[11px] ${r.level === "block" ? "text-error" : "text-warning"}`}>· {r.msg}</li>
                      ))}
                    </ul>
                  )}
                  <Button className="w-full" variant={isSel || vd.precheck.status === "block" ? "outline" : "primary"}
                    disabled={!!busy || isSel} onClick={() => selectVariant(v, vd.precheck.status)}>
                    {isSel ? "확정됨" : vd.precheck.status === "block" ? "한도 위반 — 무시하고 선택" : "이 안으로 확정"}
                  </Button>
                </CardBody>
              </Card>
            );
          })}
        </div>
      )}

      <p className="text-xs text-neutral-400">
        선택은 append-only 로 저장(이전 안은 참고용으로 보존). 종목 단위 qty=0·가격 이상치는 확정 후 의사결정 단계에서 차단. 실전 주문은 차단 상태.
      </p>
    </div>
  );
}
