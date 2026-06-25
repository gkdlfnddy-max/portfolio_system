"use client";

import { useCallback, useEffect, useState } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import {
  RefreshCw, Layers, Eye, ShieldAlert, Info, Target, ListChecks,
  TrendingUp, AlertTriangle, CheckCircle2, HelpCircle,
} from "lucide-react";

// ── 타입(느슨 — 백엔드 JSON 모양) ──
type AnalysisData = {
  ok: boolean;
  account_index: number;
  objective: any | null;
  objective_set: boolean;
  views: any[];
  variants: any | null;
  interpretations: any | null;
  decline_scan: any | null;
  requires_user_approval: boolean;
  auto_applied: boolean;
  auto_order_created: boolean;
};

// 6축 한글 라벨(백엔드 AXIS_LABELS 와 동일 — 미연동 축도 라벨 표기용).
const AXIS_LABELS: Record<string, string> = {
  technical: "기술",
  distribution: "분산",
  macro: "거시",
  event: "이벤트",
  sentiment: "심리",
  policy: "정책/규제",
};
const AXIS_ORDER = ["technical", "distribution", "macro", "event", "sentiment", "policy"];

const PERSP_BADGE: Record<string, string> = {
  A: "bg-primary-50 text-primary-700",
  B: "bg-success/10 text-success",
  C: "bg-warning/10 text-warning",
};

function pct(n: any): string {
  return typeof n === "number" ? `${n}%` : "—";
}
function conf(n: any): string {
  return typeof n === "number" ? `${(n * 100).toFixed(0)}%` : "미상";
}

