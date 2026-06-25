"use client";

// 조언 적용/무시 이력 타임라인 — 대전제·중전제 필드 조언에 사람이 어떻게 결정했는지(적용/수정/무시/저장).
// 데이터는 /advice-history API(운영 DB: field_advice_events + field_consultations) 만 사용. mock 없음.
// 정직 원칙: 이력이 없으면 가짜 항목 없이 빈 안내. evidence/lesson 사용 이력도 DB 카운트만 표시.
import { useEffect, useState, useCallback } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { MessageSquareText, RefreshCw, Check, X, Pencil, Save } from "lucide-react";

type AdviceEvent = {
  id: number;
  field_consultation_id: number | null;
  field_name: string | null;
  user_action: string; // applied|edited|ignored|saved
  action_label: string;
  kept: boolean;
  detail: string | null;
  agent_name: string | null;
  advice_type: string | null;
  evidence_count: number;
  lesson_count: number;
  suggested_text: string | null;
  policy_version: number | null; // 이 결정 시점에 활성이던 정책 버전
  created_at: string;
};
type AdviceHistory = {
  ok: boolean;
  account_index: number;
  events: AdviceEvent[];
  counts: { applied: number; edited: number; ignored: number; saved: number; kept_total: number; ignored_total: number };
  policy_version_current: number | null;
  point_count: number;
  note: string | null;
};

const FIELD_LABELS: Record<string, string> = {
  interests: "관심 분야", views: "견해/생각", region: "지역 배분", defensive: "방어자산",
  pace: "리밸런싱 속도", whole: "전체 정리",
};

const ACTION_STYLE: Record<string, { cls: string; Icon: typeof Check }> = {
  applied: { cls: "bg-success/10 text-success", Icon: Check },
  edited: { cls: "bg-primary-50 text-primary-700", Icon: Pencil },
  saved: { cls: "bg-sky-50 text-sky-700", Icon: Save },
  ignored: { cls: "bg-neutral-100 text-neutral-500", Icon: X },
};

function fmtDate(s: string): string {
  const t = Date.parse(s.replace(" ", "T"));
  if (Number.isNaN(t)) return s;
  return new Date(t).toLocaleDateString("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function AdviceHistoryCard({ accountId }: { accountId: number }) {
  const [h, setH] = useState<AdviceHistory | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const r = await fetch(`/api/accounts/${accountId}/advice-history?limit=50`, { cache: "no-store" });
      const j = await r.json();
      setH(j?.advice && j.advice.ok ? j.advice : null);
    } catch { setH(null); }
    setLoaded(true);
    setBusy(false);
  }, [accountId]);
  useEffect(() => { load(); }, [load]);

  const hasData = h && h.events.length > 0;

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <MessageSquareText className="w-5 h-5 text-primary" /> 조언 적용 / 무시 이력
        </CardTitle>
        <button onClick={load} disabled={busy} className="text-xs text-neutral-500 inline-flex items-center gap-1 hover:text-neutral-700 disabled:opacity-50">
          <RefreshCw className={`w-3.5 h-3.5 ${busy ? "animate-spin" : ""}`} /> 새로고침
        </button>
      </CardHeader>
      <CardBody className="space-y-4">
        {!loaded ? (
          <p className="text-sm text-neutral-400">불러오는 중…</p>
        ) : !hasData ? (
          <p className="text-sm text-neutral-500">
            아직 조언 적용/무시 이력이 없습니다 — 대전제·중전제 조언을 적용하거나 보류하면 여기에 타임라인으로 쌓입니다.
          </p>
        ) : (
          <>
            {/* 요약: 반영 vs 무시 */}
            <div className="flex flex-wrap gap-2 text-xs">
              <Badge className="bg-success/10 text-success">반영 {h!.counts.kept_total}</Badge>
              <Badge className="bg-neutral-100 text-neutral-500">무시 {h!.counts.ignored_total}</Badge>
              <span className="text-neutral-400 self-center">
                (적용 {h!.counts.applied} · 수정 {h!.counts.edited} · 저장 {h!.counts.saved})
              </span>
              {h!.policy_version_current != null && (
                <Badge className="bg-primary-50 text-primary-700 ml-auto">현재 정책 v{h!.policy_version_current}</Badge>
              )}
            </div>

            {/* 타임라인 */}
            <ul className="space-y-2">
              {h!.events.map((e) => {
                const st = ACTION_STYLE[e.user_action] ?? ACTION_STYLE.ignored;
                const Icon = st.Icon;
                const field = e.field_name ? (FIELD_LABELS[e.field_name] ?? e.field_name) : "조언";
                return (
                  <li key={e.id} className="flex items-start gap-2 border-l-2 border-neutral-100 pl-3 py-0.5">
                    <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] ${st.cls}`}>
                      <Icon className="w-3 h-3" /> {e.action_label}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="text-sm text-neutral-800">
                        <b>{field}</b>
                        {e.agent_name && <span className="text-[11px] text-neutral-400 ml-1">· {e.agent_name}</span>}
                      </div>
                      {(e.detail || e.suggested_text) && (
                        <p className="text-xs text-neutral-500 truncate">{e.detail || e.suggested_text}</p>
                      )}
                      <div className="flex flex-wrap gap-x-2 text-[10px] text-neutral-400 mt-0.5">
                        <span>{fmtDate(e.created_at)}</span>
                        {e.policy_version != null && <span>정책 v{e.policy_version}</span>}
                        {e.evidence_count > 0 && <span>근거(evidence) {e.evidence_count}</span>}
                        {e.lesson_count > 0 && <span>회귀/메모리(lesson) {e.lesson_count}</span>}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          </>
        )}
        <p className="text-[11px] text-neutral-400">
          사람의 결정(적용·수정·무시·저장)만 기록합니다. evidence/lesson 카운트는 그 조언이 참고한 근거·메모리 사용 이력입니다(운영 DB).
        </p>
      </CardBody>
    </Card>
  );
}
