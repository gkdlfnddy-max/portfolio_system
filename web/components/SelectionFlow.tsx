"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Input } from "@/components/ui/Input";
import {
  ShieldAlert, Lock, Info, CheckCircle2, Circle, AlertTriangle, Database, Layers,
  Square, CheckSquare, Sliders, Scale, ChevronDown, ChevronRight, Star,
  TrendingUp, Percent, ArrowRight, TrendingDown, Globe, Droplet, UserCheck,
} from "lucide-react";

// ──────────────────────────────────────────────────────────────────────────
// 3안 확정 후 "세부 종목/ETF 선정" 화면 (Step 1–8).
//   · 실 DB 기반 — 백엔드 CLI 가 주는 데이터만 표시. mock/가짜 차트 0.
//   · 데이터 없으면 "데이터 없음/미연동" 표기. CLI 미구현이면 "준비 중".
//   · 자동 주문/적용 0. 승인 버튼은 draft 만(승인 전 policy/order 미반영).
//   · 추천/정답 단정 금지 — 비교·토론 중심 문구. confidence 낮으면 단정 안 함.
// ──────────────────────────────────────────────────────────────────────────

type Section = { ready: boolean; data: any | null; note?: string };
type Payload = {
  ok: boolean;
  account_id: number;
  defensive: Section;
  bond_recommendation?: Section; // Step 1 — 금리 기반 국채 추천(제안일 뿐)
  bond_options?: Section; // Step 1 — 국채 비중 후보 A/B/C/D(제안일 뿐)
  govbond_etf?: Section; // Step 1 — 국채 ETF 후보 비교(역할/장점/리스크/적합성/데이터품질)
  buckets: Record<string, Section>;
  bucket_order: string[];
  error?: string;
};

// 후보 선택 1건 — bucket 안에서 고른 종목/ETF. POST 로 weight_allocator 에 전달.
type Pick = { bucket: string; ticker: string; name: string | null; asset_class: string | null };
// 개별주 bucket 옵션: 없음 / 5% / 10% (위험자산 안에서 배분, 확정안 100 불변).
type EquityOption = "none" | "5" | "10";
// weight_allocator(POST) 응답 데이터 — 없으면 graceful(준비 중).
//   data = allocate() 결과(holdings/bucket_summary/over_limit_warnings/total_pct), options = 개별주 A/B/C.
type AllocSection = { ready: boolean; data: any | null; note?: string; options?: { ready: boolean; data: any | null; note?: string } };

const BUCKET_LABEL: Record<string, string> = {
  global_core: "글로벌 코어",
  robotics: "로봇",
  semiconductor: "반도체",
  semiconductor_inverse: "반도체 인버스(헤지)",
  treasury: "국채",
};

// 데이터 가용성 축 — security_selection 의 comparison[].data_availability 키와 정확히 일치.
//   값은 boolean 이 아니라 문자열("미연동" / "직접대상 아님(ETF)" / 실측값)이다.
//   "연결됨" 판정: 값이 있고 미연동/직접대상/unknown 류가 아닐 때.
const AVAIL_AXES: { key: string; label: string }[] = [
  { key: "financials", label: "재무" },
  { key: "news", label: "뉴스" },
  { key: "filing", label: "공시" },
  { key: "etf_constituents", label: "ETF 구성" },
  { key: "macro", label: "거시" },
  { key: "flow", label: "수급" },
  { key: "price_daily", label: "가격·일봉" },
];

// data_availability 값(문자열)을 "연결됨" boolean 으로 정직하게 변환.
function isAvailConnected(v: any): boolean {
  if (v == null) return false;
  if (typeof v === "boolean") return v;
  const s = String(v);
  return !/(미연동|직접대상\s*아님|unknown|없음|미설정|미적재)/i.test(s);
}

const pct = (n: any) => (n == null || Number.isNaN(Number(n)) ? null : Math.round(Number(n) * 10) / 10);
const won = (n: any) => (n == null || Number.isNaN(Number(n)) ? "—" : Math.round(Number(n)).toLocaleString("ko-KR") + "원");

// ── 데이터 가용성 배지 ──
function AvailBadge({ connected, label }: { connected: boolean; label: string }) {
  return (
    <Badge className={connected ? "bg-success/10 text-success" : "bg-neutral-100 text-neutral-400"}>
      <span className={`w-1.5 h-1.5 rounded-full mr-1 ${connected ? "bg-success" : "bg-neutral-300"}`} />
      {label} {connected ? "연결됨" : "미연동"}
    </Badge>
  );
}

// 후보별 data_availability(문자열 맵)를 배지로 표시. 없으면 전부 미연동으로 정직하게 표시.
function AvailabilityRow({ avail }: { avail: Record<string, any> | null | undefined }) {
  const map = avail ?? {};
  const connectedCount = AVAIL_AXES.filter((a) => isAvailConnected(map[a.key])).length;
  const weak = connectedCount < 3;
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-xs text-neutral-500">
        <Database className="w-3.5 h-3.5" /> 데이터 가용성
      </div>
      <div className="flex flex-wrap gap-1.5">
        {AVAIL_AXES.map((a) => (
          <AvailBadge key={a.key} connected={isAvailConnected(map[a.key])} label={a.label} />
        ))}
      </div>
      {weak && (
        <p className="text-[11px] text-warning flex items-center gap-1">
          <AlertTriangle className="w-3.5 h-3.5" /> 데이터 부족 — <b>강한 결론 불가 · 후보 비교 단계</b>. 부족한 축은 비교에서 제외합니다.
        </p>
      )}
    </div>
  );
}

// ── confidence 표기 ──
function ConfidenceTag({ value }: { value: any }) {
  const c = value == null ? null : Number(value);
  if (c == null || Number.isNaN(c)) return <span className="text-neutral-400">confidence 데이터 없음</span>;
  const low = c < 0.5;
  return (
    <span className={low ? "text-warning" : "text-neutral-600"}>
      confidence {Math.round(c * 100)}%{low && " — 낮음, 단정하지 않음"}
    </span>
  );
}

// ── "준비 중 / 데이터 없음" 정직한 빈 상태 ──
function NotReady({ note }: { note?: string }) {
  return (
    <div className="rounded-lg border border-dashed border-neutral-200 bg-neutral-50 p-4 text-center text-sm text-neutral-500">
      {note?.includes("준비") ? "백엔드 준비 중" : "데이터 없음 · 미연동"}
      <div className="text-xs text-neutral-400 mt-1">{note ?? "연동 후 자동 표시됩니다 (mock/가짜 데이터 없음)."}</div>
    </div>
  );
}

// ── 스텝 진행 표시 ──
const STEPS = [
  "방어자산 구성",
  "글로벌 코어",
  "로봇",
  "반도체",
  "헤지·국채",
  "비중 조절",
  "승인",
  "분할 진입",
  "주문 전 확인",
];
function Stepper({ current, onJump }: { current: number; onJump: (i: number) => void }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {STEPS.map((s, i) => {
        const active = i === current;
        const done = i < current;
        return (
          <button
            key={s}
            onClick={() => onJump(i)}
            className={`flex items-center gap-1 px-2.5 py-1 rounded-full text-xs border transition ${
              active
                ? "border-primary text-primary bg-primary-50"
                : done
                  ? "border-neutral-200 text-neutral-500"
                  : "border-neutral-200 text-neutral-400"
            }`}
          >
            {done ? <CheckCircle2 className="w-3.5 h-3.5" /> : <Circle className="w-3.5 h-3.5" />}
            {i + 1}. {s}
          </button>
        );
      })}
    </div>
  );
}

// ── 필수 고지 배너 (전 화면 공통) ──
function DisclaimerBanner() {
  return (
    <div className="rounded-lg bg-primary-50 border border-primary-100 p-3 text-xs text-primary-700 space-y-1">
      <div className="flex items-center gap-1.5 font-medium"><Info className="w-3.5 h-3.5" /> 이 화면은 투자 판단 보조용입니다.</div>
      <ul className="list-disc pl-5 space-y-0.5 text-primary-700/90">
        <li>이 화면은 <b>자동으로 주문을 생성하지 않습니다.</b></li>
        <li>승인 전에는 <b>policy(목표비중)·주문에 반영되지 않습니다.</b></li>
        <li>데이터가 부족한 축은 비교에서 제외하며, <b>confidence 가 낮으면 단정하지 않습니다.</b></li>
        <li>추천·정답이 아니라 <b>현재 정책·관점 기준의 비교</b>입니다.</li>
        <li>미연동 데이터가 있으면 <b>"모든 데이터를 고려했다"고 말하지 않습니다.</b></li>
      </ul>
    </div>
  );
}

// ── 전역 데이터 연결 상태 배너 (가격/일봉·재무·공시·뉴스·ETF구성·거시·수급) ──
//   모든 bucket 후보의 data_availability 합집합으로 축별 연결 여부를 집계.
//   미연동 축이 하나라도 있으면 "모든 데이터를 고려했다고 말하지 않는다" 정직 문구.
function DataStatusBanner({ payload }: { payload: Payload | null }) {
  const buckets = payload?.buckets ?? {};
  const agg: Record<string, boolean> = {};
  for (const a of AVAIL_AXES) {
    agg[a.key] = Object.values(buckets).some((sec: any) => {
      const cands: any[] = sec?.data?.comparison ?? [];
      return cands.some((c) => isAvailConnected((c.data_availability ?? {})[a.key]));
    });
  }
  const missing = AVAIL_AXES.filter((a) => !agg[a.key]);
  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-3 space-y-2">
      <div className="flex items-center gap-1.5 text-xs font-medium text-neutral-600">
        <Database className="w-3.5 h-3.5" /> 데이터 연결 상태
      </div>
      <div className="flex flex-wrap gap-1.5">
        {AVAIL_AXES.map((a) => (
          <AvailBadge key={a.key} connected={agg[a.key]} label={a.label} />
        ))}
      </div>
      {missing.length > 0 && (
        <p className="text-[11px] text-warning flex items-start gap-1">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          <span>
            미연동 축({missing.map((m) => m.label).join(", ")})이 있어 <b>"모든 데이터를 고려했다"고 말하지 않습니다.</b>
            연결된 축만으로 비교하며, 부족한 부분은 단정하지 않습니다.
          </span>
        </p>
      )}
    </div>
  );
}

