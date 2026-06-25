"use client";

// 일별 포트폴리오 추이 — 총자산 / 자산군(bucket) 비중 / 종목별 추이.
// 차트는 전부 순수 SVG (외부 차트 라이브러리 금지). 데이터는 /history-series API(운영 DB) 만 사용.
// 정직 원칙: 보유종목 동기화 전(holdings_tracked=false)이면 종목별 추이는 빈 안내로, 가짜 숫자 0.
import { useEffect, useState, useCallback } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { TrendingUp, RefreshCw } from "lucide-react";

type HoldingPoint = { date: string; value: number; weight: number };
type Exposure = {
  long_pct: number;
  short_pct: number;
  net_pct: number;
  gross_pct: number;
  hedge_exposure_pct: number;
  theme_exposure: Record<string, number>;
  basis?: string;
};
type Series = {
  ok: boolean;
  account_index: number;
  dates: string[];
  total_value: number[];
  cash: number[];
  holdings_by_symbol: Record<string, HoldingPoint[]>;
  bucket_series: Record<string, number[]>; // 순현금 / 채권 / 위험
  exposure_series: { net: number[]; gross: number[]; hedge: number[]; theme: number[]; long?: number[]; short?: number[] };
  exposure: Exposure | null;
  exposure_tracked: boolean;
  drift_series: (number | null)[];
  holdings_tracked: boolean;
  point_count: number;
  note: string | null;
};

const EXPOSURE_COLORS: Record<string, string> = {
  net: "#6366f1",   // indigo — 순노출
  gross: "#0ea5e9", // sky — 총노출
  long: "#10b981",  // emerald — 롱(anchor+tilt)
  short: "#ef4444", // red — 숏(헤지/인버스)
  theme: "#f59e0b", // amber — 테마(롱 tilt)
  hedge: "#ec4899", // pink — 헤지(숏/인버스)
};
const EXPOSURE_LABELS: Record<string, string> = {
  net: "순노출(net)", gross: "총노출(gross)", long: "롱", short: "숏", theme: "테마", hedge: "헤지",
};
// 차트에 그릴 노출 라인(순서 = 범례 순서). long/short 가 net/gross 구성 요소임을 함께 보여준다.
const EXPOSURE_KEYS: Array<keyof Series["exposure_series"]> = ["gross", "net", "long", "short", "theme", "hedge"];

const BUCKET_COLORS: Record<string, string> = {
  순현금: "#64748b", // slate
  채권: "#0ea5e9", // sky
  위험: "#6366f1", // indigo
};
const LINE_COLORS = ["#6366f1", "#0ea5e9", "#f59e0b", "#10b981", "#ec4899", "#8b5cf6"];

function fmtKrw(n: number): string {
  if (!Number.isFinite(n)) return "-";
  if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + "억";
  if (Math.abs(n) >= 1e4) return Math.round(n / 1e4).toLocaleString() + "만";
  return Math.round(n).toLocaleString();
}

