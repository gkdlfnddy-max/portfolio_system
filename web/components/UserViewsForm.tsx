"use client";

import { useCallback, useEffect, useState } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input, Textarea } from "@/components/ui/Input";
import { Label } from "@/components/ui/Label";
import { Brain, Plus, Archive, Info } from "lucide-react";

// 백엔드 user_views CLI contract — UI 는 이대로 소비/입력만 한다.
type View = {
  id: number;
  account_index: number;
  layer: string;
  theme: string | null;
  ticker: string | null;
  etf: string | null;
  stance: string | null;
  conviction: number | null;
  horizon: string | null;
  note: string | null;
  status: string;
  superseded_by: number | null;
  created_at: string;
};

const LAYERS: [string, string][] = [
  ["grand", "대전제(투자 성향)"],
  ["mid", "중전제(관심 분야·견해)"],
  ["short", "단기 견해"],
  ["long", "장기 견해"],
];
const STANCES: [string, string][] = [
  ["positive", "긍정"],
  ["neutral", "중립"],
  ["negative", "부정"],
  ["observe", "관찰만"],
];
const HORIZONS: [string, string][] = [
  ["short", "단기"],
  ["mid", "중기"],
  ["long", "장기"],
];

const layerLabel = (v: string) => LAYERS.find(([k]) => k === v)?.[1] ?? v;
const stanceLabel = (v: string | null) => (v ? STANCES.find(([k]) => k === v)?.[1] ?? v : "—");

const STANCE_CLS: Record<string, string> = {
  positive: "bg-success/10 text-success",
  negative: "bg-error/10 text-error",
  neutral: "bg-neutral-100 text-neutral-600",
  observe: "bg-warning/10 text-warning",
};

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

type Form = {
  layer: string; theme: string; ticker: string; etf: string;
  stance: string; conviction: string; horizon: string; note: string;
};
const EMPTY: Form = { layer: "", theme: "", ticker: "", etf: "", stance: "", conviction: "", horizon: "", note: "" };

