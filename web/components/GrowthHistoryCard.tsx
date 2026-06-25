"use client";

// 성장 이력 — "에이전트가 메모리로 어떻게 성장했는가"의 추적.
// 4계층: 자료조사 근거(evidence) · 교훈 후보(lesson candidate) · 승격된 공통 교훈(promoted, 익명) · 회귀테스트(regression).
// 데이터는 /growth-history API(운영 DB) 만 사용. mock 없음. promoted lesson 은 익명화된 공통만(개인/계좌 0).
// 정직 원칙: 데이터 없으면 가짜 항목 없이 빈 안내.
import { useEffect, useState, useCallback } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Sprout, RefreshCw, FileSearch, Lightbulb, Share2, ShieldCheck } from "lucide-react";

type Evidence = {
  id: number;
  source_type: string | null;
  theme: string | null;
  topic: string | null;
  summary: string;
  stance: string | null;
  stance_label: string | null;
  base_confidence: number | null;
  eff_confidence: number | null;
  age_days: number | null;
  freshness_at: string | null;
};
type LessonCandidate = {
  id: number; scope: string | null; ref: string | null; title: string;
  observed_count: number | null; confidence: number | null; status: string | null; created_at: string;
};
type PromotedLesson = {
  id: number; scope_type: string | null; agent_name: string | null;
  theme: string | null; sector: string | null; title: string; body: string; confidence: number | null;
};
type Regression = {
  id: number; task_type: string; title: string; expect: string | null; status: string | null; created_at: string;
};
type Growth = {
  ok: boolean;
  account_index: number;
  evidence: Evidence[];
  lesson_candidates: LessonCandidate[];
  promoted_lessons: PromotedLesson[];
  regression_promotions: Regression[];
  counts: { evidence: number; lesson_candidates: number; promoted_lessons: number; regression: number };
  anonymized: boolean;
  point_count: number;
  note: string | null;
};

// stance → 색. 외부 자료를 매수/매도로 단정하지 않는 "입장" 태깅이므로 중립톤 위주.
const STANCE_STYLE: Record<string, string> = {
  long_support: "bg-success/10 text-success",
  short_support: "bg-error/10 text-error",
  hedge_support: "bg-amber-50 text-amber-700",
  risk_warning: "bg-error/10 text-error",
  watch_only: "bg-neutral-100 text-neutral-500",
  insufficient_evidence: "bg-neutral-100 text-neutral-400",
  conflicting_evidence: "bg-neutral-100 text-neutral-500",
};

function fmtConf(v: number | null): string {
  if (v == null) return "—";
  return Math.round(v * 100) + "%";
}
function fmtAge(d: number | null): string {
  if (d == null) return "";
  if (d < 1) return "오늘";
  return `${Math.round(d)}일 전`;
}

