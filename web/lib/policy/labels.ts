// 투자 정책(policy) 출처/스타일 라벨·스타일 맵 — UI 전용(웹 조회).
// SSOT 는 백엔드 policy_rules.py(default/template/user/hard). 여기엔 표시용 라벨만 둔다(하드코딩 정책값 0).

// policy_rules CLI 가 내려주는 source 값 → 사람이 읽을 라벨 + 뱃지 스타일(Tailwind).
export type PolicySource = "default" | "template" | "user" | "agent" | "hard";

export type SourceMeta = { label: string; cls: string; locked?: boolean };

// neutral=기본값, primary=사용자, template=템플릿, accent=Agent 제안, danger/lock=hard rule.
export const SOURCE_META: Record<PolicySource, SourceMeta> = {
  default: { label: "기본값", cls: "bg-neutral-100 text-neutral-500 border-neutral-200" },
  template: { label: "템플릿", cls: "bg-accent/10 text-accent-600 border-accent/20" },
  user: { label: "사용자 수정", cls: "bg-primary-50 text-primary-700 border-primary-100" },
  agent: { label: "Agent 제안", cls: "bg-warning/10 text-warning border-warning/20" },
  hard: { label: "🔒 hard rule", cls: "bg-error/10 text-error border-error/20", locked: true },
};

// 알 수 없는 source 도 정직하게 표시(가짜 라벨 금지).
export function sourceMeta(src: string | undefined | null): SourceMeta {
  if (src && src in SOURCE_META) return SOURCE_META[src as PolicySource];
  return { label: src ?? "—", cls: "bg-neutral-100 text-neutral-400 border-neutral-200" };
}

export function isHardSource(src: string | undefined | null): boolean {
  return src === "hard";
}

// 6 투자 스타일(policy_type) — 백엔드 TEMPLATES 와 동일 키. 라벨/설명은 표시용.
export type PolicyType =
  | "single_stock_focus" | "etf_diversified" | "cash_defensive"
  | "growth_theme" | "dividend_income" | "custom";

export const POLICY_TYPES: { value: PolicyType; label: string; desc: string }[] = [
  { value: "single_stock_focus", label: "개별주 집중형", desc: "소수 종목 집중 · 단일한도↑" },
  { value: "etf_diversified", label: "ETF 분산형", desc: "ETF 중심 · 국가/섹터 분산" },
  { value: "cash_defensive", label: "현금/방어형", desc: "현금밴드↑ · 안정자산 중심" },
  { value: "growth_theme", label: "성장 테마형", desc: "테마 tilt · 분할진입" },
  { value: "dividend_income", label: "배당/인컴형", desc: "배당·리츠·채권 인컴" },
  { value: "custom", label: "사용자 자유형", desc: "모든 한도 직접 설정" },
];

export function policyTypeLabel(t: string | null | undefined): string {
  return POLICY_TYPES.find((p) => p.value === t)?.label ?? (t ?? "—");
}

// effective 정책 필드 → 한글 라벨 + 단위(표시용).
export const FIELD_META: Record<string, { label: string; unit?: string }> = {
  cash_min_pct: { label: "현금 하한", unit: "%" },
  cash_max_pct: { label: "현금 상한", unit: "%" },
  single_name_max_pct: { label: "단일 종목 상한", unit: "%" },
  sector_max_pct: { label: "섹터 상한", unit: "%" },
  inverse_max_pct: { label: "인버스/숏 상한", unit: "%" },
  leverage_max_pct: { label: "레버리지 상한", unit: "%" },
  one_order_cap_pct: { label: "1주문 상한", unit: "%" },
  individual_cap_pct: { label: "개별주 총합 한도", unit: "%" },
  individual_count: { label: "개별 종목 수", unit: "종목" },
  rebalance_rounds_min: { label: "분할 최소 회차", unit: "회" },
  rebalance_rounds_max: { label: "분할 최대 회차", unit: "회" },
  pace: { label: "조정 속도" },
  use_etf: { label: "ETF 사용" },
  use_individual_stocks: { label: "개별주 사용" },
  use_bond: { label: "채권 사용" },
  allow_inverse: { label: "인버스 허용" },
  allow_themes: { label: "테마 tilt 허용" },
};

export function fieldLabel(f: string): string {
  return FIELD_META[f]?.label ?? f;
}

// effective 값 표시(불리언·숫자·문자). 단위 부착.
export function fmtPolicyValue(field: string, v: unknown): string {
  if (v === true) return "사용";
  if (v === false) return "미사용";
  if (v == null) return "—";
  const unit = FIELD_META[field]?.unit ?? "";
  return `${v}${unit}`;
}
