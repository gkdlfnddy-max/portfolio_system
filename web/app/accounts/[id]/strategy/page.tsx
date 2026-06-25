"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input, Textarea } from "@/components/ui/Input";
import { Label } from "@/components/ui/Label";
import { ArrowLeft, ArrowRight, Sparkles, Check, Lightbulb, Lock } from "lucide-react";
import { POLICY_TYPES, sourceMeta, isHardSource, type PolicyType } from "@/lib/policy/labels";
import ThemeSuggestionCards from "@/components/ThemeSuggestionCards";

type Form = {
  posture_text: string;
  policy_type: string;
  risk_tolerance: string;
  short_policy: string;
  cash_min_pct: string;
  cash_max_pct: string;
  horizon: string;
  interests_text: string;
  views_text: string;
  individual_cap_pct: string;
  individual_count: string;
  region_pref: string;
  rebalance_pace: string;
  bond_target_pct: string;
  bond_duration_pref: string;
};

// 백엔드 policy_rules CLI 응답 shape(contract). UI 는 이대로 소비만 한다.
type PolicyResp = {
  policy_type?: string;
  effective?: Record<string, unknown>;
  hard_rules?: string[];
  ignored_overrides?: string[];
  blocked_disables?: string[];
  sources?: Record<string, string>;
};

const EMPTY: Form = {
  posture_text: "", policy_type: "", risk_tolerance: "", short_policy: "", cash_min_pct: "", cash_max_pct: "",
  horizon: "", interests_text: "", views_text: "", individual_cap_pct: "", individual_count: "",
  region_pref: "", rebalance_pace: "", bond_target_pct: "", bond_duration_pref: "",
};

// 상담 "그대로 적용" 프리뷰용 — 필드 한글 라벨
const FIELD_LABEL: Record<string, string> = {
  posture_text: "컨셉(대전제)", region_pref: "지역 비중", bond_target_pct: "채권 목표(%)",
  bond_duration_pref: "채권 듀레이션", risk_tolerance: "성향", short_policy: "숏(인버스) 허용",
  cash_min_pct: "현금 하한", cash_max_pct: "현금 상한", interests_text: "관심 테마",
  views_text: "내 생각", rebalance_pace: "조정 속도", horizon: "투자 기간",
  individual_cap_pct: "개별주 한도", individual_count: "개별 종목 수",
};
const flabel = (f: string) => FIELD_LABEL[f === "posture_append" ? "posture_text" : f] ?? f;

function Chips({ value, options, onPick }: { value: string; options: [string, string][]; onPick: (v: string) => void }) {
  return (
    <div className="flex flex-wrap gap-2 mt-1">
      {options.map(([v, label]) => (
        <button key={v} type="button" onClick={() => onPick(value === v ? "" : v)}
          className={`text-sm rounded-full border px-3 py-1 transition ${value === v ? "border-primary bg-primary-50 text-primary-700" : "border-neutral-200 text-neutral-600 hover:border-primary-100"}`}>
          {label}
        </button>
      ))}
    </div>
  );
}

// 값 옆 출처 뱃지 — policy.sources[field] 로 결정. 없으면 렌더 안 함(가짜 라벨 금지).
function SourceBadge({ src }: { src?: string }) {
  if (!src) return null;
  const m = sourceMeta(src);
  return (
    <span className={`inline-flex items-center gap-0.5 text-[10px] rounded-full border px-1.5 py-0.5 ${m.cls}`}>
      {m.locked && <Lock className="w-2.5 h-2.5" />}{m.label}
    </span>
  );
}