// ── 국채 ETF 후보표 (bond_bucket.govbond_etf_candidates 전용 — bucket 비교표와 형태가 다름) ──
const GOVBOND_COLS: { key: string; label: string }[] = [
  { key: "ticker", label: "티커" },
  { key: "name", label: "이름" },
  { key: "region", label: "지역" },
  { key: "duration_band", label: "만기대" },
  { key: "bond_type", label: "유형" },
  { key: "status", label: "상태" },
];
function GovbondCandidateTable({ candidates }: { candidates: any[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-neutral-200">
      <table className="w-full text-xs">
        <thead className="bg-neutral-50 text-neutral-500">
          <tr>
            {GOVBOND_COLS.map((c) => (
              <th key={c.key} className="text-left font-medium px-2.5 py-2 whitespace-nowrap">{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {candidates.map((cand, i) => (
            <tr key={cand.ticker ?? i} className="border-t border-neutral-100 align-top">
              {GOVBOND_COLS.map((c) => (
                <td key={c.key} className="px-2.5 py-2">
                  {c.key === "ticker"
                    ? <span className="font-medium text-neutral-800 tabular-nums">{cand.ticker ?? "—"}</span>
                    : c.key === "status"
                      ? <span className="inline-flex items-center gap-1 text-warning">
                          <AlertTriangle className="w-3 h-3" />{cand.status ?? "—"}
                        </span>
                      : <span className="text-neutral-700">{cand[c.key] == null || cand[c.key] === "" ? "—" : String(cand[c.key])}</span>}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── 데이터 품질 배지 (가격 실연동 / 보수율·듀레이션 미연동 등 정직 표기) ──
//   값이 객체 {available, value} 또는 boolean/문자열 어느 쪽이든 정직하게 흡수.
function dqConnected(v: any): boolean {
  if (v == null) return false;
  if (typeof v === "boolean") return v;
  if (typeof v === "object") {
    if (typeof v.available === "boolean") return v.available;
    if (v.value != null) return true;
    if (v.connected != null) return !!v.connected;
    return false;
  }
  return !/(미연동|없음|미설정|미적재|unknown|n\/a)/i.test(String(v));
}
function dqValue(v: any): string | null {
  if (v == null) return null;
  if (typeof v === "object") {
    const raw = v.value ?? v.display ?? null;
    return raw == null ? null : String(raw);
  }
  if (typeof v === "boolean") return null;
  const s = String(v).trim();
  return s === "" ? null : s;
}
function DataQualityBadge({ label, v }: { label: string; v: any }) {
  const ok = dqConnected(v);
  const val = ok ? dqValue(v) : null;
  return (
    <Badge className={ok ? "bg-success/10 text-success" : "bg-neutral-100 text-neutral-400"}>
      <span className={`w-1.5 h-1.5 rounded-full mr-1 ${ok ? "bg-success" : "bg-neutral-300"}`} />
      {label} {ok ? (val ? `실연동 · ${val}` : "실연동") : "미연동"}
    </Badge>
  );
}

// ── 추천 강도 → 톤 (단정 금지: 데이터 부족이면 '단정 안 함') ──
function strengthBadge(raw: any) {
  const s = String(raw ?? "").trim().toLowerCase();
  if (/strong|강|high/.test(s)) return { label: "비교적 강함(사람 승인)", cls: "bg-success/10 text-success" };
  if (/moderate|medium|보통|중/.test(s)) return { label: "보통(후보)", cls: "bg-primary-50 text-primary-700" };
  if (/weak|약|low|참고/.test(s)) return { label: "참고(약)", cls: "bg-neutral-100 text-neutral-500" };
  if (/none|insufficient|데이터|미정|unknown/.test(s) || s === "")
    return { label: "데이터 부족 — 단정 안 함", cls: "bg-neutral-100 text-neutral-400" };
  return { label: String(raw), cls: "bg-neutral-100 text-neutral-500" };
}

// ── 국채 ETF 후보 비교(govbond_etf --compare) — 역할/장점/리스크/거시·계좌 적합성/데이터품질/대안/제외 ──
//   가격·거래량은 실데이터(있으면), 보수율·듀레이션은 미연동이면 "미연동" 정직. mock 0.
//   단기/장기 · 한국/미국 그룹으로 묶어 보기 쉽게. 추천이 아니라 비교(정답 아님).
type GovEtf = {
  ticker: string | null; name: string | null; region: string | null; durationBand: string | null;
  role: string | null; pros: string[]; risks: string[];
  macroFit: string | null; accountFit: string | null; strength: any;
  alternatives: string[]; excluded: string | null;
  price: any; volume: any; expenseRatio: any; duration: any;
  status: string | null;
};
function arr(v: any): string[] {
  if (Array.isArray(v)) return v.map((x) => (typeof x === "string" ? x : x?.name ?? x?.ticker ?? JSON.stringify(x))).filter(Boolean);
  if (v == null) return [];
  const s = String(v).trim();
  return s ? [s] : [];
}
// fit 객체({label,reason}) 또는 문자열을 "라벨 — 근거" 한 줄로 정직 변환.
function fitText(v: any): string | null {
  if (v == null) return null;
  if (typeof v === "object") {
    const label = strOrNull(v.label);
    const reason = strOrNull(v.reason ?? v.detail);
    if (label && reason) return `${label} — ${reason}`;
    return label ?? reason ?? null;
  }
  return strOrNull(v);
}
function normalizeGovEtf(raw: any): GovEtf {
  return {
    ticker: strOrNull(raw?.ticker),
    name: strOrNull(raw?.name),
    region: strOrNull(raw?.region),
    durationBand: strOrNull(raw?.duration_bucket ?? raw?.duration_band ?? raw?.duration),
    role: strOrNull(raw?.role ?? raw?.bucket_role),
    pros: arr(raw?.pros ?? raw?.advantages ?? raw?.강점),
    risks: arr(raw?.risks ?? raw?.리스크),
    macroFit: fitText(raw?.macro_fit ?? raw?.거시적합성),
    accountFit: fitText(raw?.purpose_fit ?? raw?.account_fit ?? raw?.계좌적합성 ?? raw?.fit),
    strength: raw?.recommendation_strength ?? raw?.strength ?? null,
    alternatives: arr(raw?.alternatives ?? raw?.대안),
    excluded: strOrNull(raw?.excluded ?? raw?.excluded_reason ?? raw?.제외),
    price: raw?.data_quality?.price ?? raw?.price ?? null,
    volume: raw?.data_quality?.volume ?? raw?.volume ?? null,
    expenseRatio: raw?.data_quality?.expense_ratio ?? raw?.expense_ratio ?? null,
    duration: raw?.data_quality?.duration_years ?? raw?.data_quality?.duration ?? raw?.duration_value ?? null,
    status: strOrNull(raw?.status),
  };
}
const BAND_KO: Record<string, string> = { short: "단기", intermediate: "중기", long: "장기" };

function GovbondEtfCard({ etf, picked, onPick }: { etf: GovEtf; picked: boolean; onPick: () => void }) {
  const st = strengthBadge(etf.strength);
  return (
    <div className={`rounded-lg border p-3 space-y-2.5 transition ${picked ? "border-primary bg-primary-50/40" : "border-neutral-200"}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="text-sm">
          <span className="font-medium text-neutral-800">{etf.name ?? etf.ticker ?? "—"}</span>
          {etf.ticker && <span className="text-neutral-400 tabular-nums"> · {etf.ticker}</span>}
          <div className="flex flex-wrap gap-1 mt-1">
            {etf.region && <Badge className="bg-neutral-100 text-neutral-500">{etf.region}</Badge>}
            {etf.durationBand && <Badge className="bg-sky-50 text-sky-700">{BAND_KO[etf.durationBand] ?? etf.durationBand}</Badge>}
            {etf.role && <Badge className="bg-neutral-100 text-neutral-500">역할: {etf.role}</Badge>}
          </div>
        </div>
        <Badge className={st.cls}>{st.label}</Badge>
      </div>

      {/* 데이터 품질 배지 — 가격/거래량 실연동, 보수율/듀레이션 미연동은 정직하게 미연동 */}
      <div className="space-y-1.5">
        <div className="flex items-center gap-1 text-[11px] text-neutral-500"><Database className="w-3.5 h-3.5" /> 데이터 품질</div>
        <div className="flex flex-wrap gap-1.5">
          <DataQualityBadge label="가격" v={etf.price} />
          <DataQualityBadge label="거래량" v={etf.volume} />
          <DataQualityBadge label="보수율" v={etf.expenseRatio} />
          <DataQualityBadge label="듀레이션" v={etf.duration} />
        </div>
      </div>

      {etf.pros.length > 0 && (
        <div className="text-xs text-neutral-600">
          <span className="text-neutral-400">장점: </span>
          <ul className="list-disc pl-4 space-y-0.5 mt-0.5">{etf.pros.slice(0, 3).map((p, i) => <li key={i}>{p}</li>)}</ul>
        </div>
      )}
      {etf.risks.length > 0 && (
        <div className="text-xs text-warning">
          <span className="text-warning/80">리스크: </span>
          <ul className="list-disc pl-4 space-y-0.5 mt-0.5">{etf.risks.slice(0, 3).map((r, i) => <li key={i}>{r}</li>)}</ul>
        </div>
      )}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1 text-[11px]">
        <div className="flex items-start gap-1.5"><TrendingUp className="w-3 h-3 text-sky-500 shrink-0 mt-0.5" />
          <span className="text-neutral-500">현 거시 적합성: </span><span className="text-neutral-700">{etf.macroFit ?? <span className="text-neutral-300">데이터 없음</span>}</span></div>
        <div className="flex items-start gap-1.5"><UserCheck className="w-3 h-3 text-primary shrink-0 mt-0.5" />
          <span className="text-neutral-500">계좌 적합성: </span><span className="text-neutral-700">{etf.accountFit ?? <span className="text-neutral-300">데이터 없음</span>}</span></div>
      </div>
      {etf.alternatives.length > 0 && (
        <div className="text-[11px] text-neutral-500"><span className="text-neutral-400">대안: </span>{etf.alternatives.join(", ")}</div>
      )}
      {etf.excluded && (
        <div className="text-[11px] text-warning flex items-start gap-1"><AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" /><span>제외: {etf.excluded}</span></div>
      )}
      {etf.status && (
        <div className="text-[11px] text-neutral-400 flex items-center gap-1"><Info className="w-3 h-3" />{etf.status}</div>
      )}
      <div className="flex justify-end">
        <Button size="sm" variant={picked ? "outline" : "outline"} onClick={onPick}>
          {picked ? "표시됨 (draft)" : "이 ETF 표시 (draft)"}
        </Button>
      </div>
    </div>
  );
}

// 후보들을 지역(한국/미국) × 만기대(단기/중기/장기) 그룹으로 묶어 표시.
function GovbondEtfCompare({ sec }: { sec?: Section }) {
  const [picked, setPicked] = useState<string | null>(null);
  const ready = sec?.ready === true;
  const rawList: any[] = ready
    ? (Array.isArray(sec?.data?.candidates) ? sec!.data.candidates
      : Array.isArray(sec?.data) ? (sec!.data as any[]) : [])
    : [];
  const etfs = useMemo(() => rawList.map((r) => normalizeGovEtf(r)), [rawList]);
  // 상위 메타(있으면): 금리환경·장기채 경고·제외 후보.
  const regimeRaw = ready ? (sec!.data?.rate_regime ?? null) : null;
  const regimeLabelStr = regimeRaw
    ? (typeof regimeRaw === "object" ? strOrNull(regimeRaw.detail ?? regimeRaw.regime ?? regimeRaw.label) : strOrNull(regimeRaw))
    : null;
  const longWarn = ready ? strOrNull(sec!.data?.long_bond_volatility_warning) : null;
  const excludedList: any[] = ready && Array.isArray(sec!.data?.excluded) ? sec!.data.excluded : [];

  if (!ready) {
    return (
      <div className="rounded-lg border border-dashed border-neutral-200 bg-neutral-50 p-3 text-xs text-neutral-500 flex items-start gap-1.5">
        <Info className="w-3.5 h-3.5 shrink-0 mt-0.5" />
        <span>
          {sec?.note?.includes("준비") || sec?.note?.includes("미연동")
            ? "국채 ETF 비교 엔진 준비 중(미연동)"
            : "국채 ETF 비교 데이터 없음 · 미연동"}
          {" "}— 연동 후 후보별 역할·장점·리스크·데이터 품질이 자동 표시됩니다 (mock/가짜 데이터 없음).
        </span>
      </div>
    );
  }
  if (etfs.length === 0) return <NotReady note={sec?.note ?? "국채 ETF 후보 데이터 없음 · 미연동"} />;

  // 지역 그룹 → 만기대 그룹.
  const byRegion: Record<string, GovEtf[]> = {};
  for (const e of etfs) (byRegion[e.region ?? "기타"] ||= []).push(e);
  const BAND_ORDER = ["short", "intermediate", "long"];
  const regionKeys = Object.keys(byRegion).sort((a, b) => (a === "한국" ? -1 : b === "한국" ? 1 : a.localeCompare(b)));

  return (
    <div className="space-y-4">
      <div className="rounded-lg bg-warning/5 border border-warning/30 p-2.5 text-[11px] text-neutral-600 space-y-1">
        <p className="text-warning flex items-start gap-1">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          <span><b>국채 ETF 는 방어자산을 구현하는 수단일 뿐 — 수익 극대화 수단이 아닙니다.</b> 정답이 아니라 비교용입니다.</span>
        </p>
        <p>가격·거래량은 실데이터(연동 시)이며, <b>보수율·듀레이션이 미연동이면 "미연동"으로 정직 표기</b>합니다(가짜 숫자 없음). C안을 바로 확정하지 말고 후보를 비교한 뒤 선택하세요.</p>
      </div>
      {(regimeLabelStr || longWarn) && (
        <div className="rounded-lg border border-sky-100 bg-sky-50/60 p-2.5 text-[11px] text-neutral-600 space-y-1">
          {regimeLabelStr && (
            <p className="flex items-center gap-1"><TrendingUp className="w-3.5 h-3.5 text-sky-600" /> <span className="text-neutral-500">현 금리환경:</span> {regimeLabelStr}</p>
          )}
          {longWarn && (
            <p className="text-warning flex items-start gap-1"><AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" /> {longWarn}</p>
          )}
        </div>
      )}
      {regionKeys.map((region) => {
        const group = byRegion[region];
        const byBand: Record<string, GovEtf[]> = {};
        for (const e of group) (byBand[e.durationBand ?? "기타"] ||= []).push(e);
        const bandKeys = Object.keys(byBand).sort((a, b) => BAND_ORDER.indexOf(a) - BAND_ORDER.indexOf(b));
        return (
          <div key={region} className="space-y-2.5">
            <div className="text-sm font-medium text-neutral-700 flex items-center gap-1.5">
              <Globe className="w-4 h-4 text-sky-600" /> {region} 국채
            </div>
            {bandKeys.map((band) => (
              <div key={band} className="space-y-2 pl-1">
                <div className="text-xs text-neutral-500">{BAND_KO[band] ?? band}</div>
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-2">
                  {byBand[band].map((etf, i) => {
                    const key = `${etf.ticker ?? etf.name ?? region}:${i}`;
                    return (
                      <GovbondEtfCard
                        key={key}
                        etf={etf}
                        picked={picked === key}
                        onPick={() => setPicked((p) => (p === key ? null : key))}
                      />
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        );
      })}
      {excludedList.length > 0 && (
        <div className="rounded-lg border border-neutral-200 bg-neutral-50 p-2.5 text-[11px] text-neutral-500 space-y-1">
          <div className="font-medium text-neutral-600 flex items-center gap-1"><AlertTriangle className="w-3.5 h-3.5 text-neutral-400" /> 제외된 후보(정직 표기)</div>
          <ul className="list-disc pl-4 space-y-0.5">
            {excludedList.slice(0, 6).map((x: any, i: number) => (
              <li key={i}>
                {strOrNull(x?.ticker) ? `${x.ticker} · ` : ""}{strOrNull(x?.name) ?? ""}
                {(strOrNull(x?.reason) || strOrNull(x?.excluded) || strOrNull(x)) ? ` — ${strOrNull(x?.reason) ?? strOrNull(x?.excluded) ?? strOrNull(x)}` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="text-[11px] text-neutral-400">
        이 비교는 <b>초안(draft)</b>일 뿐 — 자동으로 적용/주문되지 않습니다. 실제 반영은 채권 비중 입력 → 3안 재확정 → 승인 후에만 이뤄집니다.
      </p>
    </div>
  );
}

// ── 금리 동향 → 한글 라벨 (rate_regime 정규화) ──
//   백엔드 키(hiking/high/cut_expected/easing/uncertain/unknown 등) + 한글 입력 모두 흡수.
const RATE_REGIME: Record<string, { label: string; tone: string }> = {
  hiking: { label: "금리 인상기", tone: "text-error" },
  high: { label: "금리 높음(고점)", tone: "text-warning" },
  cut_expected: { label: "금리 인하 기대", tone: "text-success" },
  easing: { label: "금리 하락(인하)", tone: "text-success" },
  uncertain: { label: "불확실", tone: "text-neutral-500" },
  unknown: { label: "미상", tone: "text-neutral-400" },
};
function regimeLabel(v: any): { label: string; tone: string } {
  if (v == null || String(v).trim() === "") return RATE_REGIME.unknown;
  const k = String(v).trim().toLowerCase();
  if (RATE_REGIME[k]) return RATE_REGIME[k];
  // 한글 직접 입력도 그대로 표시(정직 — 매핑 못해도 가짜로 바꾸지 않음).
  return { label: String(v).trim(), tone: "text-neutral-600" };
}

// data_source(거시 연동 / 사용자 견해 / 없음) 배지 — 추천 신뢰 출처를 정직하게 표기.
function DataSourceBadge({ source }: { source: any }) {
  const s = String(source ?? "").trim().toLowerCase();
  if (/macro|거시|연동/.test(s) && !/none|없음|미연동/.test(s)) {
    return <Badge className="bg-success/10 text-success"><span className="w-1.5 h-1.5 rounded-full mr-1 bg-success" />거시 데이터 연동</Badge>;
  }
  if (/user|view|견해|수동|manual/.test(s)) {
    return <Badge className="bg-primary-50 text-primary-700"><span className="w-1.5 h-1.5 rounded-full mr-1 bg-primary" />사용자 금리뷰 기반</Badge>;
  }
  return <Badge className="bg-neutral-100 text-neutral-400"><span className="w-1.5 h-1.5 rounded-full mr-1 bg-neutral-300" />금리 데이터 없음</Badge>;
}

// ── 수동 금리뷰 입력 (거시 미연동 stopgap) ──
//   견해 API(user_views) 재사용: layer="mid"(중전제=거시 관심), theme="금리", stance 로 환경 저장.
//   통찰 입력일 뿐 — 자동 적용 아님. 저장 후 onSaved() 로 추천 재조회.
const RATE_OPTIONS: { key: string; label: string; stance: string; note: string }[] = [
  { key: "hiking", label: "인상기", stance: "negative", note: "금리 인상기 — 채권 가격 압박, 단기 선호" },
  { key: "high", label: "높음(고점)", stance: "neutral", note: "금리 높음(고점 부근) — 듀레이션 확대 검토 여지" },
  { key: "cut_expected", label: "인하 기대", stance: "positive", note: "금리 인하 기대 — 장기국채 매력 증가" },
  { key: "easing", label: "하락(인하)", stance: "positive", note: "금리 하락(인하 국면) — 장기국채 유리" },
  { key: "uncertain", label: "불확실", stance: "observe", note: "금리 방향 불확실 — 중립·관찰" },
];
function RateReviewInput({ accountId, onSaved }: { accountId: number; onSaved: () => void }) {
  const [sel, setSel] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const save = useCallback(async () => {
    const opt = RATE_OPTIONS.find((o) => o.key === sel);
    if (!opt) return;
    setSaving(true);
    setMsg(null);
    try {
      const r = await fetch(`/api/accounts/${accountId}/views`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "add",
          layer: "mid", // 중전제(거시 관심) — user_views 는 grand|mid|short|long 만 허용
          theme: "금리",
          stance: opt.stance,
          note: `[금리뷰] ${opt.label} · ${opt.note}`,
        }),
      });
      if (r.status === 401) { setMsg("로그인이 필요합니다."); return; }
      if (r.status === 403) { setMsg("이 계좌에 대한 접근 권한이 없습니다."); return; }
      const j = await r.json();
      if (!r.ok || j?.ok === false) { setMsg(j?.error ?? "저장 실패"); return; }
      setMsg("금리뷰 저장됨 — 추천을 갱신합니다 (자동 적용 아님).");
      onSaved();
    } catch (e: any) {
      setMsg(e?.message ?? "저장 실패");
    } finally {
      setSaving(false);
    }
  }, [accountId, sel, onSaved]);
  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-3 space-y-2.5">
      <div className="text-xs font-medium text-neutral-600 flex items-center gap-1">
        <Percent className="w-3.5 h-3.5" /> 현재 금리 환경 입력 (거시 미연동 시)
      </div>
      <p className="text-[11px] text-neutral-500">
        거시 금리 데이터가 연동되지 않았다면, 직접 보는 <b>금리 환경</b>을 입력해 추천을 정교화할 수 있습니다.
        이는 <b>통찰(견해) 입력</b>일 뿐 — <b>자동으로 비중에 적용되지 않습니다.</b>
      </p>
      <div className="flex flex-wrap gap-1.5">
        {RATE_OPTIONS.map((o) => (
          <button
            key={o.key}
            onClick={() => setSel(o.key)}
            className={`px-2.5 py-1 rounded-full text-xs border transition ${
              sel === o.key ? "border-primary text-primary bg-primary-50" : "border-neutral-200 text-neutral-500"
            }`}
          >
            {o.label}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-2">
        <Button size="sm" variant="outline" disabled={!sel || saving} onClick={save}>
          {saving ? "저장 중…" : "금리뷰 저장(견해)"}
        </Button>
        {msg && <span className="text-[11px] text-neutral-500">{msg}</span>}
      </div>
    </div>
  );
}

// ── 금리 기반 국채 추천 카드 (bond_recommendation) ──
//   추천(제안)일 뿐 — 승인 전 미반영·자동 적용 0. 데이터/견해 없으면 일반 원칙만(가짜 숫자 0).
function BondRecommendationCard({ sec, accountId, onSaved }: { sec?: Section; accountId: number; onSaved: () => void }) {
  const d = sec?.ready ? sec?.data : null;
  const regime = regimeLabel(d?.rate_regime);
  const bondRatio = d ? pct(d.suggested_bond_ratio_pct) : null;
  const duration = d?.suggested_duration ?? null;
  const split = d?.suggested_split ?? null; // 단기/장기 분할 등
  const ladder: any[] = Array.isArray(d?.ladder) ? d.ladder : [];
  const rationale: any[] = Array.isArray(d?.rationale) ? d.rationale : [];
  const dataSource = d?.data_source ?? null;
  const requiresApproval = d?.requires_user_approval;
  // 추천을 신뢰성 있게 줄 수 있는가? (연동/견해 기반 + 숫자가 있을 때)
  const sLower = String(dataSource ?? "").toLowerCase();
  const hasSource = !!d && /macro|거시|연동|user|view|견해|수동|manual/.test(sLower) && !/^none$|^없음$/.test(sLower);
  const hasNumbers = bondRatio != null || duration != null;
  const generalOnly = !d || !hasSource || !hasNumbers;

  return (
    <Card className="border-sky-100">
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-1.5 text-base"><TrendingUp className="w-4 h-4 text-sky-600" /> 금리 기반 국채 추천</CardTitle>
        <DataSourceBadge source={dataSource} />
      </CardHeader>
      <CardBody className="space-y-3">
        {sec && !sec.ready ? (
          <div className="rounded-lg border border-dashed border-neutral-200 bg-neutral-50 p-3 text-xs text-neutral-500">
            {sec.note?.includes("준비") ? "금리 추천 엔진 준비 중" : "금리 추천 데이터 없음 · 미연동"} — 연동/입력 후 자동 표시됩니다 (mock/가짜 숫자 없음).
          </div>
        ) : null}

        <div className="flex items-center justify-between text-sm">
          <span className="text-neutral-500">현재 금리 환경 (rate regime)</span>
          <span className={`font-medium ${regime.tone}`}>{regime.label}</span>
        </div>

        {generalOnly ? (
          <div className="rounded-lg bg-warning/5 border border-warning/30 p-3 text-xs text-neutral-600 space-y-1">
            <div className="flex items-center gap-1 text-warning font-medium"><AlertTriangle className="w-3.5 h-3.5" /> 금리 데이터·견해 없음 — 일반 원칙만</div>
            <p>
              금리 추천은 <b>거시 데이터 또는 사용자 금리뷰</b>가 있을 때 정교해집니다. 현재는 정확한 비중·듀레이션 숫자를
              제시하지 않습니다(가짜 숫자 없음). 아래에서 <b>금리 환경을 입력</b>하면 추천이 정교화됩니다.
            </p>
            <p className="text-neutral-500">
              일반 원칙: 금리 <b>인상기</b>엔 단기국채로 변동 방어, 금리 <b>인하 기대</b>면 장기국채로 듀레이션 확대를 검토합니다.
            </p>
          </div>
        ) : (
          <>
            <div className="rounded-lg bg-sky-50/60 border border-sky-100 p-3 text-sm space-y-1.5">
              <div className="flex justify-between">
                <span className="text-neutral-600">추천 국채 비중 (방어자산 대비)</span>
                <span className="tabular-nums font-medium text-sky-700">{bondRatio == null ? "데이터 없음" : `${bondRatio}%`}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-neutral-600">추천 듀레이션</span>
                <span className="text-neutral-700">{duration ?? "데이터 없음"}</span>
              </div>
              {split && (
                <div className="flex justify-between pt-1 border-t border-sky-100 text-xs">
                  <span className="text-neutral-500">단기/장기 분할</span>
                  <span className="text-neutral-600">
                    {typeof split === "object"
                      ? Object.entries(split).map(([k, v]) => `${k} ${pct(v) ?? v}%`).join(" · ")
                      : String(split)}
                  </span>
                </div>
              )}
            </div>

            {ladder.length > 0 && (
              <div>
                <div className="text-xs text-neutral-500 mb-1.5 flex items-center gap-1"><Layers className="w-3.5 h-3.5" /> 만기 사다리(ladder)</div>
                <div className="flex flex-wrap gap-1.5">
                  {ladder.map((rung: any, i: number) => (
                    <Badge key={i} className="bg-neutral-100 text-neutral-600">
                      {typeof rung === "object"
                        ? `${rung.band ?? rung.duration ?? rung.maturity ?? "구간"}${rung.weight_pct != null ? ` ${pct(rung.weight_pct)}%` : ""}`
                        : String(rung)}
                    </Badge>
                  ))}
                </div>
              </div>
            )}

            {rationale.length > 0 && (
              <div>
                <div className="text-xs text-neutral-500 mb-1">근거</div>
                <ul className="text-xs text-neutral-600 list-disc pl-4 space-y-0.5">
                  {rationale.slice(0, 5).map((r: any, i: number) => <li key={i}>{String(r)}</li>)}
                </ul>
              </div>
            )}

            <div className="flex items-center justify-between text-xs">
              <ConfidenceTag value={d?.confidence} />
            </div>

            {/* 추천 → 채권 입력 안내 (자동 적용 X) */}
            <div className="rounded-lg bg-primary-50 border border-primary-100 p-3 text-xs text-primary-700 space-y-1">
              <div className="flex items-center gap-1 font-medium"><ArrowRight className="w-3.5 h-3.5" /> 이 추천대로 하려면</div>
              <p>
                전략 화면에서 <b>채권(국채) 비중을 직접 입력</b>하고 <b>3안을 재확정</b>하세요. 이 추천은
                <b> 자동으로 적용되지 않습니다.</b>
                {requiresApproval !== false && <> 적용은 <b>사용자 입력·승인 후에만</b> 이뤄집니다.</>}
              </p>
              <p className="text-primary-700/80">승인 전에는 목표비중(policy)·주문에 <b>반영되지 않습니다.</b></p>
            </div>
          </>
        )}

        {/* 수동 금리뷰 입력 — 항상 노출(거시 미연동 stopgap) */}
        <RateReviewInput accountId={accountId} onSaved={onSaved} />
      </CardBody>
    </Card>
  );
}

// ── 국채 비중 후보(A/B/C/D) 정규화 ──
//   bond_recommendation --options 의 options[] 를 흡수. 백엔드 키 변형을 관대하게 매핑(정직 — 없으면 null).
//   full_equiv(전체환산): 순현금/단기국채/장기국채/국채합계/위험%. account_memory.md 의
//   "국채=현금의 일부" 모델에 맞춰 govbond 은 방어 안의 일부로 표기.
type BondOption = {
  id: string;
  label: string;
  bondRatioPct: number | null; // 방어자산 대비 국채 비율
  pureCashPct: number | null;
  shortGovPct: number | null;
  longGovPct: number | null;
  govbondPct: number | null;
  riskAssetPct: number | null;
  rationale: string[];
  risingRateRisk: string | null;
  fallingRateBenefit: string | null;
  fxRisk: string | null;
  liquidity: string | null;
  accountFit: string | null;
  suitedWhen: string | null; // 적합 상황
  confidence: any;
  systemRecommended: boolean;
};

function num(v: any): number | null {
  return v == null || Number.isNaN(Number(v)) ? null : Number(v);
}
function strOrNull(v: any): string | null {
  if (v == null) return null;
  const s = String(v).trim();
  return s === "" ? null : s;
}
function toRationale(v: any): string[] {
  if (Array.isArray(v)) return v.map((x) => String(x)).filter(Boolean);
  if (v == null) return [];
  const s = String(v).trim();
  return s ? [s] : [];
}

function normalizeBondOption(raw: any, i: number): BondOption {
  const eq = raw?.full_equiv ?? raw?.total_breakdown ?? raw?.equiv ?? raw ?? {};
  const pick = (...keys: string[]) => {
    for (const k of keys) {
      const v = (eq as any)[k] ?? (raw as any)?.[k];
      if (v != null) return v;
    }
    return null;
  };
  const labelRaw = strOrNull(raw?.label);
  const letter = ["A", "B", "C", "D"][i] ?? String(i + 1);
  return {
    id: String(raw?.id ?? raw?.option ?? raw?.key ?? labelRaw ?? letter),
    // 백엔드 label 이 "A" 같은 글자만 주면 "A안" 으로 보강(정직 — 없으면 letter 안).
    label: labelRaw ? (/^[A-D]$/.test(labelRaw) ? `${labelRaw}안` : labelRaw) : `${letter}안`,
    bondRatioPct: num(raw?.govbond_ratio_pct ?? raw?.bond_ratio_pct ?? raw?.suggested_bond_ratio_pct ?? raw?.bond_pct),
    pureCashPct: num(pick("pure_cash_pct_of_total", "pure_cash_pct", "suggested_pure_cash_pct_of_total", "net_cash_pct")),
    shortGovPct: num(pick("short_govbond_pct_of_total", "short_govbond_pct", "suggested_short_govbond_pct_of_total")),
    longGovPct: num(pick("long_govbond_pct_of_total", "long_govbond_pct", "suggested_long_govbond_pct_of_total")),
    govbondPct: num(pick("govbond_pct_of_total", "govbond_pct", "suggested_govbond_pct_of_total", "bond_total_pct")),
    riskAssetPct: num(pick("risk_asset_pct", "risk_pct")),
    rationale: toRationale(raw?.rationale ?? raw?.why ?? raw?.reason),
    risingRateRisk: strOrNull(raw?.rising_rate_risk),
    fallingRateBenefit: strOrNull(raw?.falling_rate_benefit),
    fxRisk: strOrNull(raw?.fx_risk),
    liquidity: strOrNull(raw?.liquidity),
    accountFit: strOrNull(raw?.account_fit ?? raw?.fit),
    suitedWhen: strOrNull(raw?.suited_when ?? raw?.suited),
    confidence: raw?.confidence,
    systemRecommended: raw?.system_recommended === true,
  };
}

// ── 단/장 비율 · 한/미 지역 (비중 결정 후 선택 — draft 표시일 뿐, 자동 적용 0) ──
type DurationSplit = "short_heavy" | "balanced" | "long_heavy";
type BondRegion = "kr" | "us" | "kr_us";
const DURATION_OPTS: { key: DurationSplit; label: string; desc: string }[] = [
  { key: "short_heavy", label: "단기 위주", desc: "금리 변동 방어 · 가격 안정 (금리 인상기 선호)" },
  { key: "balanced", label: "단·장 분산(ladder)", desc: "만기 사다리로 금리 방향 불확실 대비" },
  { key: "long_heavy", label: "장기 위주", desc: "금리 인하 수혜 기대 · 단 가격 변동 큼" },
];
const REGION_OPTS: { key: BondRegion; label: string; desc: string }[] = [
  { key: "kr", label: "한국 국채", desc: "환위험 없음 · 원화 자산" },
  { key: "us", label: "미국 국채", desc: "금리·안전자산 위상 · 환(USD) 노출" },
  { key: "kr_us", label: "한·미 분산", desc: "환·금리 분산 — 한 쪽 쏠림 완화" },
];

// ── 비중 후보 카드 1장 (전체환산·왜·적합 상황·금리/환/유동성·confidence·system_recommended) ──
function BondOptionCard({
  opt, selected, onSelect,
}: { opt: BondOption; selected: boolean; onSelect: () => void }) {
  const rows = [
    { label: "순현금 (즉시 매수여력)", v: opt.pureCashPct, cls: "text-neutral-700" },
    { label: "국채 합계 (방어, 현금의 일부)", v: opt.govbondPct, cls: "text-sky-600" },
    { label: " · 단기국채", v: opt.shortGovPct, cls: "text-sky-500 pl-3" },
    { label: " · 장기국채", v: opt.longGovPct, cls: "text-sky-700 pl-3" },
    { label: "위험자산 (성장)", v: opt.riskAssetPct, cls: "text-neutral-700" },
  ];
  const Axis = ({ icon, label, value, tone }: { icon: React.ReactNode; label: string; value: string | null; tone?: string }) => (
    <div className="flex items-start gap-1.5 text-[11px]">
      <span className={`shrink-0 mt-0.5 ${tone ?? "text-neutral-400"}`}>{icon}</span>
      <span className="text-neutral-500 shrink-0">{label}</span>
      <span className="text-neutral-700">{value ?? <span className="text-neutral-300">데이터 없음</span>}</span>
    </div>
  );
  return (
    <div className={`rounded-lg border p-3 space-y-3 transition ${selected ? "border-primary bg-primary-50/40" : opt.systemRecommended ? "border-sky-300 bg-sky-50/40" : "border-neutral-200"}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-neutral-800">{opt.label}</span>
          {opt.systemRecommended && (
            <Badge className="bg-sky-100 text-sky-700"><Star className="w-3 h-3 mr-1" />시스템 추천</Badge>
          )}
        </div>
        <span className="tabular-nums text-sm text-sky-700 font-medium">
          국채 {opt.bondRatioPct == null ? "—" : `${pct(opt.bondRatioPct)}%`}<span className="text-[10px] text-neutral-400"> (방어 대비)</span>
        </span>
      </div>

      {/* 전체 환산 */}
      <div className="rounded-md bg-neutral-50 border border-neutral-100 p-2.5 text-xs space-y-1">
        <div className="text-[11px] text-neutral-400 mb-0.5">전체 환산 (합계 100 기준)</div>
        {rows.map((r) => (
          <div key={r.label} className="flex justify-between">
            <span className={r.cls}>{r.label}</span>
            <span className="tabular-nums">{r.v == null ? "—" : `${pct(r.v)}%`}</span>
          </div>
        ))}
      </div>

      {/* 왜 이 비중 / 적합 상황 */}
      {opt.rationale.length > 0 && (
        <div>
          <div className="text-[11px] text-neutral-400 mb-0.5">왜 이 비중인가</div>
          <ul className="text-xs text-neutral-600 list-disc pl-4 space-y-0.5">
            {opt.rationale.slice(0, 4).map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </div>
      )}
      {opt.suitedWhen && (
        <div className="text-xs text-neutral-600 flex items-start gap-1.5">
          <CheckCircle2 className="w-3.5 h-3.5 text-success shrink-0 mt-0.5" />
          <span><span className="text-neutral-400">적합 상황: </span>{opt.suitedWhen}</span>
        </div>
      )}
      {opt.accountFit && (
        <div className="text-xs text-neutral-600 flex items-start gap-1.5">
          <UserCheck className="w-3.5 h-3.5 text-primary shrink-0 mt-0.5" />
          <span><span className="text-neutral-400">계좌 적합성: </span>{opt.accountFit}</span>
        </div>
      )}

      {/* 금리 상승 리스크 / 금리 하락 기대 / 환율 / 유동성 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1 rounded-md border border-neutral-100 p-2.5">
        <Axis icon={<TrendingUp className="w-3 h-3" />} label="금리상승 리스크:" value={opt.risingRateRisk} tone="text-error" />
        <Axis icon={<TrendingDown className="w-3 h-3" />} label="금리하락 기대:" value={opt.fallingRateBenefit} tone="text-success" />
        <Axis icon={<Globe className="w-3 h-3" />} label="환율:" value={opt.fxRisk} tone="text-warning" />
        <Axis icon={<Droplet className="w-3 h-3" />} label="유동성:" value={opt.liquidity} tone="text-sky-500" />
      </div>

      <div className="flex items-center justify-between">
        <ConfidenceTag value={opt.confidence} />
        <Button size="sm" variant={selected ? "outline" : opt.systemRecommended ? "primary" : "outline"} onClick={onSelect}>
          {selected ? "선택됨 (draft)" : "이 안 선택"}
        </Button>
      </div>
    </div>
  );
}

// ── Step 1 국채 비중 후보 흐름 (후보 → 비중 선택 → 단/장 → 한/미 → 그 다음 ETF) ──
//   추천(제안)일 뿐. 선택은 draft 표시만 — 자동 적용/주문 0. 순서를 강제(비중 결정 전 ETF 노출 금지).
function BondBucketFlow({
  optionsSec, defensiveSec, govbondEtfSec,
}: { optionsSec?: Section; defensiveSec: Section; govbondEtfSec?: Section }) {
  const [picked, setPicked] = useState<string | null>(null);
  const [duration, setDuration] = useState<DurationSplit | null>(null);
  const [region, setRegion] = useState<BondRegion | null>(null);
  const [showEtf, setShowEtf] = useState(false);
  // 비중 후보 미연동 시에도 비교를 시작할 수 있게 — ETF 비교만 따로 펼치는 토글(순서 강제와 별개).
  const [showCompareOnly, setShowCompareOnly] = useState(false);
  // "나중에 결정" — 선택을 보류했음을 표시(자동 적용 0, 단지 UI 상태).
  const [deferred, setDeferred] = useState(false);

  const rawOptions: any[] = optionsSec?.ready
    ? (Array.isArray(optionsSec.data?.options) ? optionsSec.data.options
      : Array.isArray(optionsSec.data) ? optionsSec.data : [])
    : [];
  const options = useMemo(() => rawOptions.map((o, i) => normalizeBondOption(o, i)), [rawOptions]);
  const pendingOptions = !optionsSec?.ready;
  const noOptions = optionsSec?.ready && options.length === 0;
  // 엔진이 주는 장기채 변동성 경고 문구(있으면 사용 — 백엔드와 wording 동기화).
  const engineLongWarn: string | null = optionsSec?.ready ? strOrNull(optionsSec.data?.long_bond_volatility_warning) : null;

  // 순서 강제: 비중 결정(picked) 후에만 단/장 → 지역 → ETF 노출.
  const weightDecided = picked != null;
  const durationDecided = weightDecided && duration != null;
  const regionDecided = durationDecided && region != null;

  // 선택 버튼([A안][B안][C안][D안]) — 후보 letter(A/B/C/D)로 안내. system_recommended 안 강조.
  const letterOf = (opt: BondOption, i: number) => (/^[A-D]안$/.test(opt.label) ? opt.label[0] : ["A", "B", "C", "D"][i] ?? `${i + 1}`);
  const recommended = options.find((o) => o.systemRecommended) ?? null;

  return (
    <div className="space-y-4">
      {/* 안내 — 운용 수단 설명 + 순서 강제 */}
      <div className="rounded-lg bg-sky-50/60 border border-sky-100 p-3 text-xs text-neutral-600 space-y-1.5">
        <div className="flex items-center gap-1.5 font-medium text-sky-700"><Scale className="w-3.5 h-3.5" /> 먼저 국채 "비중"과 성격을 정합니다 (ETF 티커는 그 다음)</div>
        <p>
          국채 ETF 는 <b>10년 보유 종목이 아니라 방어·완충·금리대응의 운용 수단</b>입니다. 그래서
          <b> 어떤 ETF 를 살지보다 "국채를 얼마나(비중)·어떤 성격(단/장·지역)으로 둘지"를 먼저</b> 정합니다.
          아래에서 <b>비중 후보를 고르고 → 단/장 비율 → 한/미 지역</b>을 정한 뒤 ETF 후보를 펼칩니다.
        </p>
      </div>

      {/* 1) 국채 비중 후보 A/B/C/D */}
      <div className="space-y-2.5">
        <div className="text-sm font-medium text-neutral-700 flex items-center gap-1.5">
          <span className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-primary-50 text-primary text-xs">1</span>
          국채 비중 후보 — 이 안 중 하나를 선택
        </div>
        {pendingOptions ? (
          <div className="rounded-lg border border-dashed border-neutral-200 bg-neutral-50 p-3 text-xs text-neutral-500">
            {optionsSec?.note?.includes("준비") ? "국채 비중 후보 엔진 준비 중" : "국채 비중 후보 데이터 없음 · 미연동"} — 연동/입력 후 자동 표시됩니다 (mock/가짜 숫자 없음).
          </div>
        ) : noOptions ? (
          <NotReady note={optionsSec?.note ?? "국채 비중 후보 데이터 없음 · 미연동"} />
        ) : (
          <div className="space-y-2.5">
            {options.map((opt) => (
              <BondOptionCard
                key={opt.id}
                opt={opt}
                selected={picked === opt.id}
                onSelect={() => { setPicked((p) => (p === opt.id ? null : opt.id)); setShowEtf(false); }}
              />
            ))}
          </div>
        )}
      </div>

      {/* 추천(C안 등) 근거 — 쉬운 설명 + 명시적 선택 버튼 행 */}
      {!pendingOptions && !noOptions && options.length > 0 && (
        <div className="rounded-lg bg-sky-50/60 border border-sky-100 p-3 text-xs text-neutral-600 space-y-2">
          <div className="flex items-center gap-1.5 font-medium text-sky-700"><Star className="w-3.5 h-3.5" /> 추천 근거(쉬운 설명) — 정답 아님, 비교 후 선택</div>
          {recommended ? (
            <p>
              시스템 추천은 <b>{recommended.label}</b>입니다 — 예: <b>순현금 24 · 단기국채 9.6 · 장기국채 6.4 · 위험자산 60</b> 식의 구성이면,
              <b> 유동성(즉시 매수여력)을 유지하면서 일부 국채로 방어를 운용하는 균형형</b>입니다.
              다만 <b>장기국채는 가격 변동(평가손익)이 커서 안전자산이 아닙니다 — 변동성에 주의</b>하세요.
              실제 수치는 위 후보 카드의 "전체 환산"을 보고 비교하세요(가짜 숫자 없음).
            </p>
          ) : (
            <p>
              현재 <b>시스템 추천 안이 지정되지 않았습니다</b>(금리 데이터·견해 부족). 후보들의 전체 환산(순현금/단기·장기국채/위험자산)을 비교해
              <b> 유동성과 방어, 장기국채 변동성</b>의 균형을 직접 고르세요. <b>국채 ETF 는 방어자산 구현 수단이며 수익 극대화 수단이 아닙니다.</b>
            </p>
          )}
          {/* 명시적 선택 버튼: [A안 선택][B안 선택]…[ETF 후보 비교 보기][나중에 결정] */}
          <div className="flex flex-wrap gap-1.5 pt-1">
            {options.map((opt, i) => {
              const L = letterOf(opt, i);
              const sel = picked === opt.id;
              return (
                <Button
                  key={opt.id}
                  size="sm"
                  variant={sel ? "primary" : opt.systemRecommended ? "primary" : "outline"}
                  onClick={() => { setPicked((p) => (p === opt.id ? null : opt.id)); setShowEtf(false); setDeferred(false); }}
                >
                  {sel ? `${L}안 선택됨` : `${L}안 선택`}{opt.systemRecommended ? " ★" : ""}
                </Button>
              );
            })}
            <Button
              size="sm"
              variant="outline"
              onClick={() => setShowCompareOnly((s) => !s)}
            >
              {showCompareOnly ? "ETF 후보 비교 접기" : "ETF 후보 비교 보기"}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => { setPicked(null); setDuration(null); setRegion(null); setShowEtf(false); setDeferred(true); }}
            >
              나중에 결정
            </Button>
          </div>
          {deferred && (
            <p className="text-[11px] text-neutral-500">선택을 보류했습니다 — 자동으로 적용되는 것은 없습니다(언제든 다시 고를 수 있습니다).</p>
          )}
        </div>
      )}

      {/* "ETF 후보 비교 보기" — 비중 결정 전에도 후보를 미리 비교(추천 강제 아님). 비중 결정과 별개. */}
      {showCompareOnly && (
        <div className="space-y-2 rounded-lg border border-neutral-200 p-3">
          <div className="text-sm font-medium text-neutral-700 flex items-center gap-1.5">
            <Layers className="w-4 h-4 text-sky-600" /> 국채 ETF 후보 비교 (미리 보기)
          </div>
          <p className="text-[11px] text-neutral-500">
            비중을 정하기 전에 후보를 <b>미리 비교</b>합니다 — 보고 나서 위에서 비중·성격을 정하세요. C안을 바로 확정하지 말고 비교 후 선택하세요.
          </p>
          <GovbondEtfCompare sec={govbondEtfSec} />
        </div>
      )}

      {/* 2) 단/장 비율 — 비중 선택 후에만 */}
      {weightDecided && (
        <div className="space-y-2">
          <div className="text-sm font-medium text-neutral-700 flex items-center gap-1.5">
            <span className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-primary-50 text-primary text-xs">2</span>
            단기/장기 비율
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
            {DURATION_OPTS.map((o) => (
              <button
                key={o.key}
                onClick={() => { setDuration(o.key); setShowEtf(false); }}
                className={`rounded-lg border p-2.5 text-left transition ${duration === o.key ? "border-primary bg-primary-50/50 text-primary-700" : "border-neutral-200 text-neutral-600"}`}
              >
                <div className="text-sm font-medium">{o.label}</div>
                <div className="text-[11px] text-neutral-500 mt-0.5">{o.desc}</div>
              </button>
            ))}
          </div>
          {/* 장기국채 변동성 경고 (엔진 문구 우선, 없으면 기본) */}
          {(duration === "long_heavy" || duration === "balanced") && (
            <p className="text-[11px] text-warning flex items-start gap-1">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>
                <b>장기국채는 금리 하락 수혜를 보지만 가격 변동이 큽니다 — 안전자산이 아닙니다.</b>{" "}
                {engineLongWarn ?? "장기 비중을 키울수록 가격 등락(평가손익)이 커집니다."}
              </span>
            </p>
          )}
        </div>
      )}

      {/* 3) 한/미 지역 — 단/장 선택 후에만 */}
      {durationDecided && (
        <div className="space-y-2">
          <div className="text-sm font-medium text-neutral-700 flex items-center gap-1.5">
            <span className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-primary-50 text-primary text-xs">3</span>
            한국/미국 지역
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
            {REGION_OPTS.map((o) => (
              <button
                key={o.key}
                onClick={() => { setRegion(o.key); setShowEtf(false); }}
                className={`rounded-lg border p-2.5 text-left transition ${region === o.key ? "border-primary bg-primary-50/50 text-primary-700" : "border-neutral-200 text-neutral-600"}`}
              >
                <div className="text-sm font-medium">{o.label}</div>
                <div className="text-[11px] text-neutral-500 mt-0.5">{o.desc}</div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* 4) ETF 후보 — 비중·성격 결정 후에만 펼침 */}
      <div className="space-y-2">
        <div className="text-sm font-medium text-neutral-700 flex items-center gap-1.5">
          <span className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-primary-50 text-primary text-xs">4</span>
          국채 ETF 후보
        </div>
        {!regionDecided ? (
          <div className="rounded-lg border border-dashed border-neutral-200 bg-neutral-50 p-3 text-xs text-neutral-500 flex items-start gap-1.5">
            <Info className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            <span><b>ETF 후보는 비중·성격(단/장·지역) 결정 이후</b>에 표시됩니다. 위 1~3단계를 먼저 정해 주세요. (ETF 티커를 비중 결정 전에 앞세우지 않습니다.)</span>
          </div>
        ) : (
          <div className="space-y-2">
            <button
              onClick={() => setShowEtf((s) => !s)}
              className="text-xs text-primary flex items-center gap-1"
            >
              {showEtf ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
              {showEtf ? "ETF 후보 접기" : "국채 ETF 후보 펼치기 (비중·성격 결정 완료)"}
            </button>
            {showEtf && (
              <div className="space-y-4">
                {/* 풍부한 비교(역할/장점/리스크/적합성/데이터품질) — govbond_etf --compare */}
                <GovbondEtfCompare sec={govbondEtfSec} />
                {/* 시드 후보표(검증 필요) — bond_bucket 의 govbond_etf_candidates */}
                <DefensiveStep sec={defensiveSec} etfOnly />
              </div>
            )}
          </div>
        )}
      </div>

      {/* 추천 → 적용 안내 (자동 적용 0) */}
      <div className="rounded-lg bg-primary-50 border border-primary-100 p-3 text-xs text-primary-700 space-y-1">
        <div className="flex items-center gap-1 font-medium"><ArrowRight className="w-3.5 h-3.5" /> 선택한 안대로 하려면</div>
        <p>
          전략 화면에서 <b>채권(국채) 비중을 직접 입력</b>하고 <b>3안을 재확정</b>하세요. 여기서의 선택은
          <b> 표시(draft)일 뿐 — 자동으로 적용되지 않습니다.</b>
        </p>
        <p className="text-primary-700/80">승인 전에는 목표비중(policy)·주문에 <b>반영되지 않습니다.</b></p>
      </div>
    </div>
  );
}

// ── Step 1: 방어자산 내부 구성 ── bond_bucket --account N
//   etfOnly=true: 비중·성격 결정 후 ETF 후보표만 표시(비중표/설명 생략).
//   contextOnly=true: 현재 방어 비중표만(ETF 티커는 비중 결정 전 노출 금지).
function DefensiveStep({ sec, etfOnly = false, contextOnly = false }: { sec: Section; etfOnly?: boolean; contextOnly?: boolean }) {
  if (!sec?.ready || !sec.data) return <NotReady note={sec?.note} />;
  const d = sec.data;
  const b = d.breakdown ?? {};
  const pureCash = pct(b.pure_cash_pct);
  const shortGov = pct(b.short_govbond_pct);
  const longGov = pct(b.long_govbond_pct);
  const govbond = pct(b.govbond_pct);
  const riskAsset = pct(b.risk_asset_pct);
  const durationPref = b.duration_pref ?? null;
  const candidates: any[] = d.govbond_etf_candidates ?? [];
  const rows = [
    { label: "순현금 (즉시 매수여력)", v: pureCash, cls: "text-neutral-700" },
    { label: "국채 합계 (방어, 현금의 일부)", v: govbond, cls: "text-sky-600" },
    { label: " · 단기국채 (금리 변동 방어)", v: shortGov, cls: "text-sky-500 pl-3" },
    { label: " · 장기국채 (경기 둔화 대응)", v: longGov, cls: "text-sky-700 pl-3" },
    { label: "위험자산 (성장)", v: riskAsset, cls: "text-neutral-700" },
  ];
  // ETF 후보표 블록 — etfOnly 일 때 단독 렌더, 아니면 비중표 아래에 같이.
  const etfBlock = (
    <div className="space-y-2">
      <div className="text-xs text-neutral-500 flex items-center gap-1"><Layers className="w-3.5 h-3.5" /> 국채 ETF 후보 (시드 · 검증 필요)</div>
      {/* 운용수단 + 장기채 경고 */}
      <div className="rounded-lg bg-warning/5 border border-warning/30 p-2.5 text-[11px] text-neutral-600 space-y-1">
        <p className="text-warning flex items-start gap-1">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          <span><b>장기국채는 금리 하락 수혜를 보지만 가격 변동이 큽니다 — 안전자산이 아닙니다.</b></span>
        </p>
        <p>국채 ETF 는 <b>10년 보유 종목이 아니라 방어·완충·금리대응의 운용 수단</b>입니다. ETF 선택은 위에서 정한 <b>비중·성격(단/장·지역)</b>에 맞춰 고릅니다(정답 아님 — 비교용).</p>
      </div>
      {candidates.length === 0 ? (
        <NotReady note="국채 ETF 후보 데이터 없음 · 미연동" />
      ) : (
        <GovbondCandidateTable candidates={candidates} />
      )}
    </div>
  );
  if (etfOnly) return etfBlock;
  return (
    <div className="space-y-4">
      <div className="rounded-lg bg-sky-50/60 border border-sky-100 p-3 text-sm space-y-1.5">
        {rows.map((r) => (
          <div key={r.label} className="flex justify-between">
            <span className={r.cls}>{r.label}</span>
            <span className="tabular-nums">{r.v == null ? "데이터 없음" : `${r.v}%`}</span>
          </div>
        ))}
        {durationPref && (
          <div className="flex justify-between pt-1 border-t border-sky-100 text-xs">
            <span className="text-neutral-500">듀레이션 선호</span>
            <span className="text-neutral-600">{durationPref}</span>
          </div>
        )}
      </div>
      <p className="text-xs text-neutral-500">
        {contextOnly && <span className="text-neutral-400">현재(확정/미리보기) 방어 구성입니다. </span>}
        방어자산은 "잃지 않는 투자"의 토대입니다. <b>순현금</b>은 즉시 진입 여력이고,
        <b> 국채는 현금의 일부(방어)</b>로 보며 — <b>단기국채</b>는 금리 변동 방어,
        <b> 장기국채</b>는 경기 둔화 시 완충 역할입니다. 비중은 정책·관점에 따라 비교 대상이며 정답이 아닙니다.
      </p>
      {!contextOnly && etfBlock}
    </div>
  );
}

// ── Step 2–5: bucket 후보 비교표 (security_selection.comparison[]) ──
// 각 후보 컬럼은 실제 키에서 추출: cost{expense_ratio_pct,available,reason},
//   volatility{value,available}, view_fit{fit,detail}, confidence{value,...}, risks[].
function costCell(cost: any) {
  if (!cost) return <span className="text-neutral-300">—</span>;
  if (cost.available && cost.expense_ratio_pct != null)
    return <span className="tabular-nums">{pct(cost.expense_ratio_pct)}%</span>;
  return <span className="text-neutral-400 text-[11px]">미연동</span>;
}
function volCell(vol: any) {
  if (!vol) return <span className="text-neutral-300">—</span>;
  if (vol.available && vol.value != null) return <span className="tabular-nums">{pct(vol.value)}</span>;
  return <span className="text-neutral-400 text-[11px]">미연동</span>;
}
function fitCell(fit: any) {
  if (!fit || fit.fit == null) return <span className="text-neutral-300">—</span>;
  const f = String(fit.fit);
  if (f === "unknown") return <span className="text-neutral-400 text-[11px]">판단 보류</span>;
  return <span className="text-neutral-700">{f}</span>;
}
function risksCell(risks: any) {
  if (!Array.isArray(risks) || risks.length === 0) return <span className="text-neutral-300">—</span>;
  return (
    <ul className="text-[11px] text-neutral-500 space-y-0.5 list-disc pl-3.5 max-w-[18rem]">
      {risks.slice(0, 3).map((r: any, i: number) => <li key={i}>{String(r)}</li>)}
    </ul>
  );
}

function CompareTable({ candidates }: { candidates: any[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-neutral-200">
      <table className="w-full text-xs">
        <thead className="bg-neutral-50 text-neutral-500">
          <tr>
            <th className="text-left font-medium px-2.5 py-2 whitespace-nowrap">후보</th>
            <th className="text-left font-medium px-2.5 py-2 whitespace-nowrap">자산군</th>
            <th className="text-left font-medium px-2.5 py-2 whitespace-nowrap">데이터 가용성</th>
            <th className="text-left font-medium px-2.5 py-2 whitespace-nowrap">비용(보수)</th>
            <th className="text-left font-medium px-2.5 py-2 whitespace-nowrap">변동성</th>
            <th className="text-left font-medium px-2.5 py-2 whitespace-nowrap">관점 적합성</th>
            <th className="text-left font-medium px-2.5 py-2 whitespace-nowrap">리스크</th>
            <th className="text-left font-medium px-2.5 py-2 whitespace-nowrap">confidence</th>
          </tr>
        </thead>
        <tbody>
          {candidates.map((cand, i) => {
            const avail = cand.data_availability ?? {};
            return (
              <tr key={cand.ticker ?? cand.name ?? i} className="border-t border-neutral-100 align-top">
                <td className="px-2.5 py-2">
                  <span className="font-medium text-neutral-800">{cand.name ?? cand.ticker ?? "—"}</span>
                  {cand.ticker && <span className="text-neutral-400 tabular-nums"> · {cand.ticker}</span>}
                </td>
                <td className="px-2.5 py-2 text-neutral-500">{cand.asset_class ?? "—"}</td>
                <td className="px-2.5 py-2">
                  <div className="flex flex-wrap gap-1">
                    {AVAIL_AXES.map((a) => (
                      <AvailBadge key={a.key} connected={isAvailConnected(avail[a.key])} label={a.label} />
                    ))}
                  </div>
                </td>
                <td className="px-2.5 py-2">{costCell(cand.cost)}</td>
                <td className="px-2.5 py-2">{volCell(cand.volatility)}</td>
                <td className="px-2.5 py-2">{fitCell(cand.view_fit)}</td>
                <td className="px-2.5 py-2">{risksCell(cand.risks)}</td>
                <td className="px-2.5 py-2"><ConfidenceTag value={cand.confidence?.value} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── 개별주 여부 판정 (ETF 스코어카드 vs 우량주 필터 분기) ──
function isEtfCandidate(cand: any): boolean {
  const ac = String(cand?.asset_class ?? "").toLowerCase();
  if (ac.includes("etf")) return true;
  if (ac.includes("equity") && !ac.includes("etf")) return false;
  // asset_class 불명이면 인버스/etf 표식이 없는 한 개별주로 보지 않음(보수적으로 ETF 취급 안 함은 위험) → 불명 처리.
  return ac === "" ? false : ac.includes("fund");
}

// ── ETF 스코어카드 (구성·비용·추적오차·중복노출·역할·대안·왜 적합) ──
//   security_selection.compare 의 후보 객체에서 추출. 미연동 항목은 "미연동"으로 정직 표기.
function EtfScorecard({ cand }: { cand: any }) {
  const cost = cand.cost ?? {};
  const ov = cand.overlap_exposure;
  const overlaps = Array.isArray(ov) ? ov : [];
  const ovUnavailable = !Array.isArray(ov);
  // 미연동일 수 있는 확장 필드(B 작업이 추가하면 표시, 없으면 "미연동").
  const constituents = cand.constituents ?? cand.holdings ?? null; // ETF 구성
  const trackingErr = cand.tracking_error ?? cand.tracking_error_pct ?? null; // 추적오차
  const role = cand.role ?? cand.bucket_role ?? null; // 역할
  const alternatives = Array.isArray(cand.alternatives) ? cand.alternatives : null; // 대안
  const whyFit = cand.view_fit?.detail ?? cand.why_fit ?? null; // 왜 적합

  const Field = ({ label, children }: { label: string; children: React.ReactNode }) => (
    <div className="flex justify-between gap-3 py-1 border-b border-neutral-100 last:border-0">
      <span className="text-neutral-500 shrink-0">{label}</span>
      <span className="text-right text-neutral-700">{children}</span>
    </div>
  );
  const NA = <span className="text-neutral-400 text-[11px]">미연동</span>;
  return (
    <div className="rounded-lg border border-neutral-200 bg-neutral-50/60 p-3 text-xs space-y-0.5">
      <div className="font-medium text-neutral-700 mb-1.5 flex items-center gap-1"><Layers className="w-3.5 h-3.5" /> ETF 스코어카드</div>
      <Field label="구성(주요 보유)">
        {Array.isArray(constituents) && constituents.length > 0
          ? constituents.slice(0, 5).map((c: any) => (typeof c === "string" ? c : c?.ticker ?? c?.name)).filter(Boolean).join(", ")
          : NA}
      </Field>
      <Field label="비용(운용보수)">
        {cost.available && cost.expense_ratio_pct != null ? <span className="tabular-nums">{pct(cost.expense_ratio_pct)}%</span> : NA}
      </Field>
      <Field label="추적오차">
        {trackingErr != null ? <span className="tabular-nums">{pct(trackingErr)}%</span> : NA}
      </Field>
      <Field label="기존 보유와 중복노출">
        {ovUnavailable ? (
          <span className="text-neutral-400 text-[11px]">{ov?.reason ?? "미연동"}</span>
        ) : overlaps.length === 0 ? (
          <span className="text-neutral-500">겹침 없음(임계 미만)</span>
        ) : (
          <span className={overlaps.some((o: any) => o.concentration_flag) ? "text-warning" : "text-neutral-700"}>
            {overlaps.map((o: any) => `${o.with} ${o.overlap_weight_pct != null ? pct(o.overlap_weight_pct) + "%" : ""}`).join(", ")}
            {overlaps.some((o: any) => o.concentration_flag) && " · 집중 위험"}
          </span>
        )}
      </Field>
      <Field label="역할">{role ? String(role) : NA}</Field>
      <Field label="대안">
        {alternatives && alternatives.length > 0
          ? alternatives.map((a: any) => (typeof a === "string" ? a : a?.ticker ?? a?.name)).filter(Boolean).join(", ")
          : NA}
      </Field>
      <Field label="왜 적합한가">{whyFit ? String(whyFit) : NA}</Field>
    </div>
  );
}

// ── 개별주 우량주 표시 (재무·밸류에이션 필터 결과) ──
//   B 작업이 quality 블록(financials/valuation filter)을 추가하면 표시, 없으면 "데이터 필요·필터 미적용" 정직.
function QualityCard({ cand }: { cand: any }) {
  const q = cand.quality ?? cand.fundamentals ?? null; // B 확장 필드
  const finConnected = isAvailConnected((cand.data_availability ?? {}).financials);
  const Field = ({ label, children }: { label: string; children: React.ReactNode }) => (
    <div className="flex justify-between gap-3 py-1 border-b border-neutral-100 last:border-0">
      <span className="text-neutral-500 shrink-0">{label}</span>
      <span className="text-right text-neutral-700">{children}</span>
    </div>
  );
  const NA = <span className="text-neutral-400 text-[11px]">데이터 필요 · 필터 미적용</span>;
  if (!q || !finConnected) {
    return (
      <div className="rounded-lg border border-dashed border-neutral-200 bg-neutral-50 p-3 text-xs text-neutral-500">
        <div className="font-medium text-neutral-600 mb-1 flex items-center gap-1"><Star className="w-3.5 h-3.5" /> 우량주 필터(재무·밸류에이션)</div>
        재무 데이터 <b>미연동</b> — <b>우량주 필터를 적용하지 않았습니다(정직)</b>. 재무/밸류에이션 데이터 연결 후 적용됩니다.
      </div>
    );
  }
  const excluded = Array.isArray(q.excluded_reasons) ? q.excluded_reasons : (q.exclude_reason ? [q.exclude_reason] : []);
  return (
    <div className="rounded-lg border border-neutral-200 bg-neutral-50/60 p-3 text-xs space-y-0.5">
      <div className="font-medium text-neutral-700 mb-1.5 flex items-center gap-1"><Star className="w-3.5 h-3.5" /> 우량주 필터(재무·밸류에이션)</div>
      <Field label="통과 여부">{q.passed === false ? <span className="text-warning">필터 탈락</span> : q.passed === true ? <span className="text-success">통과</span> : NA}</Field>
      <Field label="수익성/재무건전성">{q.profitability ?? q.financial_health ?? NA}</Field>
      <Field label="밸류에이션">{q.valuation ?? NA}</Field>
      {excluded.length > 0 && (
        <div className="pt-1 text-warning">
          제외 사유: {excluded.map((r: any) => String(r)).join("; ")}
        </div>
      )}
    </div>
  );
}

// ── 6축 점검(읽기 전용) — 후보의 하락 6축 종합 + confidence + 추천 강도 + 적정 비중 안내 ──
//   데이터는 security_selection.compare 의 decline_risk{available,risk_level,risk_score,
//   holistic_risk,overall_confidence} + confidence{value,strong_conclusion_allowed} 에서 읽는다.
//   데이터 부족 축/미연동은 제외(정직). confidence 낮으면 단정하지 않고 추천 강도를 낮춘다.
function SixAxisCheck({ cand, bucket }: { cand: any; bucket: string }) {
  const dr = cand.decline_risk ?? null;
  const available = dr?.available === true;
  const holistic = dr?.holistic_risk;
  const oconf = dr?.overall_confidence;
  const riskLevel = dr?.risk_level;
  const cv = cand.confidence?.value;
  const strongOk = cand.confidence?.strong_conclusion_allowed === true;
  const inverse = bucket === "semiconductor_inverse";

  // 추천 강도(읽기 전용·단정 금지): confidence·strong 가능 여부·위험으로 결정.
  let strength = "참고(약)";
  let strengthCls = "bg-neutral-100 text-neutral-500";
  if (!available || cv == null) {
    strength = "데이터 부족 — 단정 안 함";
    strengthCls = "bg-neutral-100 text-neutral-400";
  } else if (strongOk && (oconf == null || Number(oconf) >= 0.5)) {
    strength = "비교적 강함(사람 승인)";
    strengthCls = "bg-success/10 text-success";
  } else if (Number(cv) >= 0.4) {
    strength = "보통(후보)";
    strengthCls = "bg-primary-50 text-primary-700";
  }

  // 적정 비중 안내(읽기 전용·정답 아님). 헤지 bucket 은 한도 내 소량, 위험 high 면 비중 축소 톤.
  let weightHint: string;
  if (inverse) {
    weightHint = "헤지 전용 — 인버스 한도(총 10%) 안에서 소량. 롱 대체 금지.";
  } else if (available && riskLevel === "high") {
    weightHint = "하락 6축 위험 high — 진입 비중 축소·분할 진입 검토(단정 아님).";
  } else if (!available || cv == null) {
    weightHint = "데이터 부족 — 적정 비중 단정 불가. 자료 보강 후 재평가(정직).";
  } else {
    weightHint = "확정안 bucket 한도 안에서 분산 배분(단일 집중 금지). 정답 아님 — 비교용.";
  }

  return (
    <div className="rounded-lg border border-neutral-200 bg-neutral-50/60 p-3 text-xs space-y-1.5">
      <div className="font-medium text-neutral-700 flex items-center gap-1"><ShieldAlert className="w-3.5 h-3.5" /> 하락 6축 점검</div>
      {available ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-neutral-600">
          {holistic != null && <span>종합 위험 <b className="tabular-nums">{pct(holistic) ?? holistic}</b></span>}
          {riskLevel && <span>등급 <b>{String(riskLevel)}</b></span>}
          <span>종합 신뢰도 {oconf != null ? `${Math.round(Number(oconf) * 100)}%` : "미상"}{oconf != null && Number(oconf) < 0.3 && " — 낮음, 단정 안 함"}</span>
        </div>
      ) : (
        <p className="text-neutral-400">6축 종합 데이터 부족(가격/일봉 등 미연동) — 분석에서 제외했습니다(정직).</p>
      )}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-neutral-500">추천 강도</span>
        <Badge className={strengthCls}>{strength}</Badge>
        <ConfidenceTag value={cv} />
      </div>
      <p className="text-[11px] text-neutral-500">적정 비중: {weightHint}</p>
      <p className="text-[11px] text-neutral-400">데이터 없는 축은 제외 · confidence 낮으면 단정 안 함 · 자동 주문/적용 없음(승인 전 미반영).</p>
    </div>
  );
}

// ── 선택 가능한 후보 카드 (체크박스 + 펼침 상세[스코어카드/우량주]) ──
function CandidateRow({
  cand, selected, onToggle, bucket,
}: { cand: any; selected: boolean; onToggle: () => void; bucket: string }) {
  const [open, setOpen] = useState(false);
  const etf = isEtfCandidate(cand);
  const avail = cand.data_availability ?? {};
  const connectedCount = AVAIL_AXES.filter((a) => isAvailConnected(avail[a.key])).length;
  const inverse = String(cand.asset_class ?? "").includes("inverse") || String(cand.ticker ?? "").toUpperCase().includes("INVERSE");
  return (
    <div className={`rounded-lg border p-3 transition ${selected ? "border-primary bg-primary-50/40" : "border-neutral-200"}`}>
      <div className="flex items-start gap-2">
        <button onClick={onToggle} className="mt-0.5 text-primary shrink-0" aria-label={selected ? "선택 해제" : "선택"}>
          {selected ? <CheckSquare className="w-4 h-4" /> : <Square className="w-4 h-4 text-neutral-300" />}
        </button>
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between gap-2">
            <div className="text-sm">
              <span className="font-medium text-neutral-800">{cand.name ?? cand.ticker ?? "—"}</span>
              {cand.ticker && <span className="text-neutral-400 tabular-nums"> · {cand.ticker}</span>}
              <Badge className="ml-2 bg-neutral-100 text-neutral-500">{etf ? "ETF" : "개별주"}</Badge>
            </div>
            <button onClick={() => setOpen((o) => !o)} className="text-xs text-neutral-400 flex items-center gap-0.5 shrink-0">
              {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />} 상세
            </button>
          </div>
          <div className="flex flex-wrap gap-1 mt-1.5">
            {AVAIL_AXES.map((a) => (
              <AvailBadge key={a.key} connected={isAvailConnected(avail[a.key])} label={a.label} />
            ))}
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-0.5 mt-1.5 text-[11px] text-neutral-500">
            <span>비용 {cand.cost?.available && cand.cost?.expense_ratio_pct != null ? `${pct(cand.cost.expense_ratio_pct)}%` : "미연동"}</span>
            <span>변동성 {cand.volatility?.available && cand.volatility?.value != null ? pct(cand.volatility.value) : "미연동"}</span>
            <span>관점 적합 {cand.view_fit?.fit && cand.view_fit.fit !== "unknown" ? cand.view_fit.fit : "보류"}</span>
            <ConfidenceTag value={cand.confidence?.value} />
            {connectedCount < 3 && <span className="text-warning">데이터 부족 · 단정 안 함</span>}
            {inverse && <span className="text-warning">인버스 — 헤지 전용</span>}
          </div>
        </div>
      </div>
      {open && (
        <div className="mt-3 pl-6 space-y-3">
          <SixAxisCheck cand={cand} bucket={bucket} />
          {etf ? <EtfScorecard cand={cand} /> : <QualityCard cand={cand} />}
          {Array.isArray(cand.risks) && cand.risks.length > 0 && (
            <ul className="text-[11px] text-neutral-500 list-disc pl-4 space-y-0.5">
              {cand.risks.slice(0, 4).map((r: any, i: number) => <li key={i}>{String(r)}</li>)}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function BucketStep({
  sec, label, bucket, selectedTickers, onToggle,
}: {
  sec: Section; label: string; bucket: string;
  selectedTickers: Set<string>; onToggle: (cand: any) => void;
}) {
  if (!sec?.ready || !sec.data) return <NotReady note={sec?.note} />;
  const d = sec.data;
  const candidates: any[] = d.comparison ?? [];
  // bucket 전체 가용성 = 후보들의 data_availability 합집합(축별 하나라도 연결되면 연결).
  const aggAvail: Record<string, boolean> = {};
  for (const a of AVAIL_AXES) {
    aggAvail[a.key] = candidates.some((c) => isAvailConnected((c.data_availability ?? {})[a.key]));
  }
  const connectedCount = AVAIL_AXES.filter((a) => aggAvail[a.key]).length;
  const strongPossible = d.strong_conclusion_possible === true;
  const weak = !strongPossible || connectedCount < 3 || candidates.length === 0;
  const headline: string | null = d.headline ?? null;
  return (
    <div className="space-y-4">
      <AvailabilityRow avail={aggAvail} />
      {weak ? (
        <div className="rounded-lg border border-dashed border-warning/40 bg-warning/5 p-3 text-sm text-warning flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 shrink-0" /> 데이터 부족 — <b>강한 결론 불가 · 후보 비교 단계</b>입니다.
        </div>
      ) : null}
      {candidates.length === 0 ? (
        <NotReady note={sec.note ?? "후보 데이터 없음 · 미연동"} />
      ) : (
        <>
          <p className="text-xs text-neutral-500">
            후보를 <b>선택</b>하면 다음 "비중 조절" 단계에서 <b>확정안 bucket 한도 안</b>으로 draft 비중이 배분됩니다.
            선택은 초안일 뿐 주문·policy에 반영되지 않습니다. 상세를 펼치면 ETF 스코어카드 / 우량주 필터를 볼 수 있습니다.
          </p>
          <div className="space-y-2">
            {candidates.map((cand, i) => (
              <CandidateRow
                key={cand.ticker ?? cand.name ?? i}
                cand={cand}
                bucket={bucket}
                selected={selectedTickers.has(`${bucket}:${cand.ticker}`)}
                onToggle={() => onToggle(cand)}
              />
            ))}
          </div>
          <details className="text-xs text-neutral-500">
            <summary className="cursor-pointer text-neutral-400">표 형식으로 비교 보기</summary>
            <div className="mt-2"><CompareTable candidates={candidates} /></div>
          </details>
        </>
      )}
      <div className="rounded-lg bg-neutral-50 border border-neutral-100 p-3 text-xs text-neutral-600">
        <div className="font-medium text-neutral-700 mb-1">비교 관점 ({label})</div>
        {headline ? (
          <p>{headline}</p>
        ) : (
          <p className="text-neutral-500">
            정답을 제시하지 않습니다. 같은 bucket 안에서도 <b>A는 정책·관점에 적합</b>, <b>B는 비용↓</b>,
            <b> C는 성장↑·변동↑</b> 처럼 트레이드오프가 다릅니다. 목적이 <b>"잃지 않는 투자"</b>라면 변동성이
            낮고 중복이 적은 후보가 적합할 수 있습니다. (근거 데이터가 부족하면 단정하지 않습니다.)
          </p>
        )}
      </div>
    </div>
  );
}

// ── 개별주 bucket 옵션 A/B/C (없음/5%/10%) ──
//   실수치는 weight_allocator --individual-options (options.{A,B,C}) 에서 가져온다.
//   엔진 미연동이면 기본 라벨로 graceful 표시(정직).
function EquityOptionPicker({
  value, onChange, options,
}: {
  value: EquityOption; onChange: (v: EquityOption) => void;
  options?: { ready: boolean; data: any | null; note?: string };
}) {
  const od = options?.ready ? options.data : null;
  const riskPct = od?.risk_asset_pct;
  const opt = (k: "A" | "B" | "C") => od?.options?.[k] ?? null;
  const fallback: Record<EquityOption, { label: string; desc: string }> = {
    none: { label: "A · 없음", desc: "개별주 미편입 (ETF 중심)" },
    "5": { label: "B · 5%", desc: "위험자산 안에서 개별주 5%" },
    "10": { label: "C · 10%", desc: "위험자산 안에서 개별주 10%" },
  };
  const cells: { v: EquityOption; k: "A" | "B" | "C" }[] = [
    { v: "none", k: "A" }, { v: "5", k: "B" }, { v: "10", k: "C" },
  ];
  return (
    <div className="space-y-2">
      <div className="text-sm font-medium text-neutral-700 flex items-center gap-1"><Scale className="w-4 h-4" /> 개별주 bucket 옵션 (A/B/C)</div>
      <p className="text-xs text-neutral-500">
        개별주는 <b>위험자산{riskPct != null ? ` ${pct(riskPct)}%` : ""} 안에서 배분</b>되며 (추가 비중 아님)
        <b> 확정안 합계 100은 변하지 않습니다.</b> 개별주는 <b>단일 1~2% · 10종 내외</b>로 분산을 권장합니다(집중 위험 방지).
        {!od && <span className="text-neutral-400"> (옵션 실수치 미연동 — 기본 안내 표시)</span>}
      </p>
      <div className="grid grid-cols-3 gap-2">
        {cells.map(({ v, k }) => {
          const o = opt(k);
          return (
            <button
              key={v}
              onClick={() => onChange(v)}
              className={`rounded-lg border p-2.5 text-left transition ${
                value === v ? "border-primary bg-primary-50/50 text-primary-700" : "border-neutral-200 text-neutral-600"
              }`}
            >
              <div className="text-sm font-medium">{fallback[v].label}</div>
              {o ? (
                <div className="text-[11px] text-neutral-500 mt-0.5">
                  carve {pct(o.individual_cap_pct) ?? 0}% · {o.suggested_count ?? 0}종
                  {o.capped_to_risk && <span className="text-warning"> · 위험자산 부족 축소</span>}
                </div>
              ) : (
                <div className="text-[11px] text-neutral-500 mt-0.5">{fallback[v].desc}</div>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ── 비중 배분 결과 표 (weight_allocator 응답) ──
function AllocResult({ alloc }: { alloc: AllocSection }) {
  if (!alloc.ready || !alloc.data) {
    return (
      <NotReady note={alloc.note ?? "비중 배분 엔진(weight_allocator) 준비 중 — 선택 후 자동 계산됩니다."} />
    );
  }
  const d = alloc.data;
  // weight_allocator.allocate() 출력에 맞춤 (graceful 대체 키도 허용).
  const holdings: any[] = (d.holdings ?? d.draft_weights ?? d.allocations ?? []).filter((h: any) => Number(h.weight_pct ?? h.weight ?? 0) > 0);
  const summary: any[] = d.bucket_summary ?? [];
  const warnings: any[] = d.over_limit_warnings ?? d.warnings ?? [];
  const blocks = warnings.filter((w: any) => (w?.level ?? "warn") === "block");
  const total = d.total_pct ?? (holdings.length ? Math.round(holdings.reduce((s, r) => s + Number(r.weight_pct ?? r.weight ?? 0), 0) * 10) / 10 : null);
  const totalOk = d.total_is_100 ?? (total != null && Math.abs(Number(total) - 100) < 0.5);
  const wMsg = (w: any) => (typeof w === "string" ? w : (w.msg ?? w.message ?? JSON.stringify(w)));
  return (
    <div className="space-y-3">
      {warnings.length > 0 && (
        <div className={`rounded-lg border p-3 text-xs space-y-1 ${blocks.length ? "border-error/40 bg-error/5 text-error" : "border-warning/40 bg-warning/5 text-warning"}`}>
          <div className="font-medium flex items-center gap-1">
            <AlertTriangle className="w-3.5 h-3.5" /> 한도 {blocks.length ? "초과 차단(block)" : "경고"} ({warnings.length})
          </div>
          <ul className="list-disc pl-4 space-y-0.5">
            {warnings.map((w: any, i: number) => (
              <li key={i}>{(w?.level === "block" ? "[차단] " : "")}{wMsg(w)}</li>
            ))}
          </ul>
          <p className="text-[11px] opacity-80">한도(단일종목·섹터·헤지)는 실제 주문 단계에서 다시 hard-block 으로 재검증됩니다.</p>
        </div>
      )}
      {holdings.length === 0 ? (
        <NotReady note="배분된 draft 비중이 없습니다 — 후보를 선택했는지 확인하세요." />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-neutral-200">
          <table className="w-full text-xs">
            <thead className="bg-neutral-50 text-neutral-500">
              <tr>
                <th className="text-left font-medium px-2.5 py-2">bucket</th>
                <th className="text-left font-medium px-2.5 py-2">종목/ETF</th>
                <th className="text-left font-medium px-2.5 py-2">근거</th>
                <th className="text-right font-medium px-2.5 py-2">draft 비중</th>
              </tr>
            </thead>
            <tbody>
              {holdings.map((r: any, i: number) => (
                <tr key={(r.ticker ?? r.ref ?? "") + i} className="border-t border-neutral-100">
                  <td className="px-2.5 py-2 text-neutral-500">{BUCKET_LABEL[r.bucket] ?? r.bucket ?? "—"}</td>
                  <td className="px-2.5 py-2 text-neutral-800">
                    {r.ticker ?? r.ref ?? r.name ?? "—"}
                    {r.ref && r.ticker ? <span className="text-neutral-400"> · {r.ref}</span> : null}
                  </td>
                  <td className="px-2.5 py-2 text-neutral-400 text-[11px] max-w-[14rem]">{r.basis ?? "—"}</td>
                  <td className="px-2.5 py-2 text-right tabular-nums">{pct(r.weight_pct ?? r.weight) ?? "—"}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="rounded-lg bg-neutral-50 border border-neutral-100 p-3 text-xs space-y-1.5">
        <div className="flex items-center justify-between">
          <span className="text-neutral-500">합계</span>
          <span className={`tabular-nums font-medium ${totalOk ? "text-success" : "text-warning"}`}>
            {total == null ? "—" : `${pct(total)}%`} {totalOk ? "(100 불변 유지)" : "(검토 필요)"}
          </span>
        </div>
        {summary.length > 0 && (
          <div className="pt-1 border-t border-neutral-100 space-y-0.5">
            <div className="text-neutral-400">bucket 합계 (확정안 한도 = 불변, 초과 0)</div>
            {summary.map((s: any) => (
              <div key={s.key} className="flex justify-between">
                <span className="text-neutral-600">{BUCKET_LABEL[s.key] ?? s.key}</span>
                <span className="tabular-nums text-neutral-600">
                  {pct(s.allocated_pct) ?? "—"}% / 한도 {pct(s.weight_pct) ?? "—"}%
                  {s.headroom_pct != null && Number(s.headroom_pct) > 0 ? <span className="text-neutral-400"> (여유 {pct(s.headroom_pct)}%)</span> : null}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Step: 비중 조절 (후보 선택 → weight_allocator draft 비중) ──
function WeightStep({
  picks, equityOption, onEquityOption, alloc, allocLoading, onRecalc,
}: {
  picks: Pick[]; equityOption: EquityOption; onEquityOption: (v: EquityOption) => void;
  alloc: AllocSection | null; allocLoading: boolean; onRecalc: () => void;
}) {
  return (
    <div className="space-y-4">
      <p className="text-xs text-neutral-500">
        앞 단계에서 고른 후보를 <b>확정안 bucket 한도 안에서</b> 배분합니다. <b>합계 100·bucket 합은 불변</b>이며
        한도를 넘으면 경고와 함께 자동 축소됩니다. 이 비중은 <b>draft(초안)</b>일 뿐 policy·주문에 반영되지 않습니다.
      </p>

      <div className="rounded-lg border border-neutral-200 p-3 text-xs">
        <div className="font-medium text-neutral-700 mb-1.5 flex items-center gap-1"><Sliders className="w-3.5 h-3.5" /> 선택한 후보 ({picks.length})</div>
        {picks.length === 0 ? (
          <p className="text-neutral-400">선택된 후보가 없습니다 — 이전 bucket 단계에서 후보를 선택하세요.</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {picks.map((p) => (
              <Badge key={`${p.bucket}:${p.ticker}`} className="bg-primary-50 text-primary-700">
                {p.name ?? p.ticker} <span className="text-primary-400">· {BUCKET_LABEL[p.bucket] ?? p.bucket}</span>
              </Badge>
            ))}
          </div>
        )}
      </div>

      <EquityOptionPicker value={equityOption} onChange={onEquityOption} options={alloc?.options} />

      <Button size="sm" onClick={onRecalc} disabled={picks.length === 0 || allocLoading}>
        {allocLoading ? "배분 계산 중…" : "확정안 한도 안에서 비중 배분(draft)"}
      </Button>

      {allocLoading ? (
        <div className="text-sm text-neutral-400 py-4 text-center">배분 계산 중…</div>
      ) : alloc && (alloc.ready || alloc.data) ? (
        <AllocResult alloc={alloc} />
      ) : picks.length > 0 ? (
        <p className="text-xs text-neutral-400">위 버튼을 눌러 draft 비중을 계산하세요.</p>
      ) : null}

      <p className="text-[11px] text-neutral-400">
        배분 결과는 <b>저장되지 않은 초안</b>입니다. 실제 목표비중·주문은 승인 + 리스크 게이트 + 사장님 최종 승인 후에만 반영됩니다.
      </p>
    </div>
  );
}

// ── Step 6: 승인 (draft 만) ──
function ApprovalStep({ acknowledged, onAck, savedAt }: { acknowledged: boolean; onAck: (v: boolean) => void; savedAt: string | null }) {
  const savedLabel = savedAt
    ? new Date(savedAt).toLocaleString("ko-KR", { dateStyle: "short", timeStyle: "short" })
    : null;
  return (
    <div className="space-y-4">
      <div className="rounded-lg bg-warning/5 border border-warning/30 p-3 text-sm text-neutral-700">
        승인을 눌러도 <b>draft(초안)로만 저장</b>됩니다. <b>승인 전에는 목표비중(policy)·주문에 반영되지 않습니다.</b>
        실제 반영·주문은 별도의 리스크 게이트 + 사장님 최종 승인 단계에서만 이뤄집니다.
      </div>
      <Button
        variant={acknowledged ? "outline" : "primary"}
        onClick={() => onAck(!acknowledged)}
      >
        {acknowledged ? "초안 승인 표시됨 (미반영)" : "이 선정안을 초안으로 승인 (draft)"}
      </Button>
      {acknowledged && (
        <p className="text-xs text-success flex items-center gap-1">
          <CheckCircle2 className="w-3.5 h-3.5" /> 초안 승인으로 표시됨 — policy·주문에는 <b>반영되지 않았습니다</b>.
        </p>
      )}
      {/* 정직한 저장 피드백: 선택·승인 표시는 계좌별 draft 로 백엔드에 실제 저장된다(새로고침해도 유지). */}
      <p className="text-xs text-neutral-500 flex items-center gap-1">
        {savedLabel
          ? <>💾 자동 저장됨 — {savedLabel} (계좌별 draft, 새로고침해도 유지)</>
          : <>선택·carve·승인 표시는 자동으로 draft 에 저장됩니다(아직 저장된 변경 없음).</>}
      </p>
    </div>
  );
}

// ── Step 7: 분할 진입 계획 (분할 횟수·기간만 입력 → 시스템이 저점 지정가 사다리 자동 생성) ──
function SplitEntryStep({ accountId, picks, plan, setPlan }: {
  accountId: number; picks: Pick[]; plan: any; setPlan: (p: any) => void;
}) {
  const [rounds, setRounds] = useState(3);
  const [period, setPeriod] = useState(14);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const generate = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const res = await fetch(`/api/accounts/${accountId}/split-plan`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          rounds, period_days: period,
          picks: picks.map((p) => ({ bucket: p.bucket, ticker: p.ticker })),
        }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) { setErr(data?.error || "생성 실패"); setPlan(null); }
      else setPlan(data);
    } catch (e: any) {
      setErr(e?.message || "오류"); setPlan(null);
    } finally { setLoading(false); }
  }, [accountId, rounds, period, picks, setPlan]);

  const steps: any[] = plan?.steps ?? [];
  const sell = plan?.sell_rules;
  return (
    <div className="space-y-3">
      <p className="text-xs text-neutral-500">
        진입은 <b>시장가가 아닌 지정가(예측 진입)</b>입니다. <b>분할 횟수와 기간만</b> 정하면 시스템이
        "무릎" 지점의 <b>저점 지정가 사다리</b>(회차가 깊을수록 더 낮은 가)를 알아서 만듭니다.
        <b> 걸리면 체결, 미체결이면 매수하지 않습니다</b>(추격 없음). 결과는 <b>초안(draft)</b>이며 주문을 생성하지 않습니다.
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs text-neutral-500">분할 횟수
          <Input className="mt-1 w-20" type="number" min={1} max={10} value={rounds}
                 onChange={(e) => setRounds(Math.max(1, Math.min(10, Number(e.target.value) || 1)))} />
        </label>
        <label className="text-xs text-neutral-500">기간(일)
          <Input className="mt-1 w-24" type="number" min={1} max={120} value={period}
                 onChange={(e) => setPeriod(Math.max(1, Math.min(120, Number(e.target.value) || 1)))} />
        </label>
        <Button size="sm" onClick={generate} disabled={loading || picks.length === 0}>
          {loading ? "생성 중…" : "저점 지정가 자동 생성"}
        </Button>
      </div>
      {picks.length === 0 && <p className="text-[11px] text-warning">먼저 종목을 선택하세요.</p>}
      {err && <p className="text-[11px] text-error">{err}</p>}

      {steps.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-neutral-200">
          <table className="w-full text-xs">
            <thead className="bg-neutral-50 text-neutral-500">
              <tr>
                <th className="px-2 py-1 text-left">회차</th>
                <th className="px-2 py-1 text-left">종목</th>
                <th className="px-2 py-1 text-right">지정가</th>
                <th className="px-2 py-1 text-right">수량</th>
                <th className="px-2 py-1 text-right">저점</th>
                <th className="px-2 py-1 text-right">시점</th>
              </tr>
            </thead>
            <tbody>
              {steps.map((s, i) => (
                <tr key={i} className="border-t border-neutral-100">
                  <td className="px-2 py-1">{s.round_no}/{s.total_rounds}</td>
                  <td className="px-2 py-1 font-medium text-neutral-700">{s.ticker}</td>
                  <td className="px-2 py-1 text-right tabular-nums">{won(s.limit_price)}</td>
                  <td className="px-2 py-1 text-right tabular-nums">{s.qty}주</td>
                  <td className="px-2 py-1 text-right text-neutral-400">-{s.drop_pct}%</td>
                  <td className="px-2 py-1 text-right text-neutral-400">D+{s.schedule_day}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {plan && steps.length === 0 && (
        <p className="text-[11px] text-neutral-500">
          생성된 회차가 없습니다 — 회차 예산이 1주 가격보다 작거나(고가주) 현재가 미연동입니다(정직). 횟수를 줄여 보세요.
        </p>
      )}
      {plan?.skipped?.length > 0 && (
        <p className="text-[11px] text-neutral-400">제외: {plan.skipped.map((x: any) => `${x.ticker}(${x.reason})`).slice(0, 4).join(" · ")}</p>
      )}
      {sell && (
        <p className="text-[11px] text-neutral-500">
          매도 규칙: 목표 {sell.target_pct ?? "—"}% / 손절 {sell.stop_pct ?? "—"}% / 보수전환 {sell.conservative_switch ? "사용" : "—"} · 그 외 시그널은 제안→승인
        </p>
      )}
      <p className="text-[11px] text-neutral-400">
        리스크 게이트(1주문 최대 비중·세션 주문 수)는 실제 주문 단계에서 hard-block 으로 재검증됩니다. 위는 미반영 초안입니다.
      </p>
    </div>
  );
}

// ── Step 8: 주문 전 최종 확인 (live hard lock) ──
function FinalCheckStep({ plan, acknowledged, picks, alloc }: { plan: any; acknowledged: boolean; picks: Pick[]; alloc: AllocSection | null }) {
  const filled: any[] = plan?.steps ?? [];
  const draft: any[] = (alloc?.ready && alloc.data)
    ? (alloc.data.holdings ?? alloc.data.draft_weights ?? alloc.data.allocations ?? []).filter((h: any) => Number(h.weight_pct ?? h.weight ?? 0) > 0)
    : [];
  return (
    <div className="space-y-4">
      <div className="rounded-lg bg-error/5 border border-error/30 p-3 text-sm text-error flex items-start gap-2">
        <Lock className="w-4 h-4 shrink-0 mt-0.5" />
        <div>
          <b>실전(live) 주문은 잠겨 있습니다 (hard lock).</b> 이 화면은 자동으로 주문을 생성하지 않습니다.
          실제 주문은 모의투자(paper) 우선 원칙 + 리스크 게이트 + 사장님 최종 승인 + live 전환 확인을 모두 통과한 뒤에만 가능합니다.
        </div>
      </div>
      <div className="rounded-lg border border-neutral-200 p-3 text-sm space-y-1.5">
        <div className="font-medium text-neutral-700">초안 요약 (미반영)</div>
        <div className="flex justify-between"><span className="text-neutral-500">선택한 후보</span><span className="tabular-nums">{picks.length}종</span></div>
        {draft.length > 0 && (
          <div className="pt-1 border-t border-neutral-100 space-y-0.5">
            <div className="text-xs text-neutral-400">draft 비중 (미반영)</div>
            {draft.map((r: any, i: number) => (
              <div key={(r.ticker ?? r.ref ?? "") + i} className="flex justify-between text-xs text-neutral-500">
                <span>· {r.ticker ?? r.ref ?? r.name ?? "—"}</span>
                <span className="tabular-nums">{pct(r.weight_pct ?? r.weight) ?? "—"}%</span>
              </div>
            ))}
          </div>
        )}
        <div className="flex justify-between pt-1 border-t border-neutral-100"><span className="text-neutral-500">초안 승인 표시</span><span>{acknowledged ? "예 (draft)" : "아니오"}</span></div>
        <div className="flex justify-between"><span className="text-neutral-500">분할 진입 회차</span><span className="tabular-nums">{filled.length}건(예약 지정가)</span></div>
        {filled.map((s, i) => (
          <div key={i} className="flex justify-between text-xs text-neutral-500">
            <span>· {s.ticker} {s.round_no}/{s.total_rounds}회 · D+{s.schedule_day} · -{s.drop_pct}%</span>
            <span className="tabular-nums">{Number(s.limit_price).toLocaleString("ko-KR")}원 / {s.qty}주</span>
          </div>
        ))}
      </div>
      <p className="text-xs text-neutral-400">
        위 내용은 <b>저장된 초안</b>이며 주문이 아닙니다. 무승인 자동매매는 금지되어 있습니다.
      </p>
    </div>
  );
}

// ── 메인 ──
export function SelectionFlow({ accountId }: { accountId: number }) {
  const [payload, setPayload] = useState<Payload | null>(null);
  const [loading, setLoading] = useState(true);
  const [denied, setDenied] = useState<string | null>(null);
  const [step, setStep] = useState(0);

  const [acknowledged, setAcknowledged] = useState(false);
  const [splitPlan, setSplitPlan] = useState<any>(null);   // 분할 진입 자동 생성 결과(draft)
  // 저장된 draft 복원이 끝나기 전엔 autosave 금지(초기 빈 상태로 서버 draft 덮어쓰기 방지).
  const hydratedRef = useRef(false);
  const [draftSavedAt, setDraftSavedAt] = useState<string | null>(null); // 마지막 자동저장 시각(UI 표기)

  // 후보 선택(키 = `${bucket}:${ticker}`)과 메타(POST picks 구성용).
  const [selected, setSelected] = useState<Map<string, Pick>>(new Map());
  const [equityOption, setEquityOption] = useState<EquityOption>("none");
  const [alloc, setAlloc] = useState<AllocSection | null>(null);
  const [allocLoading, setAllocLoading] = useState(false);

  const selectedTickers = useMemo(() => new Set(selected.keys()), [selected]);
  const picks = useMemo(() => Array.from(selected.values()), [selected]);

  const togglePick = useCallback((bucket: string, cand: any) => {
    const key = `${bucket}:${cand.ticker}`;
    setSelected((prev) => {
      const next = new Map(prev);
      if (next.has(key)) next.delete(key);
      else next.set(key, { bucket, ticker: cand.ticker, name: cand.name ?? null, asset_class: cand.asset_class ?? null });
      return next;
    });
    setAlloc(null); // 선택 변경 시 이전 배분 무효화(자동 적용 금지).
  }, []);

  // 비중 배분 계산(POST). 자동 주문/적용 0 — 계산 결과만 화면에 표시.
  const recalcAlloc = useCallback(async () => {
    if (picks.length === 0) return;
    setAllocLoading(true);
    try {
      const r = await fetch(`/api/accounts/${accountId}/selection`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ picks, equity_option: equityOption }),
      });
      if (r.status === 401) { setDenied("로그인이 필요합니다."); return; }
      if (r.status === 403) { setDenied("이 계좌에 대한 접근 권한이 없습니다."); return; }
      const j = await r.json();
      setAlloc({ ready: !!j?.ready, data: j?.data ?? null, note: j?.note, options: j?.individual_options });
    } catch (e: any) {
      setAlloc({ ready: false, data: null, note: e?.message ?? "배분 계산 실패" });
    } finally {
      setAllocLoading(false);
    }
  }, [accountId, picks, equityOption]);

  // 비중 조절 단계 진입 시 개별주 A/B/C 옵션을 먼저 조회(선택과 무관한 정보). POST(picks 없이) → options.
  useEffect(() => {
    if (step !== 5 || alloc) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`/api/accounts/${accountId}/selection`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ picks: [] }),
        });
        if (r.status === 401 || r.status === 403) return; // GET 단계에서 이미 denied 처리됨
        const j = await r.json();
        if (!cancelled) setAlloc({ ready: false, data: null, note: j?.note, options: j?.individual_options });
      } catch {
        /* graceful: 옵션 없으면 picker 가 기본 안내 표시 */
      }
    })();
    return () => { cancelled = true; };
  }, [step, accountId, alloc]);

  const load = useCallback(async () => {
    setLoading(true);
    setDenied(null);
    hydratedRef.current = false; // 계좌 전환/재조회 동안 autosave 중단 → 복원 후 재개.
    try {
      const r = await fetch(`/api/accounts/${accountId}/selection`, { cache: "no-store" });
      if (r.status === 401) { setDenied("로그인이 필요합니다."); setLoading(false); return; }
      if (r.status === 403) { setDenied("이 계좌에 대한 접근 권한이 없습니다."); setLoading(false); return; }
      const j = await r.json();
      setPayload(j);
      // 저장된 선정 draft 복원 — 고른 종목·개별주 carve·초안 승인 표시를 잃지 않는다.
      try {
        const dr = await fetch(`/api/accounts/${accountId}/selection-draft`, { cache: "no-store" });
        if (dr.ok) {
          const d = (await dr.json())?.draft;
          if (d) {
            const m = new Map<string, Pick>();
            for (const p of (d.picks ?? [])) {
              if (p?.bucket && p?.ticker) {
                m.set(`${p.bucket}:${p.ticker}`, {
                  bucket: p.bucket, ticker: p.ticker, name: p.name ?? null, asset_class: p.asset_class ?? null,
                });
              }
            }
            setSelected(m);
            if (d.equity_option === "none" || d.equity_option === "5" || d.equity_option === "10") {
              setEquityOption(d.equity_option);
            }
            setAcknowledged(!!d.acknowledged);
            setDraftSavedAt(d.updated_at ?? null);
          }
        }
      } catch { /* graceful: draft 없으면 빈 상태로 시작 */ }
    } catch (e: any) {
      setDenied(e?.message ?? "조회 실패");
    }
    setLoading(false);
    hydratedRef.current = true; // 복원 완료 — 이후 변경부터 autosave.
  }, [accountId]);

  useEffect(() => { load(); }, [load]);

  // 선정 draft 자동 저장(디바운스 700ms). 고른 종목·carve·초안 승인 표시를 백엔드 DB 에
  // 저장만 한다 — policy/주문에는 반영하지 않는다(자동 적용 0). 복원 전에는 저장 금지.
  useEffect(() => {
    if (!hydratedRef.current) return;
    // 아무것도 안 고른 빈 초기 상태는 굳이 저장하지 않음(빈 row 생성 방지).
    if (picks.length === 0 && equityOption === "none" && !acknowledged) return;
    const t = setTimeout(() => {
      fetch(`/api/accounts/${accountId}/selection-draft`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ picks, equity_option: equityOption, acknowledged }),
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((j) => { if (j?.ok) setDraftSavedAt(new Date().toISOString()); })
        .catch(() => { /* graceful: 저장 실패는 다음 변경 때 재시도 */ });
    }, 700);
    return () => clearTimeout(t);
  }, [accountId, picks, equityOption, acknowledged]);

  const order = payload?.bucket_order ?? ["global_core", "robotics", "semiconductor", "semiconductor_inverse", "treasury"];
  // Step 2–5 매핑: 글로벌코어 / 로봇 / 반도체 / (반도체인버스+국채 묶음 — 헤지·국채)
  const buckets = payload?.buckets ?? {};

  const stepContent = useMemo(() => {
    const bstep = (b: string) => (
      <BucketStep
        sec={buckets[b] ?? { ready: false, data: null }}
        label={BUCKET_LABEL[b]}
        bucket={b}
        selectedTickers={selectedTickers}
        onToggle={(cand) => togglePick(b, cand)}
      />
    );
    switch (step) {
      case 0:
        return (
          <div className="space-y-6">
            {/* 현재 방어 구성(맥락) — 비중표만(ETF 표는 추천형 흐름에서 비중·성격 결정 후 노출) */}
            <DefensiveStep sec={payload?.defensive ?? { ready: false, data: null }} contextOnly />
            {/* 추천형 흐름: 국채 비중 후보 → 비중 선택 → 단/장 → 한/미 → 그 다음 ETF */}
            <BondBucketFlow
              optionsSec={payload?.bond_options}
              defensiveSec={payload?.defensive ?? { ready: false, data: null }}
              govbondEtfSec={payload?.govbond_etf}
            />
            {/* 금리 기반 단일 추천 + 수동 금리뷰 입력(거시 미연동 stopgap) */}
            <BondRecommendationCard
              sec={payload?.bond_recommendation}
              accountId={accountId}
              onSaved={load}
            />
          </div>
        );
      case 1:
        return bstep("global_core");
      case 2:
        return bstep("robotics");
      case 3:
        return bstep("semiconductor");
      case 4:
        return (
          <div className="space-y-6">
            <div>
              <div className="text-sm font-medium text-neutral-700 mb-2">{BUCKET_LABEL.semiconductor_inverse}</div>
              {bstep("semiconductor_inverse")}
            </div>
            <div>
              <div className="text-sm font-medium text-neutral-700 mb-2">{BUCKET_LABEL.treasury}</div>
              {bstep("treasury")}
            </div>
          </div>
        );
      case 5:
        return (
          <WeightStep
            picks={picks}
            equityOption={equityOption}
            onEquityOption={(v) => { setEquityOption(v); setAlloc(null); }}
            alloc={alloc}
            allocLoading={allocLoading}
            onRecalc={recalcAlloc}
          />
        );
      case 6:
        return <ApprovalStep acknowledged={acknowledged} onAck={setAcknowledged} savedAt={draftSavedAt} />;
      case 7:
        return <SplitEntryStep accountId={accountId} picks={picks} plan={splitPlan} setPlan={setSplitPlan} />;
      case 8:
        return <FinalCheckStep plan={splitPlan} acknowledged={acknowledged} picks={picks} alloc={alloc} />;
      default:
        return null;
    }
  }, [step, payload, buckets, acknowledged, splitPlan, selectedTickers, togglePick, picks, equityOption, alloc, allocLoading, recalcAlloc, accountId, load, draftSavedAt]);

  if (loading) return <div className="text-sm text-neutral-400 py-10 text-center">불러오는 중…</div>;

  if (denied) {
    return (
      <Card className="border-error/30">
        <CardBody className="text-center py-10 text-sm text-error flex flex-col items-center gap-2">
          <ShieldAlert className="w-6 h-6" /> {denied}
        </CardBody>
      </Card>
    );
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold text-neutral-900">종목 · ETF 선정 (세부)</h1>
        <p className="text-sm text-neutral-500 mt-1">
          3안 확정 후 단계입니다. 방어자산 구성부터 bucket별 후보 비교까지 <b>비교·검토</b>하고,
          초안 승인 → 분할 진입 계획까지 작성합니다. <b>이 화면은 주문을 생성하지 않습니다.</b>
        </p>
      </div>

      <DisclaimerBanner />

      <DataStatusBanner payload={payload} />

      <Stepper current={step} onJump={setStep} />

      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle>{step + 1}. {STEPS[step]}</CardTitle>
          {step <= 4 && <Badge className="bg-neutral-100 text-neutral-500">조회 · 비교 · 선택</Badge>}
          {step === 5 && <Badge className="bg-neutral-100 text-neutral-500">draft 비중 (미반영)</Badge>}
          {step === 6 && <Badge className="bg-warning/10 text-warning">초안 승인 (미반영)</Badge>}
          {step >= 7 && <Badge className="bg-error/10 text-error">draft · 자동주문 없음</Badge>}
        </CardHeader>
        <CardBody>{stepContent}</CardBody>
      </Card>

      <div className="flex justify-between">
        <Button variant="ghost" disabled={step === 0} onClick={() => setStep((s) => Math.max(0, s - 1))}>← 이전</Button>
        <Button variant={step === STEPS.length - 1 ? "outline" : "primary"} disabled={step === STEPS.length - 1} onClick={() => setStep((s) => Math.min(STEPS.length - 1, s + 1))}>
          {step === STEPS.length - 1 ? "마지막 단계" : "다음 →"}
        </Button>
      </div>

      <p className="text-xs text-neutral-400">
        본 화면은 투자 판단 보조용입니다. 자동으로 주문을 생성하지 않으며, 승인 전에는 policy·주문에 반영되지 않습니다.
        데이터가 부족한 축은 비교에서 제외하고, confidence 가 낮으면 단정하지 않습니다.
      </p>
    </div>
  );
}
