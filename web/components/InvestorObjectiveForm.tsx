"use client";

import { useCallback, useEffect, useState } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input, Textarea } from "@/components/ui/Input";
import { Label } from "@/components/ui/Label";
import { Target, Info, AlertTriangle, Save } from "lucide-react";

// 백엔드 investor_objective CLI contract (criteria 응답) — UI 는 이대로 소비/입력만.
// "최선" ≠ 수익률 최대화. 목적을 먼저 정해야 그 관점의 최선을 계산한다(자동 적용 0).
type Criterion = { metric: string; direction: "min" | "max"; weight: number };
type ObjectiveResp = {
  ok: boolean;
  is_set: boolean;
  goal: string | null;
  label?: string;
  headline?: string;
  criteria: Criterion[];
  deprioritize?: string[];
  objective: {
    investment_goal: string | null;
    horizon: string | null;
    risk_tolerance: string | null;
    loss_aversion: number | null;
    prefers: string[];
    allows: { inverse: boolean; leverage: boolean };
    region_pref: string | null;
    market_view: string | null;
    note: string | null;
  } | null;
  error?: string;
};

const GOALS: [string, string][] = [
  ["loss_reduction", "손실 축소"],
  ["dividend", "배당 수입"],
  ["growth", "성장(장기)"],
  ["aggressive_growth", "공격적 성장"],
  ["volatility_reduction", "변동성 축소"],
  ["thesis_hold", "thesis 유지"],
  ["cash_preservation", "현금 확보/보존"],
  ["stable_operation", "안정 운용"],
];
const RISK: [string, string][] = [["low", "낮음(방어)"], ["mid", "중간"], ["high", "높음(공격)"]];
const REGION: [string, string][] = [["kr", "국내"], ["us", "미국"], ["global", "글로벌"]];
const MARKET_VIEW: [string, string][] = [["short", "단기 시장관"], ["long", "장기 시장관"]];
const PREFERS: [string, string][] = [
  ["cash", "현금"], ["bond", "채권"], ["dividend", "배당"], ["growth", "성장"], ["etf", "ETF"],
];
const ALLOWS: [string, string][] = [["inverse", "인버스/숏"], ["leverage", "레버리지"]];

const goalLabel = (v: string) => GOALS.find(([k]) => k === v)?.[1] ?? v;
const dirLabel = (d: string) => (d === "min" ? "낮을수록 좋음" : "높을수록 좋음");

function Chips({ value, options, onPick }: { value: string; options: [string, string][]; onPick: (v: string) => void }) {
  return (
    <div className="flex flex-wrap gap-2 mt-1">
      {options.map(([v, label]) => (
        <button key={v} type="button" onClick={() => onPick(value === v ? "" : v)}
          className={`text-sm rounded-full border px-3 py-1 transition ${
            value === v ? "border-primary bg-primary-50 text-primary-700" : "border-neutral-200 text-neutral-600 hover:border-primary-100"
          }`}>
          {label}
        </button>
      ))}
    </div>
  );
}

function MultiChips({ values, options, onToggle }: { values: string[]; options: [string, string][]; onToggle: (v: string) => void }) {
  return (
    <div className="flex flex-wrap gap-2 mt-1">
      {options.map(([v, label]) => {
        const on = values.includes(v);
        return (
          <button key={v} type="button" onClick={() => onToggle(v)}
            className={`text-sm rounded-full border px-3 py-1 transition ${
              on ? "border-primary bg-primary-50 text-primary-700" : "border-neutral-200 text-neutral-600 hover:border-primary-100"
            }`}>
            {label}
          </button>
        );
      })}
    </div>
  );
}

type Form = {
  investment_goal: string;
  horizon: string;
  risk_tolerance: string;
  loss_aversion: string;
  prefers: string[];
  allows: string[];
  region_pref: string;
  market_view: string;
  note: string;
};
const EMPTY: Form = {
  investment_goal: "", horizon: "", risk_tolerance: "", loss_aversion: "",
  prefers: [], allows: [], region_pref: "", market_view: "", note: "",
};