// 항상 화면에 보이는 필수 안내(요구사항 §2).
function Disclaimer() {
  return (
    <div className="rounded-xl border border-neutral-200 bg-neutral-50 p-3 text-xs text-neutral-600 leading-relaxed flex gap-2">
      <Info className="w-4 h-4 shrink-0 text-neutral-400 mt-0.5" />
      <span>
        이 분석은 <b>투자 판단 보조</b>입니다 · <b>자동 주문 생성 안 함</b> ·{" "}
        <b>사용자 승인 전 policy 미반영</b> · <b>데이터 부족 축 제외</b> ·{" "}
        <b>confidence 낮으면 단정 안 함</b>.
      </span>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="text-sm text-neutral-500 text-center py-6">{children}</p>;
}

// ── 관점별 후보 카드(A/B/C) ──
function CandidateCard({ c }: { c: any }) {
  const p = String(c.perspective ?? "");
  const w = c.weights ?? {};
  return (
    <Card className="border-neutral-200">
      <CardHeader className="flex-row items-center justify-between gap-2">
        <CardTitle className="text-base flex items-center gap-2">
          <Badge className={PERSP_BADGE[p] ?? "bg-neutral-100 text-neutral-600"}>{p || "?"}</Badge>
          {c.label ?? "후보"}
        </CardTitle>
        {c.objective ? (
          <span className="text-xs text-neutral-400">{c.objective}</span>
        ) : null}
      </CardHeader>
      <CardBody className="space-y-3 text-sm">
        {c.summary ? <p className="text-neutral-700">{c.summary}</p> : null}

        {c.why_fits_user ? (
          <div>
            <div className="text-xs font-semibold text-neutral-500 mb-1">왜 내 관점에 맞는가</div>
            <p className="text-neutral-600 text-[13px] leading-relaxed">{c.why_fits_user}</p>
          </div>
        ) : null}

        {/* 비중 */}
        {w && Object.keys(w).length > 0 ? (
          <div className="grid grid-cols-3 gap-2 text-center">
            <div className="rounded-lg bg-neutral-50 py-2">
              <div className="text-[11px] text-neutral-400">방어(현금+국채)</div>
              <div className="font-semibold text-neutral-800">{pct(w.defensive)}</div>
            </div>
            <div className="rounded-lg bg-neutral-50 py-2">
              <div className="text-[11px] text-neutral-400">위험자산</div>
              <div className="font-semibold text-neutral-800">{pct(w.risk_assets)}</div>
            </div>
            <div className="rounded-lg bg-neutral-50 py-2">
              <div className="text-[11px] text-neutral-400">헤지</div>
              <div className="font-semibold text-neutral-800">{pct(w.hedge)}</div>
            </div>
          </div>
        ) : null}

        {/* 장점 / 위험 / 깨지는 조건 / 추가확인 */}
        {Array.isArray(c.pros) && c.pros.length > 0 ? (
          <BulletList icon={<CheckCircle2 className="w-3.5 h-3.5 text-success" />} title="장점" items={c.pros} />
        ) : null}
        {Array.isArray(c.risks) && c.risks.length > 0 ? (
          <BulletList icon={<AlertTriangle className="w-3.5 h-3.5 text-warning" />} title="위험" items={c.risks} />
        ) : null}
        {Array.isArray(c.break_triggers) && c.break_triggers.length > 0 ? (
          <BulletList icon={<ShieldAlert className="w-3.5 h-3.5 text-error" />} title="언제 이 안이 깨지는가" items={c.break_triggers} />
        ) : null}
        {Array.isArray(c.more_to_confirm) && c.more_to_confirm.length > 0 ? (
          <BulletList icon={<HelpCircle className="w-3.5 h-3.5 text-neutral-400" />} title="추가로 확인할 자료" items={c.more_to_confirm} />
        ) : null}

        <p className="text-[11px] text-neutral-400 pt-1">사용자 승인 전 미반영 · 자동 주문 없음</p>
      </CardBody>
    </Card>
  );
}

function BulletList({ icon, title, items }: { icon: React.ReactNode; title: string; items: string[] }) {
  return (
    <div>
      <div className="text-xs font-semibold text-neutral-500 mb-1 flex items-center gap-1">
        {icon} {title}
      </div>
      <ul className="space-y-0.5">
        {items.map((it, i) => (
          <li key={i} className="text-[13px] text-neutral-600 leading-relaxed pl-3 relative">
            <span className="absolute left-0 text-neutral-300">·</span>
            {it}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function PerspectiveAnalysis({ accountId }: { accountId: number }) {
  const [data, setData] = useState<AnalysisData | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await fetch(`/api/accounts/${accountId}/analysis`, { cache: "no-store" });
      if (!r.ok) {
        if (r.status === 401) setErr("로그인이 필요합니다.");
        else if (r.status === 403) setErr("이 계좌에 대한 접근 권한이 없습니다.");
        else setErr("분석을 불러오지 못했습니다.");
        setData(null);
      } else {
        const j = (await r.json()) as AnalysisData;
        setData(j?.ok ? j : null);
        if (!j?.ok) setErr("분석 데이터가 없습니다.");
      }
    } catch {
      setErr("분석을 불러오지 못했습니다.");
      setData(null);
    }
    setBusy(false);
    setLoaded(true);
  }, [accountId]);

  useEffect(() => { void load(); }, [load]);

  // ── 6축 가용/부족 집계(스캔된 모든 종목에서 어느 축이라도 가용이면 가용) ──
  const scan = data?.decline_scan ?? null;
  const scanned: any[] = Array.isArray(scan?.scanned) ? scan.scanned : [];
  const okScanned = scanned.filter((s) => s?.ok && s?.composite);

  // 축별 가용성 + confidence(가용 종목 평균) + 주요 신호(발화 signal 이름)를 집계.
  const axisAvailable: Record<string, boolean> = {};
  const axisConfVals: Record<string, number[]> = {};
  const axisSignals: Record<string, Set<string>> = {};
  for (const ax of AXIS_ORDER) {
    axisAvailable[ax] = false;
    axisConfVals[ax] = [];
    axisSignals[ax] = new Set<string>();
  }
  for (const s of okScanned) {
    const axes = s.composite?.axes ?? {};
    for (const ax of AXIS_ORDER) {
      const a = axes[ax];
      if (a?.data_available) {
        axisAvailable[ax] = true;
        if (typeof a.confidence === "number") axisConfVals[ax].push(a.confidence);
        for (const sig of Array.isArray(a.signals) ? a.signals : []) {
          if (sig?.fired && sig?.name) axisSignals[ax].add(String(sig.name));
        }
      }
    }
  }
  const axisConf: Record<string, number | null> = {};
  for (const ax of AXIS_ORDER) {
    const v = axisConfVals[ax];
    axisConf[ax] = v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
  }
  const anyComposite = okScanned.length > 0;

  // 계좌 overall confidence(가용 종목 평균) — 백엔드가 per-종목으로 줌.
  const confVals = okScanned
    .map((s) => s.overall_confidence)
    .filter((x): x is number => typeof x === "number");
  const overallConf = confVals.length
    ? confVals.reduce((a, b) => a + b, 0) / confVals.length
    : null;

  // 종합 위험(holistic_risk) — 가용 종목 평균. confidence 낮으면 단정 회피용.
  const riskVals = okScanned
    .map((s) => s.holistic_risk)
    .filter((x): x is number => typeof x === "number");
  const holisticRisk = riskVals.length
    ? Math.round((riskVals.reduce((a, b) => a + b, 0) / riskVals.length) * 10) / 10
    : null;

  const missingAxes = AXIS_ORDER.filter((ax) => !axisAvailable[ax]).map((ax) => AXIS_LABELS[ax]);

  // 포트폴리오 영향(읽기 전용·단정 금지) — holistic_risk + confidence 로 톤 결정.
  const portfolioImpact: string[] = [];
  if (anyComposite) {
    if (holisticRisk == null || overallConf == null) {
      portfolioImpact.push("종목 6축 종합을 낼 데이터가 부족 — 단정 없이 관망/추가확인(정직).");
    } else if (overallConf < 0.3) {
      portfolioImpact.push(`종합 위험 ${holisticRisk}(가용 종목 평균)이나 신뢰도 ${(overallConf * 100).toFixed(0)}%로 낮음 — 단정 금지, 관망/주의·데이터 추가 필요.`);
    } else if (holisticRisk >= 35) {
      portfolioImpact.push(`종합 위험 ${holisticRisk}(신뢰 ${(overallConf * 100).toFixed(0)}%) — 방어(현금/채권) 비중 점검·진입 속도 조절 검토(후보, 사람 승인).`);
    } else {
      portfolioImpact.push(`종합 위험 ${holisticRisk}(신뢰 ${(overallConf * 100).toFixed(0)}%) — 현 운용기준 유지 가능(관망도 정상).`);
    }
    if (missingAxes.length > 0) {
      portfolioImpact.push(`미연동 축(${missingAxes.join(", ")})은 분석에서 제외 — "모든 데이터를 고려했다"고 말하지 않음.`);
    }
  }

  const proposal = scan?.proposal ?? null; // 보수적 전환 후보(없으면 null)
  const summary = scan?.summary ?? null;

  const interp = data?.interpretations ?? null;
  const common = interp?.common_facts ?? null;
  const userPersp = interp?.user_perspective ?? null;
  const interpretations: any[] = Array.isArray(interp?.interpretations) ? interp.interpretations : [];

  const candidates: any[] = Array.isArray(data?.variants?.candidates) ? data!.variants.candidates : [];
  const draftSaved = data?.variants?.draft_rows_saved ?? 0;

  return (
    <div className="space-y-6">
      <Disclaimer />

      <div className="flex justify-end">
        <Button size="sm" variant="outline" onClick={() => void load()} disabled={busy}>
          <RefreshCw className={`w-4 h-4 ${busy ? "animate-spin" : ""}`} /> 다시 분석
        </Button>
      </div>

      {!loaded ? (
        <Empty>불러오는 중…</Empty>
      ) : err ? (
        <Card><CardBody><Empty>{err}</Empty></CardBody></Card>
      ) : !data ? (
        <Card><CardBody><Empty>분석 데이터가 없습니다.</Empty></CardBody></Card>
      ) : (
        <>
          {/* ── 내 투자 목적/성향 + 내 관점 ── */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Target className="w-5 h-5 text-primary" /> 내 투자 목적 · 관점
              </CardTitle>
            </CardHeader>
            <CardBody className="space-y-3 text-sm">
              <div>
                <span className="text-xs font-semibold text-neutral-500">투자 목적/성향: </span>
                {data.objective_set && data.objective ? (
                  <span className="text-neutral-700">
                    {data.objective.investment_goal ?? data.objective.label ?? "설정됨"}
                  </span>
                ) : (
                  <span className="text-neutral-500">
                    미설정 — 목적을 입력하면 후보가 더 정교해집니다 (견해만으로 분석 중).
                  </span>
                )}
              </div>
              <div>
                <div className="text-xs font-semibold text-neutral-500 mb-1">
                  내 관점(견해) {data.views.length}건
                </div>
                {data.views.length === 0 ? (
                  <p className="text-neutral-500 text-[13px]">
                    등록된 견해가 없습니다. <b>내 투자 견해</b>에서 입력하면 분석에 반영됩니다.
                  </p>
                ) : (
                  <div className="flex flex-wrap gap-1.5">
                    {data.views.slice(0, 12).map((v: any) => (
                      <Badge key={v.id} className="bg-neutral-100 text-neutral-600">
                        {v.theme ?? v.ticker ?? v.etf ?? "견해"}
                        {v.stance === "positive" ? " ↑" : v.stance === "negative" ? " ↓" : ""}
                      </Badge>
                    ))}
                  </div>
                )}
              </div>
            </CardBody>
          </Card>

          {/* ── 공통 사실 + 내 관점 해석 ── */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Eye className="w-5 h-5 text-primary" /> 같은 데이터, 다른 해석
              </CardTitle>
            </CardHeader>
            <CardBody className="space-y-4 text-sm">
              {!interp ? (
                <Empty>해석 데이터가 없습니다 (DB 미연동).</Empty>
              ) : (
                <>
                  {common ? (
                    <div className="rounded-xl bg-neutral-50 p-3">
                      <div className="text-xs font-semibold text-neutral-500 mb-1">공통 사실(관점 무관 측정값)</div>
                      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[13px] text-neutral-700">
                        <span>종목 {common.instruments ?? 0}</span>
                        <span>보유 {common.held ?? 0}</span>
                        <span>견해 {common.user_views ?? 0}</span>
                        <span>근거자료 {common.evidence_items ?? 0}</span>
                      </div>
                      {common.note ? <p className="text-[11px] text-neutral-400 mt-1">{common.note}</p> : null}
                    </div>
                  ) : null}

                  {userPersp ? (
                    <div>
                      <div className="text-xs font-semibold text-neutral-500 mb-1">내 관점 해석</div>
                      <p className="text-[13px] text-neutral-600 leading-relaxed">
                        {userPersp.objective_note ?? "목적/견해 기준으로 해석합니다."}
                      </p>
                    </div>
                  ) : null}

                  {interpretations.length > 0 ? (
                    <div className="space-y-1.5">
                      <div className="text-xs font-semibold text-neutral-500">종목별 관점 정합</div>
                      {interpretations.slice(0, 8).map((it: any, i: number) => (
                        <div key={i} className="text-[13px] text-neutral-600 border-l-2 border-neutral-200 pl-2">
                          <b>{it.instrument_code}</b>{" "}
                          <span className="text-neutral-400">[{it.alignment}]</span>
                          {it.reading ? <> — {it.reading}</> : null}
                        </div>
                      ))}
                    </div>
                  ) : null}
                </>
              )}
            </CardBody>
          </Card>

          {/* ── 관점별 후보 A/B/C ── */}
          <div>
            <div className="flex items-center gap-2 mb-3">
              <Layers className="w-5 h-5 text-primary" />
              <h2 className="text-lg font-semibold text-neutral-900">관점별 후보 (A / B / C)</h2>
            </div>
            {candidates.length === 0 ? (
              <Card><CardBody><Empty>후보를 생성할 데이터가 부족합니다 (견해/목적 입력 후 다시 시도).</Empty></CardBody></Card>
            ) : (
              <div className="space-y-3">
                {candidates.map((c: any, i: number) => <CandidateCard key={c.perspective ?? i} c={c} />)}
                <p className="text-[11px] text-neutral-400">
                  하나의 정답이 아닌 비교 후보입니다. 선택·승인 전에는 policy 에 미반영됩니다
                  {draftSaved ? "" : " (이 조회는 draft 도 저장하지 않음)"}.
                </p>
              </div>
            )}
          </div>

          {/* ── 하락 징후 6축 ── */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShieldAlert className="w-5 h-5 text-primary" /> 하락 징후 6축
              </CardTitle>
            </CardHeader>
            <CardBody className="space-y-4 text-sm">
              {!scan ? (
                <Empty>스캔 데이터가 없습니다 (DB 미연동).</Empty>
              ) : !anyComposite ? (
                <Empty>
                  6축 종합을 계산할 데이터가 부족합니다. 관심·보유 종목의 가격/지표가 연동되면 표시됩니다.
                </Empty>
              ) : (
                <>
                  {/* overall confidence + 종합 위험(holistic_risk) */}
                  <div className="rounded-xl bg-neutral-50 p-3 flex items-center justify-between gap-2 flex-wrap">
                    <span className="text-xs font-semibold text-neutral-500">종합 신뢰도(overall confidence)</span>
                    <div className="flex items-center gap-2">
                      {holisticRisk != null ? (
                        <Badge className="bg-neutral-200 text-neutral-700">종합 위험 {holisticRisk}</Badge>
                      ) : null}
                      <Badge
                        className={
                          overallConf != null && overallConf >= 0.6
                            ? "bg-success/10 text-success"
                            : overallConf != null && overallConf >= 0.3
                            ? "bg-warning/10 text-warning"
                            : "bg-neutral-200 text-neutral-600"
                        }
                      >
                        {conf(overallConf)}
                      </Badge>
                    </div>
                  </div>
                  {overallConf != null && overallConf < 0.3 ? (
                    <p className="text-[12px] text-neutral-500">
                      신뢰도가 낮아 단정하지 않습니다 — 관망/주의 + 데이터 추가 필요.
                    </p>
                  ) : null}

                  {/* 6축 가용/부족 그리드 — 축별 연동/미연동 · confidence · 주요 신호 */}
                  <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                    {AXIS_ORDER.map((ax) => {
                      const ok = axisAvailable[ax];
                      const c = axisConf[ax];
                      const sigs = Array.from(axisSignals[ax]).slice(0, 2);
                      return (
                        <div
                          key={ax}
                          className={`rounded-lg p-2.5 text-center border ${
                            ok ? "border-success/30 bg-success/5" : "border-neutral-200 bg-neutral-50"
                          }`}
                        >
                          <div className="text-[13px] font-medium text-neutral-800">{AXIS_LABELS[ax]}</div>
                          <div className={`text-[11px] mt-0.5 ${ok ? "text-success" : "text-neutral-400"}`}>
                            {ok ? `연동됨${c != null ? ` · 신뢰 ${(c * 100).toFixed(0)}%` : ""}` : "미연동(데이터 없음 — 제외)"}
                          </div>
                          {ok && sigs.length > 0 ? (
                            <div className="text-[10px] text-neutral-500 mt-1 leading-snug">{sigs.join(", ")}</div>
                          ) : null}
                        </div>
                      );
                    })}
                  </div>

                  {/* 미연동 축 명시 + 포트폴리오 영향(읽기 전용·단정 금지) */}
                  {missingAxes.length > 0 ? (
                    <p className="text-[11px] text-neutral-400">
                      미연동 축({missingAxes.join(", ")})은 분석에서 제외했습니다 — "모든 데이터를 고려했다"고 말하지 않습니다.
                    </p>
                  ) : null}
                  {portfolioImpact.length > 0 ? (
                    <div className="rounded-xl border border-neutral-200 p-3 space-y-1">
                      <div className="text-xs font-semibold text-neutral-500">포트폴리오 영향(읽기 전용)</div>
                      {portfolioImpact.map((t, i) => (
                        <p key={i} className="text-[13px] text-neutral-700">· {t}</p>
                      ))}
                    </div>
                  ) : null}

                  {summary ? (
                    <p className="text-[12px] text-neutral-500">
                      분석 {summary.analyzed ?? 0}/{summary.total ?? 0}종목
                      {typeof summary.skipped_no_data === "number" && summary.skipped_no_data > 0
                        ? ` · 데이터 부족 ${summary.skipped_no_data}종목 제외`
                        : ""}
                      {typeof summary.high_risk_count === "number"
                        ? ` · 고위험 ${summary.high_risk_count}종목`
                        : ""}
                    </p>
                  ) : null}

                  {/* 보수적 전환 후보 / policy draft 여부 */}
                  <div className="rounded-xl border border-neutral-200 p-3">
                    <div className="text-xs font-semibold text-neutral-500 mb-1 flex items-center gap-1">
                      <TrendingUp className="w-3.5 h-3.5" /> 보수적 전환 후보
                    </div>
                    {proposal ? (
                      <div className="text-[13px] text-neutral-700 space-y-1">
                        <p>{proposal.note ?? proposal.stance ?? "보수적 전환 후보가 제시되었습니다."}</p>
                        <Badge className="bg-warning/10 text-warning">
                          policy draft {proposal.policy_draft_created || draftSaved ? "생성됨(미반영)" : "미생성"}
                        </Badge>
                      </div>
                    ) : (
                      <p className="text-[13px] text-neutral-500">
                        현재 보수적 전환 후보 없음(관망). policy draft 미생성.
                      </p>
                    )}
                    <p className="text-[11px] text-neutral-400 mt-1">
                      후보일 뿐 — 사용자 승인 전 policy 미반영 · 자동 주문 없음.
                    </p>
                  </div>
                </>
              )}
            </CardBody>
          </Card>

          {/* 하단 요약 배지 — 미반영/무자동 명시 */}
          <div className="flex flex-wrap items-center gap-2 text-[11px] text-neutral-500">
            <Badge className="bg-neutral-100 text-neutral-600 flex items-center gap-1">
              <ListChecks className="w-3 h-3" /> 사용자 승인 전 미반영
            </Badge>
            <Badge className="bg-neutral-100 text-neutral-600">자동 적용 없음</Badge>
            <Badge className="bg-neutral-100 text-neutral-600">자동 주문 없음</Badge>
          </div>
        </>
      )}
    </div>
  );
}
