"use client";

// 투자 정책서 카드(클라이언트) — /api/accounts/[id]/policy(백엔드 policy_rules CLI)에서 effective/출처/hard rule 조회.
// 정책이 없으면 정직한 빈 상태. 가짜 숫자 금지.
import { useEffect, useState } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Lock } from "lucide-react";
import {
  sourceMeta, isHardSource, policyTypeLabel, fieldLabel, fmtPolicyValue,
} from "@/lib/policy/labels";

type PolicyResp = {
  policy_type?: string;
  effective?: Record<string, unknown>;
  hard_rules?: string[];
  ignored_overrides?: string[];
  blocked_disables?: string[];
  soft_disabled?: string[];
  sources?: Record<string, string>;
};

function Badge({ src }: { src?: string }) {
  if (!src) return null;
  const m = sourceMeta(src);
  return (
    <span className={`inline-flex items-center gap-0.5 text-[10px] rounded-full border px-1.5 py-0.5 ${m.cls}`}>
      {m.locked && <Lock className="w-2.5 h-2.5" />}{m.label}
    </span>
  );
}

// effective 표시 순서(주요 한도/스위치). 키가 없으면 건너뜀.
const ORDER = [
  "cash_min_pct", "cash_max_pct", "single_name_max_pct", "sector_max_pct",
  "inverse_max_pct", "leverage_max_pct", "one_order_cap_pct",
  "individual_cap_pct", "individual_count", "pace",
  "use_etf", "use_individual_stocks", "use_bond", "allow_inverse", "allow_themes",
];

export default function PolicyDoc({ id }: { id: number }) {
  const [pol, setPol] = useState<PolicyResp | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await fetch(`/api/accounts/${id}/policy`, { cache: "no-store" });
        const j = await r.json();
        if (alive) setPol(j?.policy ?? null);
      } catch { if (alive) setPol(null); }
      finally { if (alive) setLoading(false); }
    })();
    return () => { alive = false; };
  }, [id]);

  if (loading) {
    return (
      <Card><CardHeader><CardTitle>투자 정책서</CardTitle></CardHeader>
        <CardBody className="text-sm text-neutral-400 py-6">불러오는 중…</CardBody></Card>
    );
  }

  // 정직한 빈 상태 — 백엔드 정책이 아직 없음(가짜 채우지 않음).
  if (!pol || !pol.effective) {
    return (
      <Card><CardHeader><CardTitle>투자 정책서</CardTitle></CardHeader>
        <CardBody className="text-sm text-neutral-500 py-6">
          아직 컴파일된 투자 정책이 없습니다. 전략 편집에서 투자 스타일을 고르고 저장하면
          effective 정책값·출처·hard rule 이 여기에 표시됩니다.
        </CardBody></Card>
    );
  }

  const eff = pol.effective;
  const rows = ORDER.filter((k) => k in eff);
  const overrides = Object.entries(pol.sources ?? {}).filter(([, s]) => s === "user").map(([k]) => k);

  return (
    <Card>
      <CardHeader><CardTitle>투자 정책서 (effective · 출처 · hard rule)</CardTitle></CardHeader>
      <CardBody className="space-y-4 py-2">
        <div className="flex items-center gap-2">
          <span className="text-xs text-neutral-400 w-24">투자 스타일</span>
          <span className="text-sm font-medium text-neutral-800">{policyTypeLabel(pol.policy_type)}</span>
          {pol.policy_type && <span className="text-[10px] rounded bg-neutral-100 text-neutral-500 px-1.5 py-0.5">{pol.policy_type}</span>}
        </div>

        {/* effective 정책 표 + 필드별 출처 */}
        <div>
          <div className="text-xs text-neutral-400 mb-1">effective 정책값</div>
          <div className="rounded-xl border border-neutral-200 divide-y divide-neutral-50">
            {rows.map((k) => (
              <div key={k} className="flex items-center justify-between gap-2 px-3 py-1.5">
                <span className="text-sm text-neutral-600 flex items-center gap-1.5">
                  {fieldLabel(k)}{isHardSource(pol.sources?.[k]) && <Lock className="w-3 h-3 text-error" />}
                </span>
                <span className="flex items-center gap-1.5">
                  <span className="text-sm font-medium text-neutral-800">{fmtPolicyValue(k, eff[k])}</span>
                  <Badge src={pol.sources?.[k]} />
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* 사용자 override 목록 */}
        <div>
          <div className="text-xs text-neutral-400 mb-1">사용자 수정(override)</div>
          {overrides.length ? (
            <div className="flex flex-wrap gap-1.5">
              {overrides.map((k) => (
                <span key={k} className="text-xs rounded-full bg-primary-50 text-primary-700 px-2.5 py-1">{fieldLabel(k)}</span>
              ))}
            </div>
          ) : <p className="text-sm text-neutral-400">없음 (전부 기본값/템플릿)</p>}
        </div>

        {/* 비활성화된 규칙(soft) */}
        {pol.soft_disabled?.length ? (
          <div>
            <div className="text-xs text-neutral-400 mb-1">비활성화된 규칙</div>
            <div className="flex flex-wrap gap-1.5">
              {pol.soft_disabled.map((r) => (
                <span key={r} className="text-xs rounded-full bg-neutral-100 text-neutral-500 px-2.5 py-1">{r}</span>
              ))}
            </div>
          </div>
        ) : null}

        {/* hard rule(잠금) + 무시된/차단된 시도 */}
        <div className="rounded-xl border border-error/20 bg-error/5 p-3 space-y-2">
          <div className="flex items-center gap-1.5 text-sm font-medium text-error">
            <Lock className="w-4 h-4" /> 불변 규칙 (hard rule) — 변경 불가
          </div>
          {pol.hard_rules?.length ? (
            <div className="flex flex-wrap gap-1.5">
              {pol.hard_rules.map((r) => (
                <span key={r} className="text-[10px] rounded-full border border-error/20 bg-white text-error px-2 py-0.5">{r}</span>
              ))}
            </div>
          ) : <p className="text-xs text-neutral-500">표시할 hard rule 이 없습니다.</p>}
          {pol.ignored_overrides?.length ? (
            <p className="text-[11px] text-neutral-500">무시된 수정 시도: {pol.ignored_overrides.join(", ")}</p>
          ) : null}
          {pol.blocked_disables?.length ? (
            <p className="text-[11px] text-neutral-500">차단된 비활성화 시도: {pol.blocked_disables.join(", ")}</p>
          ) : null}
        </div>
        <p className="text-[11px] text-neutral-400">
          이 정책은 decision/allocation 단계에서 그대로 사용되며 provenance(어느 정책 버전이 쓰였는지)로 기록됩니다.
        </p>
      </CardBody>
    </Card>
  );
}
