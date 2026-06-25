"use client";

import { useState, useCallback } from "react";
import { Sparkles, AlertTriangle, FlaskConical, Search } from "lucide-react";

// 관심 분야 AI 후보 제안 — **자동 투자 추천 아님**. 후보는 neutral(방향 미정).
// 흐름: 제안 → [조사 후보로 추가] → 방향 분류 → 임시 반영 → 저장 → allocation 재계산.
// [조사 후보로 추가]는 policy/allocation 에 직접 반영하지 않는다(record_action 만).

type Candidate = {
  id: number;
  source_theme: string;
  candidate_theme: string;
  candidate_type: "adjacent" | "complement" | "diversify" | "hedge" | "watch";
  reason: string;
  relationship: string;
  suggested_role: string;
  direction: string;          // 항상 unknown_direction (neutral)
  confidence: number;
  evidence_freshness?: "fresh" | "stale" | "conflicting" | "none";
  evidence_source?: string | null;
  evidence_ids?: number[];
  ignored_count?: number;
  deprioritized?: boolean;
  user_action?: string;
};

type SuggestResp = {
  ok: boolean;
  candidates?: Candidate[];
  by_type?: Record<string, number>;
  disclaimer?: string;
  note?: string;
  error?: string;
};

const TYPE_LABEL: Record<string, string> = {
  adjacent: "추가 조사 후보 (인접)",
  complement: "보완 후보",
  diversify: "분산 후보",
  hedge: "헤지 후보 (검토용)",
  watch: "관찰 후보",
};
const TYPE_CLS: Record<string, string> = {
  adjacent: "border-primary-100 bg-primary-50/40 text-primary-700",
  complement: "border-sky-200 bg-sky-50/40 text-sky-700",
  diversify: "border-emerald-200 bg-emerald-50/40 text-emerald-700",
  hedge: "border-amber-200 bg-amber-50/40 text-amber-700",
  watch: "border-neutral-200 bg-neutral-50 text-neutral-600",
};
const ROLE_LABEL: Record<string, string> = {
  core: "코어", growth_tilt: "성장 tilt", hedge: "헤지", defensive: "방어", watch: "관찰",
};
const FRESH_LABEL: Record<string, string> = {
  fresh: "최신 근거", stale: "오래된 근거(stale)", conflicting: "엇갈린 근거(conflicting)", none: "근거 없음",
};
const FRESH_CLS: Record<string, string> = {
  fresh: "text-emerald-600", stale: "text-amber-600", conflicting: "text-rose-600", none: "text-neutral-400",
};
const ORDER: Candidate["candidate_type"][] = ["adjacent", "complement", "diversify", "hedge", "watch"];

