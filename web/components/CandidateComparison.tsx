// 후보 비교 — **재사용 컴포넌트**(Agent 5 개선 3). 종목/ETF/국채/인버스를 하나의 표로 통합.
// 백엔드 SSOT(CandidateEvaluation = candidate.py normalized)를 그대로 소비한다.
// 표시: 후보명 · 역할 · 추천강도 · 데이터품질 · 계좌/확정안 적합성 · 편입/제외 사유 · 승인필요.
// 원칙: 자동 적용 없음(approval_required 항상 표시) · 미정 비중 None · 가짜 강추 없음(강도=관망).
import type { CandidateEvaluation } from "@/lib/portfolio/types";

const TYPE_LABEL: Record<string, string> = {
  etf: "ETF", stock: "개별주", treasury: "국채", inverse: "인버스(헤지)",
};

const STRENGTH: Record<string, { label: string; cls: string }> = {
  moderate: { label: "비교적 강한 제안", cls: "bg-emerald-100 text-emerald-800" },
  weak: { label: "약한 제안", cls: "bg-amber-100 text-amber-800" },
  watch: { label: "관망/주의", cls: "bg-slate-100 text-slate-600" },
};

function StrengthBadge({ level }: { level?: string }) {
  const s = STRENGTH[level ?? "watch"] ?? STRENGTH.watch;
  return <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${s.cls}`}>{s.label}</span>;
}

function DataQualityBadge({ dq }: { dq: CandidateEvaluation["data_quality"] }) {
  const ok = !!dq?.available;
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs ${ok ? "bg-sky-100 text-sky-800" : "bg-slate-100 text-slate-500"}`}>
      {ok ? `연결됨${dq?.level ? `·${dq.level}` : ""}` : "미연동"}
    </span>
  );
}

function pct(v: number | null): string {
  return v === null || v === undefined ? "—" : `${v}%`;
}

export default function CandidateComparison({
  candidates,
  title,
  emptyNote = "후보 없음(데이터 부족이면 정직하게 비움).",
}: {
  candidates: CandidateEvaluation[];
  title?: string;
  emptyNote?: string;
}) {
  if (!candidates || candidates.length === 0) {
    return <p className="text-sm text-slate-500">{emptyNote}</p>;
  }
  return (
    <div className="overflow-x-auto">
      {title && <h4 className="mb-2 text-sm font-semibold text-slate-700">{title}</h4>}
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b text-left text-xs text-slate-500">
            <th className="py-1 pr-3">후보</th>
            <th className="py-1 pr-3">역할</th>
            <th className="py-1 pr-3">추천 강도</th>
            <th className="py-1 pr-3">데이터 품질</th>
            <th className="py-1 pr-3">신뢰도</th>
            <th className="py-1 pr-3">제안 비중</th>
            <th className="py-1 pr-3">사유</th>
            <th className="py-1 pr-3">승인</th>
          </tr>
        </thead>
        <tbody>
          {candidates.map((c) => {
            const reason = c.reason_to_include || c.reason_to_exclude || "";
            return (
              <tr key={`${c.candidate_type}:${c.candidate_id}`} className="border-b align-top">
                <td className="py-1.5 pr-3">
                  <div className="font-medium text-slate-800">{c.display_name || c.candidate_id}</div>
                  <div className="text-xs text-slate-400">{c.candidate_id}</div>
                </td>
                <td className="py-1.5 pr-3 text-slate-600">
                  {TYPE_LABEL[c.candidate_type] ?? c.candidate_type}
                  {c.bucket ? <span className="text-slate-400"> · {c.bucket}</span> : null}
                </td>
                <td className="py-1.5 pr-3"><StrengthBadge level={c.recommendation_strength?.level} /></td>
                <td className="py-1.5 pr-3"><DataQualityBadge dq={c.data_quality} /></td>
                <td className="py-1.5 pr-3 text-slate-600">{(c.confidence ?? 0).toFixed(2)}</td>
                <td className="py-1.5 pr-3 text-slate-600">{pct(c.suggested_weight)}</td>
                <td className="py-1.5 pr-3 text-xs text-slate-500">{reason}</td>
                <td className="py-1.5 pr-3">
                  <span className="rounded bg-rose-50 px-1.5 py-0.5 text-xs text-rose-700">승인 필요</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-slate-400">
        모든 후보는 사용자 승인 후에만 반영됩니다 · 자동 주문/적용 없음 · 미정 비중은 “—”(가짜 숫자 금지).
      </p>
    </div>
  );
}
