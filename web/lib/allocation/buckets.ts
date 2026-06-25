// 공통 interface — 3안 차트(Track3)와 전략 요약(Track2)이 **같은 데이터**를 쓴다(mock 금지).
// allocation rows(kind/ref/weight_pct) → 표준 bucket. 방어자산 = pure_cash + bond.

export type BucketType = "pure_cash" | "bond" | "core_etf" | "theme" | "hedge" | "other";

export interface Bucket {
  bucket_type: BucketType;
  label: string;
  pct: number;
  role: string;        // 방어 | 코어 | 성장 tilt | 헤지
  explanation: string; // hover/legend 설명
  color: string;
}

export interface AllocRow { kind: string; ref: string | null; weight_pct: number }

const META: Record<BucketType, { role: string; explanation: string; color: string }> = {
  pure_cash: { role: "방어", explanation: "즉시 매수 가능한 순현금(매수 여력).", color: "#94a3b8" },
  bond: { role: "방어", explanation: "방어자산 중 금리·경기 대응용 채권/국채(현금밴드 안에서 배분).", color: "#38bdf8" },
  core_etf: { role: "코어", explanation: "전체 시장/넓은 지역을 담는 중심 자산 — 테마 변동성을 받쳐줌.", color: "#6366f1" },
  theme: { role: "성장 tilt", explanation: "관심 분야를 반영한 위성 비중(롱 후보만).", color: "#22c55e" },
  hedge: { role: "헤지", explanation: "롱 테마와 분리된 하락 대응(인버스) bucket.", color: "#f59e0b" },
  other: { role: "기타", explanation: "기타.", color: "#cbd5e1" },
};
const THEME_COLORS = ["#22c55e", "#10b981", "#84cc16", "#14b8a6", "#06b6d4", "#a3e635"];

function classify(r: AllocRow): { bt: BucketType; label: string } {
  if (r.kind === "cash") return { bt: "pure_cash", label: "순현금" };
  if (r.kind === "bond") return { bt: "bond", label: r.ref ?? "채권/국채" };
  if (r.kind === "anchor") return { bt: "core_etf", label: r.ref ?? "글로벌 코어 ETF" };
  if (r.kind === "tilt") return { bt: "theme", label: r.ref ?? "테마" };
  if (r.kind === "hedge") return { bt: "hedge", label: r.ref ?? "헤지/인버스" };
  return { bt: "other", label: r.ref ?? "기타" };
}

// allocation rows → buckets. 차트/요약 공통. 채권 0%도 항상 표시(범례 누락 방지).
export function toBuckets(rows: AllocRow[]): Bucket[] {
  const buckets: Bucket[] = [];
  let ti = 0;
  for (const r of rows) {
    const { bt, label } = classify(r);
    const m = META[bt];
    const color = bt === "theme" ? THEME_COLORS[ti++ % THEME_COLORS.length] : m.color;
    buckets.push({ bucket_type: bt, label, pct: r.weight_pct, role: m.role, explanation: m.explanation, color });
  }
  // 채권/국채 0%여도 방어 구조가 보이도록 0% slice 추가(순현금 뒤).
  if (!buckets.some((b) => b.bucket_type === "bond")) {
    const ci = buckets.findIndex((b) => b.bucket_type === "pure_cash");
    const bondB: Bucket = { bucket_type: "bond", label: "채권/국채", pct: 0, role: "방어", explanation: META.bond.explanation, color: META.bond.color };
    if (ci >= 0) buckets.splice(ci + 1, 0, bondB); else buckets.push(bondB);
  }
  return buckets;
}

export const sumPct = (b: Bucket[]) => Math.round(b.reduce((a, x) => a + x.pct, 0) * 10) / 10;
export const defensivePct = (b: Bucket[]) =>
  Math.round(b.filter((x) => x.bucket_type === "pure_cash" || x.bucket_type === "bond").reduce((a, x) => a + x.pct, 0) * 10) / 10;
export const riskPct = (b: Bucket[]) => Math.round((100 - defensivePct(b)) * 10) / 10;
