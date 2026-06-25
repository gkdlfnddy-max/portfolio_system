"use client";
// 국채 bucket 추천 UI — **재사용 컴포넌트**(Agent 5 개선 1).
// 사용자가 티커를 먼저 고르지 않는다 → 비중 / 단기·장기 / 한국·미국 / 시스템추천 을 먼저 선택.
// 선택이 정해진 뒤에야 ETF 티커를 CandidateComparison(비중·성격 비교표)로 보여준다.
// 자동 적용 없음 — 선택은 제안 입력일 뿐(주문/policy 미반영).
import { useState } from "react";
import type { CandidateEvaluation } from "@/lib/portfolio/types";
import CandidateComparison from "./CandidateComparison";

export type GovbondSelection = {
  weight_pct: number;
  duration: "short" | "long" | "mixed";
  region: "KR" | "US" | "both";
};

const DURATIONS: Array<{ v: GovbondSelection["duration"]; label: string }> = [
  { v: "short", label: "단기" }, { v: "long", label: "장기" }, { v: "mixed", label: "혼합" },
];
const REGIONS: Array<{ v: GovbondSelection["region"]; label: string }> = [
  { v: "KR", label: "한국" }, { v: "US", label: "미국" }, { v: "both", label: "한국+미국" },
];

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" onClick={onClick}
      className={`rounded-full px-3 py-1 text-sm ${active ? "bg-slate-800 text-white" : "bg-slate-100 text-slate-600"}`}>
      {children}
    </button>
  );
}

export default function GovbondBucketSelect({
  systemRecommended,
  candidates = [],
  onChange,
}: {
  systemRecommended?: Partial<GovbondSelection> & { note?: string };
  candidates?: CandidateEvaluation[];   // 선택 확정 후 보여줄 국채 ETF 후보(normalized)
  onChange?: (sel: GovbondSelection) => void;
}) {
  const [weight, setWeight] = useState<number>(systemRecommended?.weight_pct ?? 10);
  const [duration, setDuration] = useState<GovbondSelection["duration"]>(systemRecommended?.duration ?? "mixed");
  const [region, setRegion] = useState<GovbondSelection["region"]>(systemRecommended?.region ?? "KR");
  const [confirmed, setConfirmed] = useState(false);

  function emit(next: Partial<GovbondSelection>) {
    const sel: GovbondSelection = { weight_pct: weight, duration, region, ...next };
    onChange?.(sel);
  }

  return (
    <div className="space-y-3">
      <div>
        <div className="mb-1 text-sm font-medium text-slate-700">국채 비중 (방어)</div>
        <div className="flex items-center gap-2">
          <input type="range" min={0} max={40} step={1} value={weight}
            onChange={(e) => { const w = Number(e.target.value); setWeight(w); emit({ weight_pct: w }); }}
            className="w-48" />
          <span className="text-sm text-slate-600">{weight}%</span>
        </div>
      </div>

      <div>
        <div className="mb-1 text-sm font-medium text-slate-700">만기</div>
        <div className="flex gap-2">
          {DURATIONS.map((d) => (
            <Chip key={d.v} active={duration === d.v} onClick={() => { setDuration(d.v); emit({ duration: d.v }); }}>{d.label}</Chip>
          ))}
        </div>
      </div>

      <div>
        <div className="mb-1 text-sm font-medium text-slate-700">지역</div>
        <div className="flex gap-2">
          {REGIONS.map((r) => (
            <Chip key={r.v} active={region === r.v} onClick={() => { setRegion(r.v); emit({ region: r.v }); }}>{r.label}</Chip>
          ))}
        </div>
      </div>

      {systemRecommended && (
        <div className="rounded bg-emerald-50 p-2 text-xs text-emerald-800">
          시스템 추천: 비중 {systemRecommended.weight_pct ?? "—"}% · {systemRecommended.duration ?? "—"} · {systemRecommended.region ?? "—"}
          {systemRecommended.note ? ` — ${systemRecommended.note}` : ""}
          <button type="button" className="ml-2 underline"
            onClick={() => {
              if (systemRecommended.weight_pct != null) setWeight(systemRecommended.weight_pct);
              if (systemRecommended.duration) setDuration(systemRecommended.duration);
              if (systemRecommended.region) setRegion(systemRecommended.region);
              emit(systemRecommended);
            }}>추천값 적용</button>
        </div>
      )}

      <button type="button" onClick={() => setConfirmed(true)}
        className="rounded bg-slate-800 px-3 py-1.5 text-sm text-white">
        이 성격으로 ETF 후보 비교 보기
      </button>

      {confirmed && (
        <div className="pt-2">
          <CandidateComparison
            title={`국채 ETF 후보 — ${weight}% · ${duration} · ${region}`}
            candidates={candidates}
            emptyNote="조건에 맞는 국채 ETF 후보가 없습니다(데이터 미연동이면 정직하게 비움)."
          />
        </div>
      )}
      <p className="text-xs text-slate-400">티커는 비중·성격이 정해진 뒤 비교표로만 제시됩니다 · 자동 적용 없음.</p>
    </div>
  );
}