// ---- 순수 SVG sparkline ----
function Sparkline({ values, color = "#6366f1", w = 280, h = 56 }: {
  values: number[]; color?: string; w?: number; h?: number;
}) {
  if (values.length === 0) return null;
  const pad = 4;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const n = values.length;
  const x = (i: number) => (n === 1 ? w / 2 : pad + (i / (n - 1)) * (w - 2 * pad));
  const y = (v: number) => h - pad - ((v - min) / span) * (h - 2 * pad);
  const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${pad},${h - pad} ${pts} ${(w - pad).toFixed(1)},${h - pad}`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} preserveAspectRatio="none" role="img" aria-label="총자산 추이">
      <polygon points={area} fill={color} opacity={0.08} />
      <polyline points={pts} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      {n === 1 && <circle cx={x(0)} cy={y(values[0])} r={3} fill={color} />}
    </svg>
  );
}

// ---- 순수 SVG 스택 막대(자산군 비중 변화) ----
function BucketBars({ dates, bucketSeries }: { dates: string[]; bucketSeries: Record<string, number[]> }) {
  const order = ["위험", "채권", "순현금"]; // 아래에서 위로 쌓는 순서(위험을 바닥)
  const h = 120;
  const n = dates.length;
  if (n === 0) return null;
  const barGap = 2;
  const totalGap = barGap * (n + 1);
  const barW = Math.max(2, (100 - 0) / n); // % 단위 폭(컨테이너 100%)
  return (
    <svg viewBox={`0 0 100 ${h}`} width="100%" height={h} preserveAspectRatio="none" role="img" aria-label="자산군 비중 변화">
      {dates.map((_, i) => {
        const x = (i / n) * 100 + barGap;
        const wpct = (100 / n) - barGap;
        let acc = 0;
        return (
          <g key={i}>
            {order.map((k) => {
              const pct = bucketSeries[k]?.[i] ?? 0;
              const segH = (pct / 100) * h;
              const yTop = h - acc - segH;
              acc += segH;
              if (segH <= 0) return null;
              return <rect key={k} x={x} y={yTop} width={Math.max(0, wpct)} height={segH} fill={BUCKET_COLORS[k]} opacity={0.9} />;
            })}
          </g>
        );
      })}
    </svg>
  );
}

// ---- 순수 SVG 다중 라인(종목별 비중 추이) ----
function HoldingLines({ dates, holdings }: { dates: string[]; holdings: Record<string, HoldingPoint[]> }) {
  const syms = Object.keys(holdings);
  if (syms.length === 0 || dates.length === 0) return null;
  // 마지막 날 비중 기준 상위 5종목.
  const top = syms
    .map((s) => ({ s, last: holdings[s][holdings[s].length - 1]?.weight ?? 0 }))
    .sort((a, b) => b.last - a.last)
    .slice(0, 5)
    .map((x) => x.s);
  const dateIdx = new Map(dates.map((d, i) => [d, i]));
  const w = 300, h = 90, pad = 4;
  const allW = top.flatMap((s) => holdings[s].map((p) => p.weight));
  const max = Math.max(1, ...allW);
  const n = dates.length;
  const x = (i: number) => (n === 1 ? w / 2 : pad + (i / (n - 1)) * (w - 2 * pad));
  const y = (v: number) => h - pad - (v / max) * (h - 2 * pad);
  return (
    <div>
      <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} preserveAspectRatio="none" role="img" aria-label="종목별 비중 추이">
        {top.map((s, idx) => {
          const pts = holdings[s]
            .map((p) => { const i = dateIdx.get(p.date); return i == null ? null : `${x(i).toFixed(1)},${y(p.weight).toFixed(1)}`; })
            .filter(Boolean)
            .join(" ");
          return <polyline key={s} points={pts} fill="none" stroke={LINE_COLORS[idx % LINE_COLORS.length]} strokeWidth={1.8} strokeLinejoin="round" />;
        })}
      </svg>
      <div className="flex flex-wrap gap-x-3 gap-y-1 mt-1">
        {top.map((s, idx) => (
          <span key={s} className="inline-flex items-center gap-1 text-[11px] text-neutral-600">
            <span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: LINE_COLORS[idx % LINE_COLORS.length] }} />
            {s} {holdings[s][holdings[s].length - 1]?.weight}%
          </span>
        ))}
      </div>
    </div>
  );
}

// ---- 순수 SVG 노출 다중 라인 (net/gross/테마/hedge, % 축 0~max) ----
function ExposureLines({ dates, series }: { dates: string[]; series: Series["exposure_series"] }) {
  // long/short 가 시리즈에 있을 때만 그 라인을 그린다(구버전 series 와의 호환).
  const keys = EXPOSURE_KEYS.filter((k) => Array.isArray(series[k]) && series[k]!.length > 0);
  if (dates.length === 0) return null;
  const all = keys.flatMap((k) => series[k] ?? []);
  const max = Math.max(1, ...all.map((v) => Math.abs(v)));
  const w = 300, h = 90, pad = 4;
  const n = dates.length;
  const x = (i: number) => (n === 1 ? w / 2 : pad + (i / (n - 1)) * (w - 2 * pad));
  const y = (v: number) => h - pad - (v / max) * (h - 2 * pad);
  return (
    <div>
      <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} preserveAspectRatio="none" role="img" aria-label="노출(net/gross/테마/hedge) 추이">
        {keys.map((k) => {
          const vals = series[k] ?? [];
          const pts = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
          return <polyline key={k} points={pts} fill="none" stroke={EXPOSURE_COLORS[k]} strokeWidth={1.8} strokeLinejoin="round" />;
        })}
      </svg>
      <div className="flex flex-wrap gap-x-3 gap-y-1 mt-1">
        {keys.map((k) => (
          <span key={k} className="inline-flex items-center gap-1 text-[11px] text-neutral-600">
            <span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: EXPOSURE_COLORS[k] }} />
            {EXPOSURE_LABELS[k]} {(() => { const a = series[k] ?? []; return a.length ? a[a.length - 1] : 0; })()}%
          </span>
        ))}
      </div>
    </div>
  );
}

// ---- 순수 SVG drift 라인 (null 은 끊어 그림 — 점검 전은 가짜 0 금지) ----
function DriftLine({ dates, drift }: { dates: string[]; drift: (number | null)[] }) {
  if (dates.length === 0) return null;
  const present = drift.filter((v): v is number => v != null);
  if (present.length === 0) return null;
  const max = Math.max(1, ...present.map((v) => Math.abs(v)));
  const w = 300, h = 56, pad = 4;
  const n = dates.length;
  const x = (i: number) => (n === 1 ? w / 2 : pad + (i / (n - 1)) * (w - 2 * pad));
  const y = (v: number) => h - pad - (v / max) * (h - 2 * pad);
  // null 구간은 polyline 을 끊는다(연속 구간별 분리).
  const segs: string[][] = [];
  let cur: string[] = [];
  drift.forEach((v, i) => {
    if (v == null) { if (cur.length) { segs.push(cur); cur = []; } return; }
    cur.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  });
  if (cur.length) segs.push(cur);
  const lastVal = present[present.length - 1];
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} preserveAspectRatio="none" role="img" aria-label="drift 추이">
      {segs.map((pts, i) => (
        pts.length === 1
          ? <circle key={i} cx={Number(pts[0].split(",")[0])} cy={Number(pts[0].split(",")[1])} r={2.5} fill="#10b981" />
          : <polyline key={i} points={pts.join(" ")} fill="none" stroke="#10b981" strokeWidth={2} strokeLinejoin="round" />
      ))}
      <title>최근 drift {lastVal}%</title>
    </svg>
  );
}

export function PortfolioTrendCard({ accountId }: { accountId: number }) {
  const [s, setS] = useState<Series | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const r = await fetch(`/api/accounts/${accountId}/history-series?days=30`, { cache: "no-store" });
      const j = await r.json();
      setS(j?.series && j.series.ok ? j.series : null);
    } catch { setS(null); }
    setLoaded(true);
    setBusy(false);
  }, [accountId]);
  useEffect(() => { load(); }, [load]);

  const hasData = s && s.dates.length > 0;
  const lastTotal = hasData ? s!.total_value[s!.total_value.length - 1] : null;
  const firstTotal = hasData ? s!.total_value[0] : null;
  const deltaPct = hasData && firstTotal ? ((lastTotal! - firstTotal!) / firstTotal!) * 100 : null;

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2"><TrendingUp className="w-5 h-5 text-primary" /> 일별 포트폴리오 추이</CardTitle>
        <button onClick={load} disabled={busy} className="text-xs text-neutral-500 inline-flex items-center gap-1 hover:text-neutral-700 disabled:opacity-50">
          <RefreshCw className={`w-3.5 h-3.5 ${busy ? "animate-spin" : ""}`} /> 새로고침
        </button>
      </CardHeader>
      <CardBody className="space-y-5">
        {!loaded ? (
          <p className="text-sm text-neutral-400">불러오는 중…</p>
        ) : !hasData ? (
          <p className="text-sm text-neutral-500">
            아직 기록된 일별 스냅샷이 없습니다. 계좌 동기화 후 매일 1행씩 추이가 쌓입니다.
          </p>
        ) : (
          <>
            {/* 1) 총자산 추이 */}
            <section>
              <div className="flex items-baseline justify-between mb-1">
                <span className="text-sm font-medium text-neutral-700">총자산 추이</span>
                <span className="text-sm">
                  <b className="text-neutral-900">{fmtKrw(lastTotal!)}</b>
                  {deltaPct != null && (
                    <span className={`ml-1 text-xs ${deltaPct >= 0 ? "text-success" : "text-warning"}`}>
                      {deltaPct >= 0 ? "▲" : "▼"} {Math.abs(deltaPct).toFixed(1)}%
                    </span>
                  )}
                </span>
              </div>
              <Sparkline values={s!.total_value} />
              <div className="flex justify-between text-[10px] text-neutral-400 mt-0.5">
                <span>{s!.dates[0]}</span><span>{s!.dates[s!.dates.length - 1]}</span>
              </div>
            </section>

            {/* 2) 자산군 비중 변화 */}
            <section>
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium text-neutral-700">자산군 비중 변화</span>
                <span className="flex gap-2">
                  {["순현금", "채권", "위험"].map((k) => (
                    <span key={k} className="inline-flex items-center gap-1 text-[11px] text-neutral-500">
                      <span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: BUCKET_COLORS[k] }} />{k}
                    </span>
                  ))}
                </span>
              </div>
              <BucketBars dates={s!.dates} bucketSeries={s!.bucket_series} />
            </section>

            {/* 3) 노출 추이 — net/gross/테마/hedge (selected allocation 확정 안 기준) */}
            <section>
              <div className="flex items-baseline justify-between mb-1">
                <span className="text-sm font-medium text-neutral-700">노출 추이 (확정 안 기준)</span>
                {s!.exposure && (
                  <span className="text-xs text-neutral-500">
                    net <b className="text-neutral-800">{s!.exposure.net_pct}%</b> · gross {s!.exposure.gross_pct}%
                  </span>
                )}
              </div>
              {s!.exposure_tracked ? (
                <>
                  <ExposureLines dates={s!.dates} series={s!.exposure_series} />
                  {s!.exposure && Object.keys(s!.exposure.theme_exposure).length > 0 && (
                    <div className="flex flex-wrap gap-x-3 gap-y-0.5 mt-1 text-[11px] text-neutral-500">
                      {Object.entries(s!.exposure.theme_exposure).map(([t, v]) => (
                        <span key={t}>{t} {v}%</span>
                      ))}
                    </div>
                  )}
                  <p className="text-[10px] text-neutral-400 mt-1">
                    net = 롱 − 숏 · gross = 롱 + 숏 · 롱/숏 라인이 net·gross 의 구성 요소 · 방어(현금+채권)는 노출 제외 · 목표(확정 안) 기준
                  </p>
                </>
              ) : (
                <p className="text-xs text-neutral-500">
                  확정된 목표 포트폴리오(3안 중 선택)가 없습니다 — 확정 후 노출(net/gross/테마/헤지)이 표시됩니다.
                </p>
              )}
            </section>

            {/* 4) drift 추이 — daily_portfolio_reviews (점검 전은 끊어 그림, 가짜 0 없음) */}
            <section>
              <span className="text-sm font-medium text-neutral-700 block mb-1">목표 대비 이탈(drift) 추이</span>
              {s!.drift_series.some((v) => v != null) ? (
                <DriftLine dates={s!.dates} drift={s!.drift_series} />
              ) : (
                <p className="text-xs text-neutral-500">
                  아직 점검(daily review) 기록이 없습니다 — 점검이 쌓이면 목표 대비 이탈(drift) 추이가 표시됩니다.
                </p>
              )}
            </section>

            {/* 5) 종목별 추이 (정직: 동기화 전이면 안내) */}
            <section>
              <span className="text-sm font-medium text-neutral-700 block mb-1">종목별 추이 (상위 5)</span>
              {s!.holdings_tracked ? (
                <HoldingLines dates={s!.dates} holdings={s!.holdings_by_symbol} />
              ) : (
                <p className="text-xs text-neutral-500">
                  보유종목 동기화 후 종목별 추이가 쌓입니다 — 현재는 현금/총자산만 기록 중입니다.
                </p>
              )}
            </section>

            {s!.note && <p className="text-[11px] text-neutral-400">※ {s!.note}</p>}
            {!s!.holdings_tracked && (
              <Badge className="bg-neutral-100 text-neutral-500">보유종목 동기화 전 — 현금/총자산만</Badge>
            )}
          </>
        )}
        <p className="text-[11px] text-neutral-400">실시간이 아니라 매일 1행씩 쌓이는 기록입니다. 수치는 운영 DB(KIS 동기화)에서만 가져오며 가짜 데이터는 없습니다.</p>
      </CardBody>
    </Card>
  );
}