export default function UserViewsForm({ accountId }: { accountId: number }) {
  const [views, setViews] = useState<View[]>([]);
  const [form, setForm] = useState<Form>(EMPTY);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await fetch(`/api/accounts/${accountId}/views`, { cache: "no-store" });
      const j = await r.json();
      if (!r.ok || !j.ok) { setErr(j.error ?? "불러오기 실패"); setViews([]); return; }
      setViews(Array.isArray(j.views) ? j.views : []);
    } catch (e: any) {
      setErr("네트워크 오류");
    } finally {
      setLoading(false);
    }
  }, [accountId]);

  useEffect(() => { load(); }, [load]);

  const set = (k: keyof Form, v: string) => setForm((f) => ({ ...f, [k]: v }));

  const submit = async () => {
    if (!form.layer) { setErr("계층(대전제/중전제/단기/장기)을 선택하세요"); return; }
    setSaving(true);
    setErr(null);
    try {
      const r = await fetch(`/api/accounts/${accountId}/views`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "add", ...form }),
      });
      const j = await r.json();
      if (!r.ok || !j.ok) { setErr(j.error ?? "저장 실패"); return; }
      setForm(EMPTY);
      await load();
    } catch {
      setErr("네트워크 오류");
    } finally {
      setSaving(false);
    }
  };

  const archive = async (view_id: number) => {
    setErr(null);
    try {
      const r = await fetch(`/api/accounts/${accountId}/views`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "archive", view_id }),
      });
      const j = await r.json();
      if (!r.ok || !j.ok) { setErr(j.error ?? "보관 실패"); return; }
      await load();
    } catch {
      setErr("네트워크 오류");
    }
  };

  const grouped = LAYERS.map(([k, label]) => ({
    key: k, label, items: views.filter((v) => v.layer === k),
  }));

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Brain className="w-5 h-5 text-primary" /> 내 투자 견해 입력
          </CardTitle>
        </CardHeader>
        <CardBody className="space-y-5">
          <p className="text-sm text-neutral-500 flex items-start gap-1.5">
            <Info className="w-4 h-4 mt-0.5 shrink-0" />
            여기 적은 생각은 <b>1급 입력</b>으로 저장됩니다. 데이터보다 무조건 우선하지도, 무시되지도 않습니다.
            시스템은 견해와 데이터의 <b>일치/충돌을 설명</b>만 하고, <b>자동으로 적용하지 않습니다</b>
            (포트폴리오 초안에만 참고되고 실제 반영은 사장님 승인 후).
          </p>

          <div>
            <Label>계층 *</Label>
            <Chips value={form.layer} options={LAYERS} onPick={(v) => set("layer", v)} />
          </div>

          <div>
            <Label>입장(stance)</Label>
            <Chips value={form.stance} options={STANCES} onPick={(v) => set("stance", v)} />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div>
              <Label>테마</Label>
              <Input value={form.theme} onChange={(e) => set("theme", e.target.value)} placeholder="예: 반도체" />
            </div>
            <div>
              <Label>종목</Label>
              <Input value={form.ticker} onChange={(e) => set("ticker", e.target.value)} placeholder="예: 005930" />
            </div>
            <div>
              <Label>ETF</Label>
              <Input value={form.etf} onChange={(e) => set("etf", e.target.value)} placeholder="예: ARKG" />
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <Label>확신도 (0~1)</Label>
              <Input type="number" min={0} max={1} step={0.1} value={form.conviction}
                onChange={(e) => set("conviction", e.target.value)} placeholder="예: 0.7" />
            </div>
            <div>
              <Label>기간(horizon)</Label>
              <Chips value={form.horizon} options={HORIZONS} onPick={(v) => set("horizon", v)} />
            </div>
          </div>

          <div>
            <Label>자유 노트(내 생각 원문)</Label>
            <Textarea value={form.note} onChange={(e) => set("note", e.target.value)}
              placeholder="예: 반도체 장기 긍정인데 단기로는 고점 같다. 바이오는 ETF로만. 양자는 관찰만." />
          </div>

          {err && <p className="text-sm text-error">{err}</p>}

          <Button onClick={submit} disabled={saving || !form.layer}>
            <Plus className="w-4 h-4" /> {saving ? "저장 중…" : "견해 저장"}
          </Button>
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>저장된 견해 (계층별)</CardTitle>
        </CardHeader>
        <CardBody className="space-y-5">
          {loading && <p className="text-sm text-neutral-500">불러오는 중…</p>}
          {!loading && views.length === 0 && (
            <p className="text-sm text-neutral-500 text-center py-4">아직 저장된 견해가 없습니다.</p>
          )}
          {!loading && grouped.map((g) => (
            g.items.length > 0 && (
              <div key={g.key}>
                <div className="text-sm font-medium text-neutral-700 mb-2">{g.label}</div>
                <div className="space-y-2">
                  {g.items.map((v) => (
                    <div key={v.id} className="flex items-start justify-between gap-3 rounded-xl border border-neutral-200 p-3">
                      <div className="space-y-1">
                        <div className="flex items-center gap-2 flex-wrap text-sm">
                          <span className={`rounded-full px-2 py-0.5 text-xs ${STANCE_CLS[v.stance ?? "neutral"] ?? "bg-neutral-100 text-neutral-600"}`}>
                            {stanceLabel(v.stance)}
                          </span>
                          {v.theme && <span className="font-medium text-neutral-900">{v.theme}</span>}
                          {v.ticker && <span className="text-neutral-500">· {v.ticker}</span>}
                          {v.etf && <span className="text-neutral-500">· ETF {v.etf}</span>}
                          {typeof v.conviction === "number" && (
                            <span className="text-xs text-neutral-400">확신도 {Math.round(v.conviction * 100)}%</span>
                          )}
                        </div>
                        {v.note && <p className="text-sm text-neutral-600">{v.note}</p>}
                      </div>
                      <Button size="sm" variant="ghost" onClick={() => archive(v.id)} title="보관">
                        <Archive className="w-4 h-4" />
                      </Button>
                    </div>
                  ))}
                </div>
              </div>
            )
          ))}
        </CardBody>
      </Card>
    </div>
  );
}
