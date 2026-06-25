"use client";

// 3안 배분 도넛 차트(Track3) — 순수 SVG, 외부 차트 라이브러리 금지.
// 차트/숫자 모두 lib/allocation/buckets.ts 의 toBuckets 결과(Bucket[])만 사용 — mock 금지.
import type { Bucket } from "@/lib/allocation/buckets";
import { defensivePct, riskPct, sumPct } from "@/lib/allocation/buckets";

// 각 slice 는 stroke-dasharray 호(arc)로 그린다. 둘레 길이를 비율대로 나눠 채운다.
// pct=0 인 bucket 은 호(arc)에선 건너뛰되, 범례(legend)에는 항상 남긴다(채권 0% 포함).
export function DonutChart({ buckets, size = 220 }: { buckets: Bucket[]; size?: number }) {
  const stroke = Math.max(18, Math.round(size * 0.16));
  const r = (size - stroke) / 2;
  const cx = size / 2;
  const cy = size / 2;
  const circumference = 2 * Math.PI * r;

  // 호 비율의 기준은 입력 pct 합(보통 100). 0 이면 분모 보호.
  const total = sumPct(buckets);
  const denom = total > 0 ? total : 100;

  // 12시 방향에서 시계방향으로 누적.
  let cursor = 0;
  const arcs = buckets
    .filter((b) => b.pct > 0)
    .map((b, i) => {
      const frac = b.pct / denom;
      const dash = frac * circumference;
      const gap = circumference - dash;
      // dashoffset 은 누적 진행분만큼 뒤로 민다(음수로 시계방향).
      const offset = -cursor * circumference;
      cursor += frac;
      return { b, dash, gap, offset, key: `${b.bucket_type}-${i}` };
    });

  const def = defensivePct(buckets);
  const risk = riskPct(buckets);

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={`자산 배분 도넛 차트. 방어 ${def}%, 위험 ${risk}%.`}
    >
      <title>{`방어 ${def}% / 위험 ${risk}% (합계 ${total}%)`}</title>
      {/* 배경 트랙 */}
      <circle cx={cx} cy={cy} r={r} fill="none" stroke="#e2e8f0" strokeWidth={stroke} />
      {/* 호 — 12시 시작(회전 -90deg) */}
      <g transform={`rotate(-90 ${cx} ${cy})`}>
        {arcs.map((a) => (
          <circle
            key={a.key}
            cx={cx}
            cy={cy}
            r={r}
            fill="none"
            stroke={a.b.color}
            strokeWidth={stroke}
            strokeDasharray={`${a.dash} ${a.gap}`}
            strokeDashoffset={a.offset}
          >
            <title>{`${a.b.label} ${a.b.pct}% · ${a.b.role}`}</title>
          </circle>
        ))}
      </g>
      {/* 중앙 라벨: 방어 / 위험 */}
      <text
        x={cx}
        y={cy - 6}
        textAnchor="middle"
        fontSize={size * 0.085}
        fontWeight={600}
        fill="#0f172a"
      >
        방어 {def}%
      </text>
      <text
        x={cx}
        y={cy + size * 0.085 + 2}
        textAnchor="middle"
        fontSize={size * 0.085}
        fontWeight={600}
        fill="#475569"
      >
        위험 {risk}%
      </text>
    </svg>
  );
}

// 범례 — 모든 bucket 표시(채권/국채 0%도 포함). title 로 hover 설명 노출.
export function DonutLegend({ buckets }: { buckets: Bucket[] }) {
  return (
    <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 6 }}>
      {buckets.map((b, i) => (
        <li
          key={`${b.bucket_type}-${i}`}
          title={b.explanation}
          style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, cursor: "help" }}
        >
          <span
            aria-hidden="true"
            style={{
              display: "inline-block",
              width: 12,
              height: 12,
              borderRadius: 3,
              background: b.color,
              flexShrink: 0,
            }}
          />
          <span style={{ fontWeight: 500, color: "#0f172a" }}>{b.label}</span>
          <span style={{ marginLeft: "auto", color: "#334155", fontVariantNumeric: "tabular-nums" }}>
            {b.pct}%
          </span>
          <span style={{ color: "#64748b", minWidth: 56, textAlign: "right" }}>{b.role}</span>
        </li>
      ))}
    </ul>
  );
}