export default function ThemeSuggestionCards(
  { accountId, onAdded }: { accountId: string | number; onAdded?: (theme: string) => void },
) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [data, setData] = useState<SuggestResp | null>(null);
  const [actions, setActions] = useState<Record<number, string>>({});

  const load = useCallback(async () => {
    setBusy(true); setErr(null);
    try {
      const r = await fetch(`/api/accounts/${accountId}/theme-suggestions`, { cache: "no-store" });
      if (r.status === 401 || r.status === 403) {
        setErr(r.status === 403 ? "이 계좌에 대한 권한이 없습니다." : "로그인이 필요합니다 — 다시 로그인하세요.");
        setBusy(false);
        return;
      }
      const j: SuggestResp = await r.json();
      if (j.ok) { setData(j); setOpen(true); setActions({}); }
      else setErr(j.error || "후보를 생성하지 못했습니다.");
    } catch {
      setErr("네트워크 오류 — 다시 시도하세요.");
    }
    setBusy(false);
  }, [accountId]);

  const record = useCallback(async (candidate_id: number, user_action: string, theme?: string) => {
    // 낙관적 표시 — 행동은 append-only 기록일 뿐(policy/allocation 불변).
    setActions((m) => ({ ...m, [candidate_id]: user_action }));
    try {
      await fetch(`/api/accounts/${accountId}/theme-suggestions`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ candidate_id, user_action }),
      });
      // [조사 후보로 추가] 성공 시 부모에 알림 → 관심 분야/관심 테마별 정리에 '방향 미정'으로 등장.
      // (백엔드가 interests 등재까지 처리. 콜백은 화면 갱신 트리거일 뿐 — 자동 long/policy 반영 없음.)
      if (user_action === "added_to_research" && theme) onAdded?.(theme);
    } catch { /* 행동 기록 실패는 UI 동작을 막지 않음 */ }
  }, [accountId, onAdded]);

  const cands = data?.candidates ?? [];
  const grouped = ORDER.map((t) => ({ type: t, items: cands.filter((c) => c.candidate_type === t) }))
    .filter((g) => g.items.length > 0);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <button type="button" onClick={load} disabled={busy}
          className="text-[11px] rounded-full border border-primary-100 text-primary-700 px-2.5 py-1 hover:bg-primary-50 disabled:opacity-50">
          <Sparkles className="w-3 h-3 inline -mt-0.5 mr-0.5" />{busy ? "후보 찾는 중…" : "AI 후보 추천"}
        </button>
        <span className="text-[11px] text-neutral-400">인접·보완·분산·헤지·관찰 후보 (Claude + 메모리 · API 미사용)</span>
      </div>

      {err && (
        <div className="rounded-lg border border-error/30 bg-error/5 p-2.5 text-sm text-error">⚠ {err}</div>
      )}

      {open && data && (
        <div className="rounded-xl border border-primary-100 bg-primary-50/30 p-3 space-y-3">
          <div className="flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-500 shrink-0 mt-0.5" />
            <p className="text-[11px] text-neutral-600 leading-relaxed">
              <b className="text-amber-700">바로 반영 금지</b> — 후보는 모두 <b>방향 미정(neutral)</b>이며 자동으로
              policy/목표비중/주문에 들어가지 않습니다. [조사 후보로 추가] 후 방향을 정하고 저장해야 반영됩니다.
            </p>
          </div>

          {cands.length === 0 && (
            <p className="text-xs text-neutral-500">{data.note ?? "후보가 없습니다 — 관심 테마를 입력해 주세요."}</p>
          )}

          {grouped.map((g) => (
            <div key={g.type} className="space-y-1.5">
              <div className="text-[11px] font-medium text-neutral-500">{TYPE_LABEL[g.type]}</div>
              <div className="grid sm:grid-cols-2 gap-2">
                {g.items.map((c) => {
                  const acted = actions[c.id];
                  const fresh = c.evidence_freshness ?? "none";
                  return (
                    <div key={c.id}
                      className={`rounded-lg border p-2.5 space-y-1.5 ${TYPE_CLS[c.candidate_type]} ${c.deprioritized ? "opacity-60" : ""}`}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-semibold text-neutral-800">{c.candidate_theme}</span>
                        <span className="text-[10px] rounded-full bg-white/70 border border-neutral-200 px-1.5 py-0.5 text-neutral-500">
                          {TYPE_LABEL[c.candidate_type].split(" ")[0]}
                        </span>
                      </div>
                      <p className="text-[11px] text-neutral-600 leading-snug">{c.reason}</p>
                      <div className="grid grid-cols-[auto_1fr] gap-x-1.5 gap-y-0.5 text-[10px] text-neutral-500">
                        <span className="text-neutral-400">관계</span><span>{c.relationship}</span>
                        <span className="text-neutral-400">역할</span><span>{ROLE_LABEL[c.suggested_role] ?? c.suggested_role}</span>
                        <span className="text-neutral-400">위험</span>
                        <span>방향 미정(neutral) — 매수 신호 아님{c.candidate_type === "hedge" ? " · 헤지는 소액·기간 한정 검토" : ""}</span>
                        <span className="text-neutral-400">자료조사</span>
                        <span className={FRESH_CLS[fresh]}>
                          {FRESH_LABEL[fresh]}{c.evidence_source ? ` · ${c.evidence_source}` : ""} (조사 필요)
                        </span>
                      </div>
                      <div className="flex items-center justify-between pt-0.5">
                        <span className="text-[10px] text-neutral-400">신뢰도 {Math.round((c.confidence ?? 0) * 100)}%
                          {c.deprioritized ? " · 반복 무시(후순위)" : ""}</span>
                        {acted ? (
                          <span className="text-[10px] text-emerald-600">
                            {acted === "added_to_research" ? "관심 테마에 추가됨 (방향 미정)" : acted === "ignored" ? "무시함" : acted}
                          </span>
                        ) : (
                          <div className="flex gap-1">
                            <button onClick={() => record(c.id, "added_to_research", c.candidate_theme)}
                              className="text-[10px] rounded border border-success/30 text-success px-1.5 py-0.5 hover:bg-success/5 inline-flex items-center gap-0.5">
                              <FlaskConical className="w-3 h-3" /> 조사 후보로 추가
                            </button>
                            <button onClick={() => record(c.id, "ignored")}
                              className="text-[10px] rounded border border-neutral-200 text-neutral-400 px-1.5 py-0.5 hover:bg-neutral-50">
                              무시
                            </button>
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}

          <p className="text-[10px] text-neutral-400 flex items-center gap-1">
            <Search className="w-3 h-3" />
            [조사 후보로 추가]는 자료조사 큐에만 들어가며 <b>policy 에 직접 반영되지 않습니다</b>.
            방향을 정하고 저장해야 목표비중이 재계산됩니다.
          </p>
        </div>
      )}
    </div>
  );
}
