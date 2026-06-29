import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";

import { requireAccountUnlocked } from "@/lib/auth/account";
import { requireAccountAccessAndUnlocked } from "@/lib/auth/rbac";

// 분할 집행 — 승인된 plan 을 예약 지정가로 제출(웹 '집행' 배선). 백엔드 exec_run CLI.
// ⚠️ 안전: paper 우선 · approve 필수 · mode 명시 · live 는 이중확인 + KIS_LIVE_CONFIRM(백엔드 하드락).
//   시장가 차단·리스크 게이트·idempotency 는 submit_order 내부 게이트(여기서 우회 불가).
export const dynamic = "force-dynamic";
const pexec = promisify(execFile);

const LIVE_PHRASE = "I_UNDERSTAND_LIVE";

function accId(id: string): number | null {
  const n = parseInt(id, 10);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

async function runPy(args: string[]) {
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(root, ".venv", "bin", "python"), "python", "python3"]) {
    try {
      const { stdout } = await pexec(py, ["-m", "main_mission.portfolio_os.exec_run", ...args], {
        cwd: root, timeout: 60000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 4 * 1024 * 1024,
      });
      const text = stdout.trim();
      try { return JSON.parse(text); } catch {
        const s = text.indexOf("{"), e = text.lastIndexOf("}");
        if (s >= 0 && e > s) return JSON.parse(text.slice(s, e + 1));
        throw new Error("CLI JSON 파싱 실패");
      }
    } catch (e: any) {
      if (e?.code === "ENOENT" && !e?.stderr) continue;
      throw e;
    }
  }
  throw new Error("python 미발견");
}

export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;

  const body = await req.json().catch(() => ({} as any));

  // 1) 전략 승인 — 명시적으로 true 여야 한다(무승인 집행 금지). 없으면 거부(백엔드도 재검증).
  if (body?.approve !== true) {
    return NextResponse.json({ ok: false, stage: "approval", error: "전략 승인(approve=true) 필요 — 무승인 집행 금지" }, { status: 400 });
  }

  // 2) mode 클램프 — 기본 paper(모의 우선). live 는 이중확인 문구가 있어야만 전달(없으면 paper 로 강등).
  let mode = String(body?.mode ?? "paper").toLowerCase();
  if (!["mock", "paper", "live"].includes(mode)) mode = "paper";
  const liveConfirm = String(body?.live_confirm ?? "");
  if (mode === "live" && liveConfirm !== LIVE_PHRASE) {
    return NextResponse.json({ ok: false, stage: "live_confirm",
      error: `live 집행은 이중확인 문구(${LIVE_PHRASE})가 필요합니다. 또한 서버에 KIS_LIVE_CONFIRM 이 설정돼야 합니다(§15).` }, { status: 400 });
  }

  // picks: [{bucket,ticker,asset_class}] → {bucket:[ticker,...]}. 개별주(stock)는 'individual'(carve 대상).
  const picksByBucket: Record<string, string[]> = {};
  for (const p of Array.isArray(body?.picks) ? body.picks : []) {
    if (!p?.bucket || !p?.ticker) continue;
    const b = String(p.asset_class ?? "") === "stock" ? "individual" : String(p.bucket);
    (picksByBucket[b] ||= []);
    if (!picksByBucket[b].includes(String(p.ticker))) picksByBucket[b].push(String(p.ticker));
  }
  if (Object.keys(picksByBucket).length === 0) {
    return NextResponse.json({ ok: false, error: "선택된 후보가 없습니다." }, { status: 400 });
  }
  const rounds = Math.max(1, Math.min(10, parseInt(String(body?.rounds ?? 3), 10) || 3));
  const period = Math.max(1, Math.min(120, parseInt(String(body?.period_days ?? 14), 10) || 14));
  const equityOption = ["none", "5", "10"].includes(String(body?.equity_option)) ? String(body.equity_option) : "none";

  const args = [
    "--account", String(id),
    "--picks", JSON.stringify(picksByBucket),
    "--mode", mode,
    "--approve",
    "--rounds", String(rounds),
    "--period", String(period),
    "--equity-option", equityOption,
  ];
  if (mode === "live") args.push("--i-understand-live", LIVE_PHRASE);

  try {
    const out = await runPy(args);
    return NextResponse.json(out, { status: out?.ok ? 200 : 200 });
  } catch (e: any) {
    return NextResponse.json({ ok: false, stage: "spawn", error: e?.message ?? "집행 실패" }, { status: 500 });
  }
}
