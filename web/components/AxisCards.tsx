// 6축 분석 카드 — **재사용 컴포넌트**(Agent 5 개선 2). 모든 축을 같은 카드로 표시.
// 백엔드 AxisResult(decline/axes) 표준 필드 소비: axis/data_available/confidence/signals/
// portfolio_impact/missing_data/suggested_actions. 미연동 축은 정직하게 "미연동" 표기(가짜 점수 없음).
import type { ReactNode } from "react";

export type AxisDatum = {
  axis?: string;
  axis_name?: string;
  label?: string;
  data_available?: boolean;
  confidence?: number;
  risk_0_100?: number;
  signals?: Array<{ name?: string; fired?: boolean; detail?: string }>;
  portfolio_impact?: string;
  missing_data?: string[];
  suggested_actions?: string[];
};

const AXIS_LABEL: Record<string, string> = {
  technical: "기술", distribution: "분산", macro: "거시",
  event: "이벤트", sentiment: "심리", policy: "정책/규제",
};

function Badge({ children, cls }: { children: ReactNode; cls: string }) {
  return <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${cls}`}>{children}</span>;
}

function firedSignals(a: AxisDatum): string[] {
  return (a.signals ?? []).filter((s) => s?.fired && s?.name).map((s) => String(s.name));
}

function AxisCard({ a }: { a: AxisDatum }) {
  const key = a.axis_name || a.axis || "";
  const label = a.label || AXIS_LABEL[key] || key;
  const available = !!a.data_available;
  const fired = firedSignals(a);
  return (
    <div className="rounded-lg border border-slate-200 p-3">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-sm font-semibold text-slate-800">{label}</span>
        {available
          ? <Badge cls="bg-sky-100 text-sky-800">연결됨</Badge>
          : <Badge cls="bg-slate-100 text-slate-500">미연동</Badge>}
      </div>
      {available ? (
        <>
          <div className="text-xs text-slate-500">
            신뢰도 {(a.confidence ?? 0).toFixed(2)}
            {typeof a.risk_0_100 === "number" ? ` · 위험 ${a.risk_0_100.toFixed(0)}` : ""}
          </div>
          {fired.length > 0 && (
            <div className="mt-1 text-xs text-slate-600">핵심 신호: {fired.join(", ")}</div>
          )}
          {a.portfolio_impact && (
            <div className="mt-1 text-xs text-slate-600">포트폴리오 영향: {a.portfolio_impact}</div>
          )}
        </>
      ) : (
        <div className="text-xs text-slate-400">
          데이터 미연동 — 분석 제외(가짜 점수 없음).
          {a.missing_data && a.missing_data.length > 0 ? ` 필요: ${a.missing_data.join(", ")}` : ""}
        </div>
      )}
      {a.suggested_actions && a.suggested_actions.length > 0 && (
        <div className="mt-1 text-xs text-amber-700">추가 확인: {a.suggested_actions.join(", ")}</div>
      )}
    </div>
  );
}

export default function AxisCards({ axes, title }: { axes: AxisDatum[]; title?: string }) {
  if (!axes || axes.length === 0) {
    return <p className="text-sm text-slate-500">6축 데이터 없음(데이터 부족이면 정직하게 비움).</p>;
  }
  return (
    <div>
      {title && <h4 className="mb-2 text-sm font-semibold text-slate-700">{title}</h4>}
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
        {axes.map((a) => <AxisCard key={a.axis_name || a.axis} a={a} />)}
      </div>
      <p className="mt-2 text-xs text-slate-400">
        미연동 축은 분석에서 제외됩니다 — “모든 데이터를 고려했다”고 말하지 않습니다(정직).
      </p>
    </div>
  );
}
