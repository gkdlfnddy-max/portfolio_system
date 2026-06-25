"use client";

// 변이별(보수/기준/공격) 전략 요약 — Track 2.
// 데이터 진리: /api/accounts/[id]/allocation-explain (백엔드 allocation_explain CLI = 규칙+실측 allocation).
// mock/하드코딩 숫자 없음 · Anthropic API 미사용. 데이터 없으면 정직한 빈 상태.
import { useEffect, useState, useCallback } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { ShieldCheck, Users, AlertTriangle, RefreshCw } from "lucide-react";

type Bucket = {
  bucket_type: string;
  label: string;
  role: string;
  pct: number;
  explanation: string;
};

type Option = {
  variant: string;
  label: string;
  summary: string;
  suitable_for: string;
  key_risks: string[];
  rebalance_reason: string;
  buckets: Bucket[];
  defensive_pct: number | null;
  risk_pct: number | null;
  drift: number | null;
  rebalance_total_krw: number | null;
  rounds: number | null;
};

type Explain = {
  ok?: boolean;
  account_index?: number;
  proposal_id?: string;
  options?: Record<string, Option>;
};

const VARIANT_LABEL: Record<string, string> = {
  conservative: "보수",
  base: "기준",
  aggressive: "공격",
};

export function StrategySummary({ accountId, variant }: { accountId: number; variant: string }) {
  const [explain, setExplain] = useState<Explain | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch(`/api/accounts/${accountId}/allocation-explain`, { cache: "no-store" });
      const j = await r.json();
      setExplain(j?.explain ?? null);
      setErr(j?.error ?? null);
    } catch {
      setExplain(null);
      setErr("불러오기 실패");
    }
    setLoaded(true);
  }, [accountId]);
  useEffect(() => {
    load();
  }, [load]);

  const opt = explain?.options?.[variant] ?? null;
  const label = VARIANT_LABEL[variant] ?? variant;

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <ShieldCheck className="w-5 h-5 text-primary" /> 전략 요약
          {opt && <Badge className="bg-primary-50 text-primary-700">{opt.label ?? label}안</Badge>}
        </CardTitle>
        <button
          type="button"
          onClick={load}
          className="text-neutral-400 hover:text-neutral-600"
          aria-label="새로고침"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
      </CardHeader>
      <CardBody className="space-y-4">
        {!loaded ? (
          <p className="text-sm text-neutral-400">불러오는 중…</p>
        ) : !opt ? (
          <p className="text-sm text-neutral-500">
            아직 <b>{label}안</b>의 전략 설명이 없습니다. 잔고 동기화 후 배분안을 생성하면 표시됩니다.
            {err ? <span className="block text-xs text-neutral-400 mt-1">· {err}</span> : null}
          </p>
        ) : (
          <>
            {(opt.defensive_pct != null || opt.risk_pct != null) && (
              <div className="flex items-center gap-2 flex-wrap text-xs">
                {opt.defensive_pct != null && (
                  <Badge className="bg-neutral-100 text-neutral-600">방어자산 {opt.defensive_pct}%</Badge>
                )}
                {opt.risk_pct != null && (
                  <Badge className="bg-warning/10 text-warning">위험자산 {opt.risk_pct}%</Badge>
                )}
                {opt.rounds != null && opt.rounds > 0 && (
                  <span className="text-neutral-500">분할 {opt.rounds}회</span>
                )}
              </div>
            )}

            <section>
              <h4 className="text-sm font-semibold text-neutral-700 mb-1">핵심 전략</h4>
              <p className="text-sm text-neutral-700 leading-relaxed">{opt.summary}</p>
            </section>

            <section>
              <h4 className="text-sm font-semibold text-neutral-700 mb-1 flex items-center gap-1.5">
                <Users className="w-4 h-4 text-neutral-400" /> 적합 대상
              </h4>
              <p className="text-sm text-neutral-600 leading-relaxed">{opt.suitable_for}</p>
            </section>

            <section>
              <h4 className="text-sm font-semibold text-neutral-700 mb-1 flex items-center gap-1.5">
                <AlertTriangle className="w-4 h-4 text-warning" /> 주요 리스크
              </h4>
              {opt.key_risks?.length ? (
                <ul className="list-disc pl-5 space-y-1">
                  {opt.key_risks.map((r, i) => (
                    <li key={i} className="text-sm text-neutral-600 leading-relaxed">
                      {r}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-neutral-400">특이 리스크 없음.</p>
              )}
            </section>

            <section>
              <h4 className="text-sm font-semibold text-neutral-700 mb-1">리밸런싱 이유</h4>
              <p className="text-sm text-neutral-600 leading-relaxed">{opt.rebalance_reason}</p>
            </section>

            <p className="text-[11px] text-neutral-400">
              설명은 실제 배분 결과(규칙 기반)에서 생성됩니다. 가짜 숫자 없음 · 시장가 매수 없음 · 사람 승인 후에만 실행.
            </p>
          </>
        )}
      </CardBody>
    </Card>
  );
}