export default function StrategyPage() {
  const params = useParams();
  const id = String(params.id);
  const [form, setForm] = useState<Form>(EMPTY);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [distilling, setDistilling] = useState(false);
  const [distillNote, setDistillNote] = useState<string | null>(null);
  const [keywords, setKeywords] = useState<string[]>([]);
  const [gaps, setGaps] = useState<string[]>([]);
  const [advice, setAdvice] = useState<any[]>([]);
  const [adviceThemes, setAdviceThemes] = useState<any[]>([]);
  const [themeDirections, setThemeDirections] = useState<Record<string, string>>({});  // 테마별 방향 override(doc.theme_directions)
  const [midAnalysis, setMidAnalysis] = useState<any | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [consultOpen, setConsultOpen] = useState(false);
  const [consultInput, setConsultInput] = useState("");
  const [consultLog, setConsultLog] = useState<any[]>([]);
  const [consultBusy, setConsultBusy] = useState(false);
  // "그대로 적용" 임시 반영 프리뷰(변경 전/후) + 저장 경고(지역 합계 등)
  const [appliedChanges, setAppliedChanges] = useState<{ field: string; before: string; after: string }[]>([]);
  const [saveWarnings, setSaveWarnings] = useState<string[]>([]);
  // 유연 투자기준(effective 정책 + 출처). 백엔드 policy_rules CLI 가 진리 — 없으면 null(빈 상태).
  const [policy, setPolicy] = useState<PolicyResp | null>(null);

  const set = (k: keyof Form, v: string) => { setForm((f) => ({ ...f, [k]: v })); setSaved(false); };

  // 필드별 출처(default/template/user/agent/hard) — 백엔드 sources 맵에서. hard 면 잠금(편집 불가).
  const srcOf = (field: string): string | undefined => policy?.sources?.[field];
  const lockedField = (field: string): boolean => isHardSource(policy?.sources?.[field]);

  const load = useCallback(async () => {
    const r = await fetch(`/api/accounts/${id}/profile`, { cache: "no-store" });
    const p = (await r.json()).profile;
    if (p) {
      setForm({
        posture_text: p.posture_text ?? "", policy_type: p.policy_type ?? "", risk_tolerance: p.risk_tolerance ?? "", short_policy: p.short_policy ?? "",
        cash_min_pct: p.cash_min_pct != null ? String(p.cash_min_pct) : "", cash_max_pct: p.cash_max_pct != null ? String(p.cash_max_pct) : "",
        horizon: p.horizon ?? "", interests_text: p.interests_text ?? "", views_text: p.views_text ?? "",
        individual_cap_pct: p.individual_cap_pct != null ? String(p.individual_cap_pct) : "",
        individual_count: p.individual_count != null ? String(p.individual_count) : "",
        region_pref: p.region_pref ?? "", rebalance_pace: p.rebalance_pace ?? "",
        bond_target_pct: p.bond_target_pct != null ? String(p.bond_target_pct) : "",
        bond_duration_pref: p.bond_duration_pref ?? "",
      });
      try {
        const doc = p.doc ? JSON.parse(p.doc) : null;
        if (doc?.theme_directions && typeof doc.theme_directions === "object") setThemeDirections(doc.theme_directions);
      } catch { /* doc 파싱 실패 무시 */ }
    }
    // 유연 투자기준(effective + 출처) — 백엔드 policy_rules CLI. 없으면 null(정직한 빈 상태).
    try {
      const pr = await fetch(`/api/accounts/${id}/policy`, { cache: "no-store" });
      const pj = await pr.json();
      setPolicy(pj?.policy ?? null);
    } catch { setPolicy(null); }
  }, [id]);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    setBusy(true);
    try {
      // 단단한 변수는 컬럼으로, 진화하는 내용(키워드/보완점)은 doc(JSON 문서)로 — 하이브리드.
      const doc = { keywords, gaps, updated_from: "strategy_ui", theme_directions: themeDirections };
      const r = await fetch(`/api/accounts/${id}/profile`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ ...form, doc }),
      });
      const j = await r.json();
      setSaved(!!j.ok);
      setSaveWarnings(j.warnings ?? []);
      if (j.ok) setAppliedChanges([]);  // 저장되면 임시반영 프리뷰 해소(=policy version 생성)
    } catch { setSaved(false); }
    setBusy(false);
  };

  const distill = async () => {
    if (!form.posture_text.trim()) { setDistillNote("먼저 컨셉을 한두 줄 적어주세요."); return; }
    setDistilling(true); setDistillNote(null);
    try {
      const r = await fetch(`/api/profile/distill`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text: form.posture_text }),
      });
      const j = await r.json();
      if (j.ok && j.suggested) {
        const s = j.suggested;
        setForm((f) => ({
          ...f,
          risk_tolerance: s.risk_tolerance || f.risk_tolerance,
          short_policy: s.short_policy || f.short_policy,
          cash_min_pct: s.cash_min_pct != null ? String(s.cash_min_pct) : f.cash_min_pct,
          cash_max_pct: s.cash_max_pct != null ? String(s.cash_max_pct) : f.cash_max_pct,
          horizon: s.horizon || f.horizon,
          interests_text: s.interests_text || f.interests_text,
          individual_cap_pct: s.individual_cap_pct != null ? String(s.individual_cap_pct) : f.individual_cap_pct,
          individual_count: s.individual_count != null ? String(s.individual_count) : f.individual_count,
          region_pref: s.region_pref || f.region_pref,
          rebalance_pace: s.rebalance_pace || f.rebalance_pace,
        }));
        setKeywords(j.keywords ?? []);
        setGaps(j.gaps ?? []);
        setSaved(false);
        setDistillNote("컨셉에서 대전제를 정리했습니다 ↓ 값을 확인·수정한 뒤 저장하세요. (대화로 더 다듬을 수 있어요)");
        // 조언(개선 제안) — 규칙+벤치마크+우리 메모리(lessons) 근거
        try {
          const ar = await fetch(`/api/accounts/${id}/advice`, {
            method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ concept: form.posture_text }),
          });
          const aj = await ar.json();
          if (aj.ok) { setAdvice(aj.items ?? []); setAdviceThemes(aj.themes ?? []); }
        } catch { /* 조언 실패는 정리 자체를 막지 않음 */ }
      } else setDistillNote(j.error || "정리 실패");
    } catch { setDistillNote("정리 실패"); }
    setDistilling(false);
  };

  // 조사 후보 추가 → 관심 테마별 정리 갱신. 백엔드가 interests 등재를 처리하므로
  // 여기서는 ① 폼 즉시 표시(낙관적) ② 프로필 reload ③ 관심 테마별 정리(advice themes) 재조회.
  // **방향 미정으로만 등장** — 자동 long/policy/주문 반영은 일절 없음.
  const refreshThemes = useCallback(async () => {
    try {
      const ar = await fetch(`/api/accounts/${id}/advice`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ concept: form.posture_text || form.interests_text || " " }),
      });
      const aj = await ar.json();
      if (aj.ok) { setAdvice(aj.items ?? []); setAdviceThemes(aj.themes ?? []); }
    } catch { /* 갱신 실패는 추가 자체를 막지 않음 */ }
  }, [id, form.posture_text, form.interests_text]);

  const onThemeAddedToResearch = useCallback((theme: string) => {
    // 폼에도 즉시 반영(중복 방지) — 사용자가 바로 '관심 분야'에서 확인 가능.
    setForm((f) => {
      const items = (f.interests_text || "").split(",").map((s) => s.trim()).filter(Boolean);
      if (items.some((t) => t === theme)) return f;
      return { ...f, interests_text: [...items, theme].join(", ") };
    });
    setSaved(false);
    // 백엔드 등재가 반영된 최신 상태로 다시 불러와 관심 테마별 정리에 '방향 미정'으로 노출.
    load();
    refreshThemes();
  }, [load, refreshThemes]);

  const decideAdvice = async (advice_id: number, accept: boolean) => {
    setAdvice((a) => a.map((it) => (it.id === advice_id ? { ...it, status: accept ? "accepted" : "rejected" } : it)));
    try {
      await fetch(`/api/accounts/${id}/advice`, {
        method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ advice_id, accept }),
      });
    } catch { /* 로컬 상태는 이미 반영 */ }
  };

  const analyzeMid = async () => {
    setAnalyzing(true);
    try {
      const r = await fetch(`/api/accounts/${id}/analysis`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ interests: form.interests_text, views: form.views_text }),
      });
      const j = await r.json();
      if (j.ok) setMidAnalysis(j);
    } catch { /* 무시 */ }
    setAnalyzing(false);
  };

  const askConsult = async () => {
    const q = consultInput.trim();
    if (!q) return;
    setConsultBusy(true); setConsultInput("");
    try {
      const r = await fetch(`/api/accounts/${id}/consult`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ question: q }),
      });
      const j = await r.json();
      setConsultLog((log) => [...log, { q, a: j.answer ?? j.error ?? "응답 없음", suggestions: j.suggestions ?? [], refs: j.refs ?? [] }]);
    } catch { setConsultLog((log) => [...log, { q, a: "상담 실패", suggestions: [], refs: [] }]); }
    setConsultBusy(false);
  };

  const applyConsult = (apply: { field: string; value: string }) => {
    const isAppend = apply.field === "posture_append";
    const key = (isAppend ? "posture_text" : apply.field) as keyof Form;
    const before = String(form[key] ?? "");
    const after = isAppend ? (form.posture_text + apply.value).trim() : apply.value;
    setForm((f) => ({ ...f, [key]: after }));
    // 임시 반영 — 변경 전/후를 프리뷰에 기록(저장 전엔 DB policy 불변). 같은 필드는 최신만.
    setAppliedChanges((cs) => [...cs.filter((c) => c.field !== key), { field: key as string, before, after }]);
    setSaved(false);
  };

  const srcLabel = (s: string) =>
    s === "rule" ? "규칙" : s === "benchmark" ? "외부사례" : String(s).startsWith("lesson") ? "메모리" : s === "research" ? "외부조사" : s;

  // ---- 필드별 AI 조언 (field advisors) — 임시 제안. '그대로 적용'은 폼 state 만 바꾸고 저장은 직접 ----
  type FieldAdvice = {
    field_name: string; agent_name: string; advice_type: string;
    original_text: string; suggested_text: string;
    extracted_variables: Record<string, any>; risk_warnings: string[];
    missing_points: string[]; follow_up: string[]; sources: any[]; confidence: number;
  };
  const [fieldAdvice, setFieldAdvice] = useState<Record<string, { advice: FieldAdvice; consultation_id: number } | null>>({});
  const [fieldBusy, setFieldBusy] = useState<string | null>(null);
  const [fieldErr, setFieldErr] = useState<string | null>(null);  // 조언 실패/세션잠김 가시화(조용한 실패 금지)

  // 패널 키(필드)별로 어떤 폼 키에 적용할지 매핑.
  const TARGET_FIELD: Record<string, keyof Form> = {
    interests: "interests_text", views: "views_text", region: "region_pref",
    defensive: "bond_target_pct", pace: "rebalance_pace", whole: "views_text",
  };

  const askField = async (field: string, advice_type?: string) => {
    setFieldBusy(field + (advice_type ?? "")); setFieldErr(null);
    try {
      const payload: any = { field, advice_type };
      if (field === "whole") { payload.interests = form.interests_text; payload.views = form.views_text; }
      else payload.text = String(form[TARGET_FIELD[field]] ?? "");
      const r = await fetch(`/api/accounts/${id}/field-advice`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload),
      });
      if (r.status === 401 || r.status === 403) {
        // 로그인 만료(401) 또는 계좌 권한 없음(403) — 알림 + 로그인 화면으로.
        setFieldErr(r.status === 403 ? "이 계좌에 대한 권한이 없습니다." : "로그인이 필요합니다 — 다시 로그인하세요.");
        setFieldBusy(null);
        if (r.status === 401) setTimeout(() => { window.location.href = "/login"; }, 1200);
        return;
      }
      const j = await r.json();
      if (j.ok && j.advice) setFieldAdvice((m) => ({ ...m, [field]: { advice: j.advice, consultation_id: j.consultation_id } }));
      else { setFieldAdvice((m) => ({ ...m, [field]: null })); setFieldErr(j.error || "조언을 생성하지 못했습니다."); }
    } catch (e: any) { setFieldErr("네트워크 오류 — 다시 시도하세요."); }
    setFieldBusy(null);
  };

  // field_advice_events 기록(append-only) — 저장이 아니라 행동 추적.
  const recordFieldAction = (field: string, consultation_id: number, user_action: string, detail?: string) => {
    fetch(`/api/accounts/${id}/field-advice`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ action: "record", field, consultation_id, user_action, detail }),
    }).catch(() => { /* 행동 기록 실패는 폼 동작을 막지 않음 */ });
  };

  // '그대로 적용' — 기존 applyConsult 와 동일한 임시 반영(폼만 변경, saved=false). DB/정책 불변.
  const applyFieldAdvice = (field: string, edit = false) => {
    const entry = fieldAdvice[field];
    if (!entry) return;
    const key = TARGET_FIELD[field];
    const before = String(form[key] ?? "");
    const after = entry.advice.suggested_text;
    setForm((f) => ({ ...f, [key]: after }));
    setAppliedChanges((cs) => [...cs.filter((c) => c.field !== key), { field: key as string, before, after }]);
    setSaved(false);
    recordFieldAction(field, entry.consultation_id, edit ? "edited" : "applied",
      edit ? "수정해서 적용(폼 임시 반영, 저장 전)" : "그대로 적용(폼 임시 반영, 저장 전)");
    setFieldAdvice((m) => ({ ...m, [field]: null }));
  };

  const ignoreFieldAdvice = (field: string) => {
    const entry = fieldAdvice[field];
    if (entry) recordFieldAction(field, entry.consultation_id, "ignored");
    setFieldAdvice((m) => ({ ...m, [field]: null }));
  };

  // 채권 비율 3안 적용 — **채권 비율(방어 대비) + 듀레이션만** 폼 반영. 현금밴드는 위에서 정한 값 유지.
  const applyDefensiveOption = (opt: any, edit = false) => {
    const entry = fieldAdvice["defensive"];
    const bond = String(opt.bond_ratio_pct ?? 0);   // 방어자산 대비 국채 비율(%)
    const dur = opt.bond_duration_preference ?? "";
    const targets: [keyof Form, string][] = [
      ["bond_target_pct", bond],
      ...(dur ? ([["bond_duration_pref", dur]] as [keyof Form, string][]) : []),
    ];
    const changes = targets.map(([k, after]) => ({ field: k as string, before: String(form[k] ?? ""), after }));
    setForm((f) => { const nf = { ...f }; targets.forEach(([k, v]) => { (nf as any)[k] = v; }); return nf; });
    setAppliedChanges((cs) => [...cs.filter((c) => !changes.some((x) => x.field === c.field)), ...changes]);
    setSaved(false);
    if (entry) recordFieldAction("defensive", entry.consultation_id, edit ? "edited" : "applied",
      `채권 ${opt.option}안 적용(국채 = 방어의 ${opt.bond_ratio_pct}% · ${opt.bond_duration_preference}) — 현금밴드 불변, 폼 임시반영`);
    setFieldAdvice((m) => ({ ...m, defensive: null }));
  };

  // 인라인 조언 패널.
  const FieldBtn = ({ field, type, label }: { field: string; type?: string; label: string }) => (
    <button type="button" onClick={() => askField(field, type)} disabled={fieldBusy === field + (type ?? "")}
      className="text-[11px] rounded-full border border-primary-100 text-primary-700 px-2.5 py-1 hover:bg-primary-50 disabled:opacity-50">
      <Sparkles className="w-3 h-3 inline -mt-0.5 mr-0.5" />{fieldBusy === field + (type ?? "") ? "…" : label}
    </button>
  );

  const AdvicePanel = ({ field }: { field: string }) => {
    const entry = fieldAdvice[field];
    if (!entry) return null;
    const a = entry.advice;
    const key = TARGET_FIELD[field];
    const before = String(form[key] ?? "");
    return (
      <div className="rounded-xl border border-primary-100 bg-primary-50/40 p-3 space-y-2 mt-2">
        <div className="flex items-center justify-between">
          <div className="text-sm font-medium text-primary-700 flex items-center gap-1.5">
            <Sparkles className="w-4 h-4" /> AI 조언 ({a.agent_name}) — 임시 제안, 저장 전엔 정책 불변
          </div>
          <span className="text-[10px] text-neutral-400">신뢰도 {Math.round((a.confidence ?? 0) * 100)}%</span>
        </div>
        {a.original_text && (
          <div className="text-xs"><span className="text-neutral-400">원문</span> <span className="text-neutral-600">{a.original_text}</span></div>
        )}
        {a.suggested_text && (
          <div className="text-xs"><span className="text-neutral-400">개선안</span> <span className="text-neutral-900 font-medium">{a.suggested_text}</span></div>
        )}
        {/* 채권 비율 3안(보수/기준/공격) — 방어자산 대비 국채 비율 + 듀레이션만 (현금밴드 불변) */}
        {field === "defensive" && Array.isArray(a.extracted_variables?.options) && (
          <div className="space-y-1.5">
            <span className="text-[11px] text-neutral-400">채권 비율 3안 — 방어자산(현금밴드) 안에서 국채 비율 + 듀레이션. 현금밴드는 위 설정 유지.</span>
            <div className="grid grid-cols-3 gap-1.5">
              {a.extracted_variables.options.map((o: any) => {
                const isRec = o.option === a.extracted_variables.recommendation?.option;
                const label = o.option === "conservative" ? "보수안" : o.option === "aggressive" ? "공격안" : "기준안";
                const durK: Record<string, string> = { short: "단기", intermediate: "중기", long: "장기", mixed: "사다리" };
                return (
                  <div key={o.option} className={`rounded-lg border p-2 text-[11px] ${isRec ? "border-primary-300 bg-primary-50" : "border-neutral-200 bg-white"}`}>
                    <div className="font-medium text-neutral-800">{label}{isRec && <span className="text-primary-600"> · 권장</span>}</div>
                    <div className="text-sky-700 mt-0.5">국채 = 방어의 <b>{o.bond_ratio_pct}%</b></div>
                    <div className="text-neutral-600">듀레이션 {durK[o.bond_duration_preference] ?? o.bond_duration_preference}</div>
                    {o.reason && <div className="text-neutral-400 mt-0.5 leading-tight">{o.reason}</div>}
                    {o.est_bond_pct != null && <div className="text-neutral-300 mt-0.5">현 방어밴드 기준 ≈ {o.est_bond_pct}%</div>}
                    <button onClick={() => applyDefensiveOption(o, false)} className="mt-1 w-full text-[10px] rounded border border-success/30 text-success px-1.5 py-0.5 hover:bg-success/5">이 안 적용</button>
                  </div>
                );
              })}
            </div>
            <p className="text-[10px] text-neutral-400">적용 시 <b>채권 비율(방어 대비 %)</b>과 듀레이션만 폼에 임시 반영됩니다 — 현금밴드는 안 건드립니다(저장 전 정책 불변). 숫자는 직접 수정 가능.</p>
          </div>
        )}
        {field === "interests" && Array.isArray(a.extracted_variables?.suggested_additions) && a.extracted_variables.suggested_additions.length > 0 && (
          <div className="space-y-1">
            <span className="text-[11px] text-neutral-400">추천 관심 분야 (시황·분산 기반 · 클릭하면 추가 — 강요 아님)</span>
            <div className="flex flex-wrap gap-1.5">
              {a.extracted_variables.suggested_additions.map((s: any) => (
                <button key={s.theme} type="button" title={s.reason}
                  onClick={() => { setForm((f) => ({ ...f, interests_text: f.interests_text ? `${f.interests_text}, ${s.theme}` : s.theme })); setSaved(false); }}
                  className="text-[11px] rounded-full border border-primary-100 text-primary-700 px-2 py-0.5 hover:bg-primary-50">
                  + {s.theme} <span className="text-neutral-400">· {s.kind === "adjacent" ? "보완" : "분산"}</span>
                </button>
              ))}
            </div>
          </div>
        )}
        {a.extracted_variables && Object.keys(a.extracted_variables).length > 0 && !(field === "defensive" && Array.isArray(a.extracted_variables?.options)) && (
          <div className="text-[11px]">
            <span className="text-neutral-400">추출된 변수</span>
            <pre className="mt-0.5 rounded bg-white border border-neutral-100 p-1.5 overflow-x-auto text-neutral-600">{JSON.stringify(a.extracted_variables, null, 1)}</pre>
          </div>
        )}
        {a.missing_points?.length > 0 && (
          <div className="text-[11px]"><span className="text-neutral-400">부족한 점</span>
            <ul className="mt-0.5 space-y-0.5">{a.missing_points.map((m, i) => <li key={i} className="text-neutral-600">· {m}</li>)}</ul></div>
        )}
        {a.risk_warnings?.length > 0 && (
          <div className="text-[11px]"><span className="text-warning">위험 경고</span>
            <ul className="mt-0.5 space-y-0.5">{a.risk_warnings.map((m, i) => <li key={i} className="text-neutral-700">⚠ {m}</li>)}</ul></div>
        )}
        {a.follow_up?.length > 0 && (
          <div className="text-[11px]"><span className="text-neutral-400">추가 질문</span>
            <ul className="mt-0.5 space-y-0.5">{a.follow_up.map((m, i) => <li key={i} className="text-neutral-500">? {m}</li>)}</ul></div>
        )}
        {!(field === "defensive" && Array.isArray(a.extracted_variables?.options)) && (
          <div className="text-[11px] text-neutral-500">
            적용될 필드: <b>{flabel(key as string)}</b> · 변경 전→후:
            <span className="text-neutral-400"> {before || "미설정"}</span> → <span className="text-neutral-900 font-medium">{a.suggested_text}</span>
          </div>
        )}
        <div className="flex flex-wrap gap-1.5 pt-1">
          {!(field === "defensive" && Array.isArray(a.extracted_variables?.options)) && (
            <>
              <button onClick={() => applyFieldAdvice(field, false)} className="text-[11px] rounded border border-success/30 text-success px-2 py-1 hover:bg-success/5">그대로 적용</button>
              <button onClick={() => applyFieldAdvice(field, true)} className="text-[11px] rounded border border-primary-100 text-primary-700 px-2 py-1 hover:bg-primary-50">수정해서 적용</button>
            </>
          )}
          <button onClick={() => ignoreFieldAdvice(field)} className="text-[11px] rounded border border-neutral-200 text-neutral-400 px-2 py-1 hover:bg-neutral-50">무시</button>
          {a.follow_up?.length > 0 && (
            <button onClick={() => { setConsultInput(a.follow_up[0]); setConsultOpen(true); }} className="text-[11px] rounded border border-neutral-200 text-neutral-500 px-2 py-1 hover:bg-neutral-50">추가 질문</button>
          )}
        </div>
        <p className="text-[10px] text-neutral-400">‘그대로 적용’은 폼에만 반영됩니다(임시). 저장해야 policy version이 생성됩니다 · API 미사용.</p>
      </div>
    );
  };

  return (
    <div className="max-w-2xl mx-auto px-5 py-10 space-y-6">
      <Link href={`/accounts/${id}`} className="text-sm text-neutral-500 flex items-center gap-1 hover:text-primary">
        <ArrowLeft className="w-4 h-4" /> 계좌 화면
      </Link>

      <div>
        <h1 className="text-2xl font-bold text-neutral-900">운용 전략 (대전제 → 중전제)</h1>
        <p className="text-sm text-neutral-500 mt-1">
          종목부터 고르지 않습니다. 먼저 <b>어떻게 운용할지</b>와 <b>관심 분야·생각</b>을 정합니다.
          컨셉을 자유롭게 쓰면 제(Claude)가 정리하고 <b>부족한 점도 짚어</b> 함께 발전시킵니다. 언제든 수정 가능합니다.
        </p>
      </div>

      {/* 투자 스타일(policy_type) — 시작점 템플릿. 강요 아님, 언제든 수정 가능 */}
      <Card>
        <CardHeader><CardTitle>투자 스타일 선택 (정책 템플릿 · 시작점)</CardTitle></CardHeader>
        <CardBody className="space-y-3">
          <p className="text-sm text-neutral-500">
            하나의 철학을 강요하지 않습니다. 스타일을 고르면 한도·비중의 <b>기본값(템플릿)</b>이 깔리고,
            아래에서 값을 직접 바꾸면 <b>사용자 수정</b>으로 기록됩니다. 안전·정합성 규칙(hard rule)은 잠겨 변경할 수 없습니다.
          </p>
          <div className="flex flex-wrap gap-2">
            {POLICY_TYPES.map((t) => {
              const active = form.policy_type === t.value;
              return (
                <button key={t.value} type="button"
                  onClick={() => set("policy_type", active ? "" : t.value)}
                  className={`text-left rounded-xl border px-3 py-2 transition ${active ? "border-primary bg-primary-50" : "border-neutral-200 hover:border-primary-100"}`}>
                  <div className={`text-sm font-medium ${active ? "text-primary-700" : "text-neutral-700"}`}>{t.label}</div>
                  <div className="text-[11px] text-neutral-400 mt-0.5">{t.desc}</div>
                </button>
              );
            })}
          </div>
          {policy?.policy_type && policy.policy_type !== form.policy_type && (
            <p className="text-[11px] text-warning">
              현재 저장된 정책 스타일: <b>{POLICY_TYPES.find((p) => p.value === policy.policy_type)?.label ?? policy.policy_type}</b>
              {" "}— 변경 후 저장하면 새 정책 버전이 생성됩니다.
            </p>
          )}
        </CardBody>
      </Card>

      {/* 대전제 */}
      <Card>
        <CardHeader><CardTitle>① 대전제 — 어떻게 운용할까</CardTitle></CardHeader>
        <CardBody className="space-y-4">
          <div>
            <Label htmlFor="posture">컨셉을 자유롭게 말해보세요 (대전제로 정리해 드립니다)</Label>
            <Textarea
              id="posture" rows={5}
              placeholder="예: 공격적으로 가되 숏은 보험 수준. 현금은 20~40% 유동적, 지금은 50%. 관심은 로봇·바이오·양자. 전세계로 분산, 개별주는 저평가 3종목 10%까지. 너무 빠르게 바꾸진 않음."
              value={form.posture_text}
              onChange={(e) => set("posture_text", e.target.value)}
            />
            <div className="flex items-center gap-2 mt-2">
              <Button type="button" variant="outline" size="sm" onClick={distill} disabled={distilling}>
                <Sparkles className="w-4 h-4" /> {distilling ? "정리 중…" : "이 컨셉에서 대전제 정리"}
              </Button>
              <span className="text-[11px] text-neutral-400">아래 값은 자동 정리 후에도 직접 수정 가능</span>
            </div>
            {distillNote && <p className="text-[11px] text-primary mt-2">{distillNote}</p>}
          </div>

          {/* 정리된 키워드 */}
          {keywords.length > 0 && (
            <div>
              <Label>정리된 키워드</Label>
              <div className="flex flex-wrap gap-2 mt-1">
                {keywords.map((k, i) => (
                  <span key={i} className="text-xs rounded-full bg-primary-50 text-primary-700 px-2.5 py-1">{k}</span>
                ))}
              </div>
            </div>
          )}

          {/* 개선 제안 (조언) — 규칙 + 외부사례 + 우리 메모리. 반영/보류는 사람이 결정 */}
          {(advice.length > 0 || gaps.length > 0) && (
            <div className="rounded-xl border border-warning/20 bg-warning/5 p-3 space-y-2">
              <div className="flex items-center gap-1.5 text-sm font-medium text-warning">
                <Lightbulb className="w-4 h-4" /> 개선 제안 (조언) — 반영 여부는 사장님이 결정
              </div>
              {advice.length === 0 ? (
                <ul className="space-y-1">
                  {gaps.map((g, i) => <li key={i} className="text-xs text-neutral-600">· {g}</li>)}
                </ul>
              ) : (
                <div className="space-y-2">
                  {advice.map((it) => (
                    <div key={it.id} className={`rounded-lg border p-2.5 bg-white ${it.status === "accepted" ? "border-success/30" : it.status === "rejected" ? "border-neutral-200 opacity-50" : "border-neutral-200"}`}>
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="flex items-center gap-1.5">
                            <span className="text-[10px] rounded bg-neutral-100 text-neutral-500 px-1.5 py-0.5 shrink-0">{srcLabel(it.source)}</span>
                            <span className="text-sm font-medium text-neutral-800">{it.title.replace(/^\[메모리\]\s*/, "")}</span>
                          </div>
                          <p className="text-xs text-neutral-500 mt-1">{it.detail}</p>
                        </div>
                        {it.status === "open" ? (
                          <div className="flex gap-1 shrink-0">
                            <button onClick={() => decideAdvice(it.id, true)} className="text-xs rounded border border-success/30 text-success px-2 py-1 hover:bg-success/5">반영</button>
                            <button onClick={() => decideAdvice(it.id, false)} className="text-xs rounded border border-neutral-200 text-neutral-400 px-2 py-1 hover:bg-neutral-50">보류</button>
                          </div>
                        ) : (
                          <span className={`text-xs shrink-0 ${it.status === "accepted" ? "text-success" : "text-neutral-400"}`}>{it.status === "accepted" ? "반영함" : "보류"}</span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
              <p className="text-[11px] text-neutral-400">출처: 규칙 / 외부사례(벤치마크) / 메모리(우리 agent 분석·외부조사 누적). 반영은 아래 값을 직접 수정해 저장하시면 됩니다.</p>
            </div>
          )}

          {/* 구조화된 대전제 (편집 가능) */}
          <div>
            <Label>성향</Label>
            <Chips value={form.risk_tolerance} onPick={(v) => set("risk_tolerance", v)}
              options={[["aggressive", "공격적"], ["neutral", "중립"], ["defensive", "방어적"]]} />
          </div>
          <div>
            <Label>숏(인버스) 허용</Label>
            <Chips value={form.short_policy} onPick={(v) => set("short_policy", v)}
              options={[["none", "안 함"], ["insurance", "보험 수준"], ["active", "적극"]]} />
          </div>
          <div>
            <Label>조정 속도 (분할 빈도)</Label>
            <Chips value={form.rebalance_pace} onPick={(v) => set("rebalance_pace", v)}
              options={[["slow", "천천히"], ["normal", "보통"], ["fast", "빠르게"]]} />
            <div className="flex flex-wrap gap-1.5 mt-1.5">
              <FieldBtn field="pace" type="improve" label="AI 분할 계획" />
            </div>
            <AdvicePanel field="pace" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <div className="flex items-center gap-1.5"><Label htmlFor="cmin">현금 밴드 하한(%)</Label><SourceBadge src={srcOf("cash_min_pct")} /></div>
              <Input id="cmin" type="number" placeholder="예: 20" value={form.cash_min_pct} readOnly={lockedField("cash_min_pct")} disabled={lockedField("cash_min_pct")} onChange={(e) => set("cash_min_pct", e.target.value)} />
            </div>
            <div>
              <div className="flex items-center gap-1.5"><Label htmlFor="cmax">현금 밴드 상한(%)</Label><SourceBadge src={srcOf("cash_max_pct")} /></div>
              <Input id="cmax" type="number" placeholder="예: 40" value={form.cash_max_pct} readOnly={lockedField("cash_max_pct")} disabled={lockedField("cash_max_pct")} onChange={(e) => set("cash_max_pct", e.target.value)} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <div className="flex items-center gap-1.5"><Label htmlFor="icap">개별주 총합 한도(%)</Label><SourceBadge src={srcOf("individual_cap_pct")} /></div>
              <Input id="icap" type="number" placeholder="예: 10" value={form.individual_cap_pct} readOnly={lockedField("individual_cap_pct")} disabled={lockedField("individual_cap_pct")} onChange={(e) => set("individual_cap_pct", e.target.value)} />
            </div>
            <div>
              <div className="flex items-center gap-1.5"><Label htmlFor="icnt">개별 종목 수</Label><SourceBadge src={srcOf("individual_count")} /></div>
              <Input id="icnt" type="number" placeholder="예: 3" value={form.individual_count} readOnly={lockedField("individual_count")} disabled={lockedField("individual_count")} onChange={(e) => set("individual_count", e.target.value)} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="region">지역 비중</Label>
              <Input id="region" placeholder="예: 미국 50 / 한국 40 / 기타 10" value={form.region_pref} onChange={(e) => set("region_pref", e.target.value)} />
              <div className="flex flex-wrap gap-1.5 mt-1.5">
                <FieldBtn field="region" type="improve" label="AI 조언" />
                <FieldBtn field="region" type="risk_check" label="합계 검증" />
              </div>
              <AdvicePanel field="region" />
              <p className="text-[11px] text-neutral-400 mt-1">숫자로 적으면 지역별 anchor로 분해됩니다. 합계 100% 권장(아니면 경고).</p>
            </div>
            <div>
              <Label htmlFor="horizon">투자 기간 · 목적</Label>
              <Input id="horizon" placeholder="예: 3~5년 장기" value={form.horizon} onChange={(e) => set("horizon", e.target.value)} />
            </div>
          </div>
          {/* 채권/국채 — 방어자산 bucket 내 배분(현금밴드에 무조건 더하지 않음) */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="bondpct">방어자산 중 채권/국채 비중(%)</Label>
              <Input id="bondpct" type="number" placeholder="예: 25 (방어자산의 25%를 국채로)" value={form.bond_target_pct} onChange={(e) => set("bond_target_pct", e.target.value)} />
              <div className="flex flex-wrap gap-1.5 mt-1.5">
                <FieldBtn field="defensive" type="improve" label="AI 채권 비중 조언" />
              </div>
              <AdvicePanel field="defensive" />
              <p className="text-[11px] text-neutral-400 mt-1">현금밴드(방어자산 총량)는 위에서 설정합니다. 여기는 <b>그 방어자산 중 몇 %를 국채로</b> 가져갈지입니다(나머지는 순현금). 예: 방어 40% · 국채 25% → 국채 10%(전체) + 순현금 30%. 즉 전체 기준이 아니라 <b>방어자산 대비 비율</b>입니다.</p>
            </div>
            <div>
              <Label>채권 듀레이션</Label>
              <Chips value={form.bond_duration_pref} onPick={(v) => set("bond_duration_pref", v)}
                options={[["short", "단기"], ["intermediate", "중기"], ["long", "장기"], ["mixed", "혼합/사다리"]]} />
            </div>
          </div>

          {/* 컴파일된 한도값 + 출처 — 백엔드 policy_rules effective. hard rule 은 잠금 표시 */}
          {policy?.effective ? (
            <div className="rounded-xl border border-neutral-200 p-3 space-y-2">
              <Label>리스크 한도 (출처 표시 · hard rule 잠금)</Label>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
                {[
                  ["single_name_max_pct", "단일 종목 상한", "%"],
                  ["sector_max_pct", "섹터 상한", "%"],
                  ["inverse_max_pct", "인버스/숏 상한", "%"],
                  ["leverage_max_pct", "레버리지 상한", "%"],
                  ["one_order_cap_pct", "1주문 상한", "%"],
                ].map(([f, label, unit]) => {
                  const v = policy.effective?.[f];
                  return (
                    <div key={f} className="flex items-center justify-between gap-2 text-sm">
                      <span className="text-neutral-500">{label}</span>
                      <span className="flex items-center gap-1.5">
                        <span className="text-neutral-800 font-medium">{v == null ? "—" : `${v}${unit}`}</span>
                        <SourceBadge src={srcOf(f)} />
                      </span>
                    </div>
                  );
                })}
              </div>
              <p className="text-[11px] text-neutral-400">
                기본값(템플릿) 위에 사용자 수정이 얹힙니다. 단일 종목·인버스·레버리지·1주문 상한 등 안전 한도는
                리스크 게이트가 강제하며, hard rule 표시 항목은 변경할 수 없습니다.
              </p>
            </div>
          ) : (
            <p className="text-[11px] text-neutral-400">
              컴파일된 정책값이 아직 없습니다. 스타일을 고르고 값을 저장하면 한도·출처가 표시됩니다.
            </p>
          )}

          {/* hard rule(잠금) — 절대 변경 불가. 무시된 override / 차단된 disable 도 정직히 표시 */}
          {(policy?.hard_rules?.length || policy?.ignored_overrides?.length || policy?.blocked_disables?.length) ? (
            <div className="rounded-xl border border-error/20 bg-error/5 p-3 space-y-2">
              <div className="flex items-center gap-1.5 text-sm font-medium text-error">
                <Lock className="w-4 h-4" /> 불변 규칙 (hard rule) — 변경 불가
              </div>
              {policy?.hard_rules?.length ? (
                <div className="flex flex-wrap gap-1.5">
                  {policy.hard_rules.map((r) => (
                    <span key={r} className="text-[10px] rounded-full border border-error/20 bg-white text-error px-2 py-0.5">{r}</span>
                  ))}
                </div>
              ) : null}
              {policy?.ignored_overrides?.length ? (
                <p className="text-[11px] text-neutral-500">무시된 수정 시도(hard rule): {policy.ignored_overrides.join(", ")}</p>
              ) : null}
              {policy?.blocked_disables?.length ? (
                <p className="text-[11px] text-neutral-500">차단된 비활성화 시도(hard rule): {policy.blocked_disables.join(", ")}</p>
              ) : null}
            </div>
          ) : null}
        </CardBody>
      </Card>

      {/* 중전제 */}
      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle>② 중전제 — 관심 분야 + 내 생각</CardTitle>
          <div className="flex items-center gap-2">
            <FieldBtn field="whole" type="reflect" label="전체 정합성 점검" />
            <Button type="button" variant="outline" size="sm" onClick={() => setConsultOpen(true)}>
              <Sparkles className="w-4 h-4" /> Claude 분석 전문가에게 조언 구하기
            </Button>
          </div>
        </CardHeader>
        <CardBody className="space-y-4">
          {fieldErr && (
            <div className="rounded-lg border border-error/30 bg-error/5 p-2.5 text-sm text-error">
              ⚠ {fieldErr}
            </div>
          )}
          <AdvicePanel field="whole" />
          <div>
            <Label htmlFor="interests">관심 분야 / 섹터 / 테마</Label>
            <Textarea id="interests" rows={2} placeholder="예: 로봇, 바이오, 양자컴퓨터"
              value={form.interests_text} onChange={(e) => set("interests_text", e.target.value)} />
            <div className="flex flex-wrap gap-1.5 mt-1.5">
              <FieldBtn field="interests" type="improve" label="AI 개선" />
              <FieldBtn field="interests" type="risk_check" label="리스크 점검" />
            </div>
            <AdvicePanel field="interests" />
            {/* 관심 분야 AI 후보 추천 — neutral(자동반영 없음). 조사 후보로 추가 → 방향 분류 → 저장 흐름 */}
            <div className="mt-3">
              <ThemeSuggestionCards accountId={id} onAdded={onThemeAddedToResearch} />
            </div>
          </div>

          {/* 중전제 — 관심 테마별 항목(역할 + 메모리 분석) */}
          {adviceThemes.length > 0 && (
            <div className="space-y-2">
              <Label>관심 테마별 정리 (역할 · 메모리 분석)</Label>
              {adviceThemes.map((t, i) => {
                const eff = themeDirections[t.theme] ?? t.direction ?? "unknown_direction";
                const reflect = eff === "long_candidate" ? "→ 롱 tilt 반영" : eff === "short_or_hedge_candidate" ? "→ 인버스 bucket" : eff === "mixed_swing" ? "→ 스윙(롱+인버스 페어)" : "→ 미반영";
                return (
                  <div key={i} className="rounded-lg border border-neutral-200 p-2.5">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-medium text-neutral-800">{t.theme}</span>
                      <select value={eff}
                        onChange={(e) => { setThemeDirections((m) => ({ ...m, [t.theme]: e.target.value })); setSaved(false); }}
                        className="text-[11px] rounded border border-neutral-200 px-1.5 py-0.5 bg-white text-neutral-700">
                        <option value="long_candidate">롱 후보</option>
                        <option value="short_or_hedge_candidate">숏/헤지 후보</option>
                        <option value="mixed_swing">롱숏 혼재(스윙)</option>
                        <option value="watch_only">관망</option>
                        <option value="avoid_or_exclude">제외</option>
                        <option value="unknown_direction">방향 미정</option>
                      </select>
                      <span className="text-[10px] text-neutral-400">{reflect}</span>
                      {themeDirections[t.theme] && themeDirections[t.theme] !== (t.direction ?? "unknown_direction") && <span className="text-[10px] text-primary-600">수정됨</span>}
                      {!t.has_memory && <span className="text-[10px] text-neutral-300">메모리 없음</span>}
                    </div>
                    {t.notes?.[0] && <p className="text-xs text-neutral-500 mt-1">{t.notes[0]}</p>}
                  </div>
                );
              })}
              <p className="text-[11px] text-neutral-400">관심 테마는 <b>분석 대상</b>입니다 — 방향을 직접 지정하면 목표비중에 그대로 반영됩니다. <b>롱 후보</b>만 tilt, <b>숏/헤지 후보</b>는 인버스 bucket, <b>관망·제외·방향 미정</b>은 미반영(자동 롱 금지). 변경 후 <b>저장</b>하면 다음 3안 계산부터 적용됩니다.</p>
            </div>
          )}
          <div>
            <Label htmlFor="views">내 생각 / 견해</Label>
            <Textarea id="views" rows={3} placeholder="예: 미국·한국 위주, ETF·액티브 운용사(ARK류)도 OK, 천천히 분할매수, 현금으로 대응."
              value={form.views_text} onChange={(e) => set("views_text", e.target.value)} />
            <div className="flex flex-wrap items-center gap-1.5 mt-2">
              <FieldBtn field="views" type="recommend" label="✨ AI 종합 추천" />
              <FieldBtn field="views" type="improve" label="AI 정리" />
              <FieldBtn field="views" type="find_gaps" label="부족한 점 찾기" />
              <FieldBtn field="views" type="extract" label="정책변수 추출" />
            </div>
            <AdvicePanel field="views" />
            <div className="flex items-center gap-2 mt-2">
              <Button type="button" variant="outline" size="sm" onClick={analyzeMid} disabled={analyzing}>
                <Sparkles className="w-4 h-4" /> {analyzing ? "분석 중…" : "내 생각 정리 · 핵심 아이디어 도출 (AI 분석)"}
              </Button>
              <span className="text-[11px] text-neutral-400">Claude + 메모리 · API 미사용</span>
            </div>
          </div>

          {/* 중전제 AI 분석 결과 */}
          {midAnalysis && (
            <div className="rounded-xl border border-primary-100 bg-primary-50/40 p-3 space-y-3">
              {midAnalysis.ideas?.length > 0 && (
                <div>
                  <div className="text-sm font-medium text-primary-700 mb-1">핵심 아이디어</div>
                  <div className="flex flex-wrap gap-2">
                    {midAnalysis.ideas.map((it: string, i: number) => (
                      <span key={i} className="text-xs rounded-full bg-white border border-primary-100 text-primary-700 px-2.5 py-1">{it}</span>
                    ))}
                  </div>
                </div>
              )}
              {midAnalysis.ai_opinion && (
                <div>
                  <div className="text-sm font-medium text-neutral-700 mb-1">AI 종합 의견 (메모리 기반)</div>
                  <p className="text-xs text-neutral-600 leading-relaxed">{midAnalysis.ai_opinion}</p>
                </div>
              )}
              {midAnalysis.suggestions?.length > 0 && (
                <div>
                  <div className="text-sm font-medium text-neutral-700 mb-1">개선 제안</div>
                  <ul className="space-y-1">
                    {midAnalysis.suggestions.map((s: any, i: number) => (
                      <li key={i} className="text-xs text-neutral-600"><b>{s.title}</b> — {s.detail}</li>
                    ))}
                  </ul>
                </div>
              )}
              <p className="text-[11px] text-neutral-400">반영하려면 위 값(관심·지역·헤지 등)을 수정해 저장하세요. 심층 분석은 제가(Claude) 메모리로 계속 보강합니다.</p>
            </div>
          )}

          <p className="text-[11px] text-neutral-400 flex items-center gap-1">
            <Sparkles className="w-3.5 h-3.5" /> 다음 단계에서 이 관심 분야로 제가 자료를 조사해 목표비중 초안과 근거를 제안합니다 (Claude + 메모리 · API 미사용).
          </p>
        </CardBody>
      </Card>

      {/* 상담 "그대로 적용" 임시 반영 프리뷰 — 저장 전엔 DB policy 불변 */}
      {appliedChanges.length > 0 && (
        <div className="rounded-xl border border-warning/30 bg-warning/5 p-3 space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-sm font-medium text-warning flex items-center gap-1.5">
              <Lightbulb className="w-4 h-4" /> 상담 조언 임시 반영됨 — 아직 저장 전입니다
            </div>
            <button onClick={() => setAppliedChanges([])} className="text-[11px] text-neutral-400 hover:text-neutral-600">프리뷰 닫기</button>
          </div>
          {appliedChanges.map((c, i) => (
            <div key={i} className="rounded-lg border border-neutral-200 bg-white p-2.5 text-xs">
              <div className="font-medium text-neutral-700">적용 필드: {flabel(c.field)}</div>
              <div className="mt-1 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-neutral-600">
                <span className="text-neutral-400">변경 전</span><span className="truncate">{c.before || <i className="text-neutral-300">미설정</i>}</span>
                <span className="text-neutral-400">변경 후</span><span className="text-neutral-900 font-medium">{c.after}</span>
              </div>
            </div>
          ))}
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-neutral-500">
            <span>적용 상태: <b className="text-warning">임시 반영</b></span>
            <span>저장 필요: <b className="text-neutral-700">예</b></span>
            <span>allocation 재계산 필요: <b className="text-neutral-700">예 (저장 후 3안 화면에서)</b></span>
          </div>
          <p className="text-[11px] text-neutral-400">저장해야 policy version이 생성되고 목표비중 3안에 반영됩니다. 저장 전까지는 DB 정책이 바뀌지 않습니다.</p>
        </div>
      )}

      {/* 저장 경고 (지역 합계 100% 아님 / 금리 듀레이션 등) */}
      {saveWarnings.length > 0 && (
        <div className="rounded-xl border border-accent/30 bg-accent/5 p-3">
          <div className="text-sm font-medium text-accent-600 mb-1">확인 필요 (자동 보정하지 않음)</div>
          <ul className="space-y-0.5">
            {saveWarnings.map((w, i) => <li key={i} className="text-xs text-neutral-600">· {w}</li>)}
          </ul>
        </div>
      )}

      <div className="flex items-center gap-3">
        <Button onClick={save} disabled={busy} size="lg">
          {busy ? "저장 중…" : saved ? <><Check className="w-4 h-4" /> 저장됨</> : "저장"}
        </Button>
        <Link href={`/accounts/${id}/strategy/view`}>
          <Button variant="ghost" size="lg">정리 문서 보기</Button>
        </Link>
        <Link href={`/accounts/${id}/allocation`}>
          <Button variant="outline" size="lg">다음: 목표 포트폴리오 3안 <ArrowRight className="w-4 h-4" /></Button>
        </Link>
      </div>

      {/* Claude 분석 전문가 상담 팝업 */}
      {consultOpen && (
        <div className="fixed inset-0 z-50 flex items-end md:items-center justify-center bg-black/30 p-4" onClick={() => setConsultOpen(false)}>
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg max-h-[80vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between px-4 py-3 border-b border-neutral-100">
              <div className="flex items-center gap-2 font-semibold text-neutral-800"><Sparkles className="w-4 h-4 text-primary" /> Claude 분석 전문가 상담</div>
              <button onClick={() => setConsultOpen(false)} className="text-neutral-400 hover:text-neutral-700 text-sm">닫기</button>
            </div>
            <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
              {consultLog.length === 0 && (
                <p className="text-xs text-neutral-400">어떻게 넣을지 한 줄로 물어보세요. 예: “반도체 인버스 비중 얼마?”, “양자 어떻게 넣어?”, “미국/한국 비중?”, “현금 대신 채권?”</p>
              )}
              {consultLog.map((m, i) => (
                <div key={i} className="space-y-1.5">
                  <div className="text-sm text-right"><span className="inline-block bg-primary-50 text-primary-700 rounded-xl px-3 py-1.5">{m.q}</span></div>
                  <div className="rounded-xl bg-neutral-50 px-3 py-2">
                    <p className="text-sm text-neutral-700 leading-relaxed">{m.a}</p>
                    {m.refs?.length > 0 && m.refs.map((r: any, j: number) => (
                      <p key={j} className="text-[11px] text-neutral-400 mt-1">📎 [{r.theme}] {r.note}</p>
                    ))}
                    {m.suggestions?.length > 0 && (
                      <div className="flex flex-wrap gap-2 mt-2">
                        {m.suggestions.map((s: any, j: number) => (
                          <button key={j} onClick={() => applyConsult(s.apply)} className="text-xs rounded-lg border border-success/30 text-success px-2.5 py-1 hover:bg-success/5">
                            그대로 적용 · {s.label}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              ))}
              {consultBusy && <p className="text-xs text-neutral-400">분석 중…</p>}
            </div>
            <div className="px-4 py-3 border-t border-neutral-100 flex gap-2">
              <Input value={consultInput} onChange={(e) => setConsultInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") askConsult(); }}
                placeholder="어떻게 넣을지 간단히 적어보세요…" />
              <Button onClick={askConsult} disabled={consultBusy}>보내기</Button>
            </div>
            <p className="text-[11px] text-neutral-400 px-4 pb-3">즉시 답은 정책 한도+메모리 기반 · 심층 분석은 Claude가 세션에서 보강 · API 미사용. ‘그대로 적용’은 폼에 반영되며 저장은 직접.</p>
          </div>
        </div>
      )}
    </div>
  );
}