export default function InvestorObjectiveForm({ accountId }: { accountId: number }) {
  const [resp, setResp] = useState<ObjectiveResp | null>(null);
  const [form, setForm] = useState<Form>(EMPTY);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await fetch(`/api/accounts/${accountId}/objective`, { cache: "no-store" });
      const j: ObjectiveResp = await r.json();
      if (!r.ok || !j.ok) { setErr(j.error ?? "불러오기 실패"); setResp(null); return; }
      setResp(j);
      const o = j.objective;
      if (o) {
        setForm({
          investment_goal: o.investment_goal ?? "",
          horizon: o.horizon ?? "",
          risk_tolerance: o.risk_tolerance ?? "",
          loss_aversion: o.loss_aversion != null ? String(o.loss_aversion) : "",
          prefers: Array.isArray(o.prefers) ? o.prefers : [],
          allows: o.allows ? Object.entries(o.allows).filter(([, v]) => v).map(([k]) => k) : [],
          region_pref: o.region_pref ?? "",
          market_view: o.market_view ?? "",
          note: o.note ?? "",
        });
      }
    } catch {
      setErr("네트워크 오류");
    } finally {
      setLoading(false);
    }
  }, [accountId]);

  useEffect(() => { load(); }, [load]);

  const set = (k: keyof Form, v: string) => setForm((f) => ({ ...f, [k]: v }));
  const toggle = (k: "prefers" | "allows", v: string) =>
    setForm((f) => ({ ...f, [k]: f[k].includes(v) ? f[k].filter((x) => x !== v) : [...f[k], v] }));

  const submit = async () => {
    if (!form.investment_goal) { setErr("투자 목적을 먼저 선택하세요('최선'의 기준이 됩니다)"); return; }
    setSaving(true);
    setErr(null);
    try {
      const r = await fetch(`/api/accounts/${accountId}/objective`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...form,
          loss_aversion: form.loss_aversion === "" ? null : Number(form.loss_aversion),
        }),
      });
      const j = await r.json();
      if (!r.ok || !j.ok) { setErr(j.error ?? "저장 실패"); return; }
      await load();
    } catch {
      setErr("네트워크 오류");
    } finally {
      setSaving(false);
    }
  };

  const objSet = resp?.is_set === true;

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Target className="w-5 h-5 text-primary" /> 내 투자 목적·성향
          </CardTitle>
        </CardHeader>
        <CardBody className="space-y-5">
          <p className="text-sm text-neutral-500 flex items-start gap-1.5">
            <Info className="w-4 h-4 mt-0.5 shrink-0" />
            <b>“최선”은 사람마다 다릅니다.</b> 손실 줄이기·배당·잘 자기·thesis 유지·공격적 성장 등.
            목적을 먼저 정해야 그 관점에서의 최선을 계산합니다. 입력은 <b>저장만</b> 되고
            <b> 자동으로 적용되지 않습니다</b>(포트폴리오 초안이 참고할 뿐, 반영은 사장님 승인 후).
          </p>

          {/* 정직: 목적 미설정이면 기본값 가정 금지 — 안내 표시 */}
          {!loading && resp && !objSet && (
            <div className="flex items-start gap-2 rounded-xl border border-warning/30 bg-warning/5 p-3 text-sm text-warning">
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              투자 목적 미설정 — 먼저 입력을 권장합니다. 목적이 없으면 “최선”의 기준을
              임의로 가정하지 않습니다.
            </div>
          )}

          <div>
            <Label>투자 목적 * (“최선”의 기준)</Label>
            <Chips value={form.investment_goal} options={GOALS} onPick={(v) => set("investment_goal", v)} />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <Label>위험 감수</Label>
              <Chips value={form.risk_tolerance} options={RISK} onPick={(v) => set("risk_tolerance", v)} />
            </div>
            <div>
              <Label>투자 기간(horizon)</Label>
              <Input value={form.horizon} onChange={(e) => set("horizon", e.target.value)} placeholder="예: 3년 / 장기" />
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <Label>손실 회피 성향 (0~1)</Label>
              <Input type="number" min={0} max={1} step={0.1} value={form.loss_aversion}
                onChange={(e) => set("loss_aversion", e.target.value)} placeholder="예: 0.8 (손실 매우 싫음)" />
            </div>
            <div>
              <Label>지역 선호</Label>
              <Chips value={form.region_pref} options={REGION} onPick={(v) => set("region_pref", v)} />
            </div>
          </div>

          <div>
            <Label>시장관(보는 기간)</Label>
            <Chips value={form.market_view} options={MARKET_VIEW} onPick={(v) => set("market_view", v)} />
          </div>

          <div>
            <Label>선호 (복수 선택)</Label>
            <MultiChips values={form.prefers} options={PREFERS} onToggle={(v) => toggle("prefers", v)} />
          </div>

          <div>
            <Label>허용 (복수 선택)</Label>
            <MultiChips values={form.allows} options={ALLOWS} onToggle={(v) => toggle("allows", v)} />
          </div>

          <div>
            <Label>자유 노트(목적/성향 보충)</Label>
            <Textarea value={form.note} onChange={(e) => set("note", e.target.value)}
              placeholder="예: 손실이 제일 싫고 밤에 마음 편한 게 우선. 배당으로 현금 흐름도 좋다." />
          </div>

          {err && <p className="text-sm text-error">{err}</p>}

          <Button onClick={submit} disabled={saving || !form.investment_goal}>
            <Save className="w-4 h-4" /> {saving ? "저장 중…" : "목적·성향 저장"}
          </Button>
        </CardBody>
      </Card>

      {/* "최선 기준" 미리보기 — 목적 → 평가지표 우선순위(규칙 기반, Anthropic 미사용) */}
      <Card>
        <CardHeader>
          <CardTitle>이 목적에서의 “최선” 기준</CardTitle>
        </CardHeader>
        <CardBody className="space-y-3">
          {loading && <p className="text-sm text-neutral-500">불러오는 중…</p>}
          {!loading && !objSet && (
            <p className="text-sm text-neutral-500 text-center py-4">
              목적이 설정되면 “최선”을 어떤 지표로 판단할지 여기 표시됩니다.
            </p>
          )}
          {!loading && objSet && resp && (
            <>
              <p className="text-sm text-neutral-700">
                <b>{resp.label ? goalLabel(resp.goal ?? "") : resp.goal}</b> — {resp.headline}
              </p>
              <div className="space-y-2">
                {resp.criteria.map((c) => (
                  <div key={c.metric} className="flex items-center justify-between gap-3 rounded-xl border border-neutral-200 p-3 text-sm">
                    <span className="font-medium text-neutral-900">{c.metric}</span>
                    <span className="flex items-center gap-2">
                      <span className={`rounded-full px-2 py-0.5 text-xs ${c.direction === "min" ? "bg-success/10 text-success" : "bg-primary-50 text-primary-700"}`}>
                        {dirLabel(c.direction)}
                      </span>
                      <span className="text-xs text-neutral-400">가중 {c.weight}</span>
                    </span>
                  </div>
                ))}
              </div>
              {resp.deprioritize && resp.deprioritize.length > 0 && (
                <p className="text-xs text-neutral-500">
                  후순위(이 목적에선 덜 중요): {resp.deprioritize.join(", ")}
                </p>
              )}
              <p className="text-xs text-neutral-400">
                규칙 기반 매핑(Anthropic API 미사용). 저장만 되며 자동 적용되지 않습니다.
              </p>
            </>
          )}
        </CardBody>
      </Card>
    </div>
  );
}
