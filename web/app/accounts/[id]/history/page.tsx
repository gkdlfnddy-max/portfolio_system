import Link from "next/link";
import { getProfileHistory, getSelectionHistory } from "@/lib/server/portfolioDb";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { ArrowLeft } from "lucide-react";

export const dynamic = "force-dynamic";

const DURATION: Record<string, string> = { short: "단기", intermediate: "중기", long: "장기", mixed: "혼합" };
const VARIANT: Record<string, string> = { conservative: "보수", base: "기준", aggressive: "공격", custom: "수동" };

// 프로필 스냅샷에서 지역/채권 구조만 추출
function regionBondOf(snapshotJson: string): { region: string; bond: string } | null {
  try {
    const s = JSON.parse(snapshotJson);
    let region = "미설정";
    if (s.region_targets) {
      try {
        const rt = JSON.parse(s.region_targets) as Record<string, number>;
        const e = Object.entries(rt).filter(([, v]) => v != null);
        if (e.length) region = e.map(([k, v]) => `${k} ${v}%`).join(" · ");
      } catch { /* keep 미설정 */ }
    } else if (s.region_pref) region = `선호: ${s.region_pref}`;
    const bond = s.bond_target_pct != null
      ? `채권 ${s.bond_target_pct}%${s.bond_duration_pref ? `·${DURATION[s.bond_duration_pref] ?? s.bond_duration_pref}` : ""}`
      : "채권 미설정";
    return { region, bond };
  } catch { return null; }
}

// 확정 allocation JSON → 방어/지역/테마 구성 요약
function composeOf(allocationJson: string) {
  try {
    const rows = JSON.parse(allocationJson) as { kind: string; ref: string | null; weight_pct: number }[];
    const sum = (k: string) => Math.round(rows.filter((r) => r.kind === k).reduce((a, r) => a + r.weight_pct, 0) * 10) / 10;
    const anchors = rows.filter((r) => r.kind === "anchor")
      .map((r) => `${(r.ref ?? "").replace(/\s*기본배분$/, "")} ${r.weight_pct}%`);
    const tilts = rows.filter((r) => r.kind === "tilt").map((r) => `${r.ref} ${r.weight_pct}%`);
    return { cash: sum("cash"), bond: sum("bond"), hedge: sum("hedge"), anchors, tilts };
  } catch { return null; }
}

// 연속 동일 지역/채권은 변화만 보이도록 압축
function changesOnly(hist: { id: number; snapshot: string; source: string | null; created_at: string }[]) {
  const out: { id: number; created_at: string; source: string | null; region: string; bond: string }[] = [];
  let prev = "";
  for (const h of [...hist].reverse()) { // 오래된→최신
    const rb = regionBondOf(h.snapshot);
    if (!rb) continue;
    const key = rb.region + "|" + rb.bond;
    if (key !== prev) { out.push({ id: h.id, created_at: h.created_at, source: h.source, ...rb }); prev = key; }
  }
  return out.reverse(); // 최신→오래된
}

export default function HistoryPage({ params }: { params: { id: string } }) {
  const id = parseInt(params.id, 10);
  const profHist = getProfileHistory(id, 50);
  const sels = getSelectionHistory(id, 30);
  const regionBondChanges = changesOnly(profHist);

  return (
    <div className="max-w-2xl mx-auto px-5 py-10 space-y-6">
      <Link href={`/accounts/${id}`} className="text-sm text-neutral-500 flex items-center gap-1 hover:text-primary">
        <ArrowLeft className="w-4 h-4" /> 계좌 화면
      </Link>
      <div>
        <h1 className="text-2xl font-bold text-neutral-900">지역 · 채권 비중 변화 이력</h1>
        <p className="text-sm text-neutral-500 mt-1">
          정책의 지역/채권 목표와, 확정한 목표비중의 방어(현금·채권)·지역 구성이 시간에 따라 어떻게 바뀌었는지 봅니다.
          전부 append-only DB 기록입니다.
        </p>
      </div>

      {/* 1) 정책 지역/채권 변경 이력 */}
      <Card>
        <CardHeader><CardTitle>정책 지역·채권 목표 변경</CardTitle></CardHeader>
        <CardBody className="py-2">
          {regionBondChanges.length === 0 ? (
            <p className="text-sm text-neutral-400">변경 이력 없음 — 전략에서 지역/채권을 입력·저장하면 여기 쌓입니다.</p>
          ) : (
            <ul className="space-y-2.5">
              {regionBondChanges.map((c) => (
                <li key={c.id} className="border-l-2 border-primary-100 pl-3">
                  <div className="text-[11px] text-neutral-400">
                    {new Date(c.created_at).toLocaleString("ko-KR")} · {c.source ?? "—"}
                  </div>
                  <div className="text-sm text-neutral-800">🌐 {c.region}</div>
                  <div className="text-sm text-sky-600">🪙 {c.bond}</div>
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>

      {/* 2) 확정 allocation 구성 이력 */}
      <Card>
        <CardHeader><CardTitle>확정 목표비중 구성 이력</CardTitle></CardHeader>
        <CardBody className="py-2">
          {sels.length === 0 ? (
            <p className="text-sm text-neutral-400">확정 이력 없음 — 목표비중 3안에서 하나를 확정하면 여기 쌓입니다.</p>
          ) : (
            <ul className="space-y-3">
              {sels.map((s) => {
                const c = composeOf(s.allocation);
                return (
                  <li key={s.id} className={`rounded-lg border p-3 ${s.status === "active" ? "border-primary-200 bg-primary-50/30" : "border-neutral-100"}`}>
                    <div className="flex items-center justify-between gap-2">
                      <div className="text-sm font-medium text-neutral-800">
                        {VARIANT[s.variant ?? ""] ?? s.variant} 안
                        <span className={`ml-2 text-[10px] rounded px-1.5 py-0.5 ${s.status === "active" ? "bg-primary-100 text-primary-700" : "bg-neutral-100 text-neutral-400"}`}>{s.status === "active" ? "현재 적용" : s.status}</span>
                        {s.policy_version != null && <span className="ml-1 text-[10px] text-neutral-400">policy v{s.policy_version}</span>}
                      </div>
                      <span className="text-[11px] text-neutral-400">{new Date(s.selected_at).toLocaleString("ko-KR")}</span>
                    </div>
                    {c && (
                      <div className="mt-1.5 text-xs text-neutral-600 space-y-0.5">
                        <div>방어: 현금 <b>{c.cash}%</b>{c.bond > 0 ? <> · 채권 <b className="text-sky-600">{c.bond}%</b>(별도)</> : ""}{c.hedge > 0 ? <> · 헤지 {c.hedge}%</> : ""}</div>
                        {c.anchors.length > 0 && <div>🌐 지역: {c.anchors.join(" · ")}</div>}
                        {c.tilts.length > 0 && <div>테마: {c.tilts.join(" · ")}</div>}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
          <p className="text-[11px] text-neutral-400 mt-2">재선택·취소해도 이전 확정은 삭제되지 않고 superseded/cancelled 로 보존됩니다(추적성).</p>
        </CardBody>
      </Card>
    </div>
  );
}