export function GrowthHistoryCard({ accountId }: { accountId: number }) {
  const [g, setG] = useState<Growth | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const r = await fetch(`/api/accounts/${accountId}/growth-history?limit=20`, { cache: "no-store" });
      const j = await r.json();
      setG(j?.growth && j.growth.ok ? j.growth : null);
    } catch { setG(null); }
    setLoaded(true);
    setBusy(false);
  }, [accountId]);
  useEffect(() => { load(); }, [load]);

  const hasData = g && g.point_count > 0;

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <Sprout className="w-5 h-5 text-primary" /> 성장 이력 (근거 · 교훈 · 회귀)
        </CardTitle>
        <button onClick={load} disabled={busy} className="text-xs text-neutral-500 inline-flex items-center gap-1 hover:text-neutral-700 disabled:opacity-50">
          <RefreshCw className={`w-3.5 h-3.5 ${busy ? "animate-spin" : ""}`} /> 새로고침
        </button>
      </CardHeader>
      <CardBody className="space-y-5">
        {!loaded ? (
          <p className="text-sm text-neutral-400">불러오는 중…</p>
        ) : !hasData ? (
          <p className="text-sm text-neutral-500">
            아직 성장 이력이 없습니다 — 자료조사 근거(evidence)·교훈 후보·승격된 공통 교훈·회귀테스트가 쌓이면 여기에 표시됩니다.
          </p>
        ) : (
          <>
            {/* 요약 카운트 */}
            <div className="flex flex-wrap gap-2 text-xs">
              <Badge className="bg-primary-50 text-primary-700">근거 {g!.counts.evidence}</Badge>
              <Badge className="bg-amber-50 text-amber-700">교훈 후보 {g!.counts.lesson_candidates}</Badge>
              <Badge className="bg-success/10 text-success">승격 교훈 {g!.counts.promoted_lessons}</Badge>
              <Badge className="bg-neutral-100 text-neutral-600">회귀 {g!.counts.regression}</Badge>
            </div>

            {/* 1) 최근 evidence — stance / freshness / confidence */}
            {g!.evidence.length > 0 && (
              <section>
                <div className="flex items-center gap-1.5 mb-1.5 text-sm font-medium text-neutral-700">
                  <FileSearch className="w-4 h-4 text-neutral-400" /> 최근 자료조사 근거
                </div>
                <ul className="space-y-2">
                  {g!.evidence.slice(0, 8).map((e) => (
                    <li key={e.id} className="border-l-2 border-neutral-100 pl-3">
                      <div className="flex items-center gap-2 flex-wrap">
                        {e.stance && (
                          <span className={`px-1.5 py-0.5 rounded text-[11px] ${STANCE_STYLE[e.stance] ?? "bg-neutral-100 text-neutral-500"}`}>
                            {e.stance_label ?? e.stance}
                          </span>
                        )}
                        <span className="text-sm text-neutral-800">{e.theme || e.topic || e.source_type || "근거"}</span>
                      </div>
                      {e.summary && <p className="text-xs text-neutral-500 truncate">{e.summary}</p>}
                      <div className="flex flex-wrap gap-x-3 text-[10px] text-neutral-400 mt-0.5">
                        <span>신뢰도(현재) {fmtConf(e.eff_confidence)}</span>
                        {e.base_confidence != null && <span>기준 {fmtConf(e.base_confidence)}</span>}
                        {e.age_days != null && <span>{fmtAge(e.age_days)}</span>}
                        {e.source_type && <span>{e.source_type}</span>}
                      </div>
                    </li>
                  ))}
                </ul>
                <p className="text-[10px] text-neutral-400 mt-1">
                  근거의 신뢰도는 시간이 지나면 자동 감쇠(freshness decay)합니다 — 외부 자료를 매수/매도로 단정하지 않고 "입장(stance)"만 태깅합니다.
                </p>
              </section>
            )}

            {/* 2) 교훈 후보 — 승격 전 관찰 */}
            {g!.lesson_candidates.length > 0 && (
              <section>
                <div className="flex items-center gap-1.5 mb-1.5 text-sm font-medium text-neutral-700">
                  <Lightbulb className="w-4 h-4 text-neutral-400" /> 교훈 후보 (승격 전 관찰)
                </div>
                <ul className="space-y-1.5">
                  {g!.lesson_candidates.slice(0, 8).map((l) => (
                    <li key={l.id} className="flex items-start gap-2 text-sm">
                      <Badge className="bg-amber-50 text-amber-700 text-[10px]">{l.scope ?? "?"}</Badge>
                      <div className="min-w-0 flex-1">
                        <span className="text-neutral-800">{l.title}</span>
                        <span className="ml-2 text-[10px] text-neutral-400">
                          관찰 {l.observed_count ?? 0}회 · 신뢰도 {fmtConf(l.confidence)} · {l.status ?? "candidate"}
                        </span>
                      </div>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {/* 3) 승격된 공통 교훈 — 익명화(개인/계좌 식별정보 0) */}
            {g!.promoted_lessons.length > 0 && (
              <section>
                <div className="flex items-center gap-1.5 mb-1.5 text-sm font-medium text-neutral-700">
                  <Share2 className="w-4 h-4 text-neutral-400" /> 승격된 공통 교훈
                  <Badge className="bg-success/10 text-success text-[10px]">익명화</Badge>
                </div>
                <ul className="space-y-1.5">
                  {g!.promoted_lessons.slice(0, 8).map((p) => (
                    <li key={p.id} className="border-l-2 border-success/30 pl-3">
                      <div className="text-sm text-neutral-800">{p.title}</div>
                      {p.body && <p className="text-xs text-neutral-500 truncate">{p.body}</p>}
                      <div className="flex flex-wrap gap-x-3 text-[10px] text-neutral-400 mt-0.5">
                        {p.agent_name && <span>{p.agent_name}</span>}
                        {p.theme && <span>{p.theme}</span>}
                        <span>신뢰도 {fmtConf(p.confidence)}</span>
                      </div>
                    </li>
                  ))}
                </ul>
                <p className="text-[10px] text-neutral-400 mt-1">
                  계좌 간 공통으로 재사용되는 일반화 교훈입니다. 개인·계좌 식별정보는 승격 시 제거(익명화)됩니다.
                </p>
              </section>
            )}

            {/* 4) 회귀테스트 승격 — 실패→재발방지(시스템 학습) */}
            {g!.regression_promotions.length > 0 && (
              <section>
                <div className="flex items-center gap-1.5 mb-1.5 text-sm font-medium text-neutral-700">
                  <ShieldCheck className="w-4 h-4 text-neutral-400" /> 회귀테스트 승격 (재발 방지)
                </div>
                <ul className="space-y-1.5">
                  {g!.regression_promotions.slice(0, 8).map((r) => (
                    <li key={r.id} className="flex items-start gap-2 text-sm">
                      <Badge className="bg-neutral-100 text-neutral-600 text-[10px]">{r.task_type}</Badge>
                      <div className="min-w-0 flex-1">
                        <span className="text-neutral-800">{r.title}</span>
                        {r.expect && <span className="ml-2 text-[10px] text-neutral-400">기대: {r.expect}</span>}
                      </div>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {g!.note && <p className="text-[11px] text-neutral-400">※ {g!.note}</p>}
          </>
        )}
        <p className="text-[11px] text-neutral-400">
          에이전트가 메모리로 성장한 흔적입니다 — 전부 운영 DB 기록이며 가짜 데이터는 없습니다. 승격된 공통 교훈은 익명화되어 개인·계좌 정보가 포함되지 않습니다.
        </p>
      </CardBody>
    </Card>
  );
}
