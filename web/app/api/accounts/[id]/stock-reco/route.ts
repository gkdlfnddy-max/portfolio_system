import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";

import { requireAccountUnlocked } from "@/lib/auth/account";
import { requireAccountAccessAndUnlocked } from "@/lib/auth/rbac";

// 개별주 자동 추천(상위 N) — 백엔드 stock_reco CLI(정직: 가짜 티커/점수 금지, 자동적용 0).
// 조회 전용 — 추천만 반환한다. picks 반영은 사용자(CEO)가 화면에서 확인·추가해야 한다.
export const dynamic = "force-dynamic";
const pexec = promisify(execFile);

function accId(id: string): number | null {
  const n = parseInt(id, 10);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

async function runPy(mod: string, args: string[]) {
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(root, ".venv", "bin", "python"), "python", "python3"]) {
    try {
      const { stdout } = await pexec(py, ["-m", `main_mission.portfolio_os.${mod}`, ...args], {
        cwd: root, timeout: 30000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 4 * 1024 * 1024,
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

// 추천 조회. ?list=themes → 공통 테마 목록. theme/sector 지정 → 2계층(공통×계좌) 추천. 기본 → 개별주.
export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;

  const url = new URL(req.url);
  try {
    if (url.searchParams.get("list") === "themes") {
      const out = await runPy("instrument_master", ["--list-themes"]);
      return NextResponse.json({ ...out, readonly: true });
    }
    const n = Math.max(1, Math.min(30, parseInt(url.searchParams.get("n") ?? "10", 10) || 10));
    const theme = (url.searchParams.get("theme") ?? "").trim();
    const sector = (url.searchParams.get("sector") ?? "").trim();
    const bucket = (url.searchParams.get("bucket") ?? "").trim();
    const kind = ["stock", "etf", "all"].includes(url.searchParams.get("kind") ?? "") ? url.searchParams.get("kind")! : "all";
    const extra = (url.searchParams.get("extra") ?? "").trim();

    const args = ["--account", String(id), "--n", String(n), "--kind", kind];
    if (theme) args.push("--theme", theme);
    else if (sector) args.push("--sector", sector);
    else if (bucket) args.push("--bucket", bucket);
    else if (extra) args.push("--extra", extra);
    const out = await runPy("stock_reco", args);
    return NextResponse.json({ ...out, readonly: true });
  } catch (e: any) {
    return NextResponse.json({ ok: false, error: e?.message ?? "추천 실패" }, { status: 500 });
  }
}

// CEO 피드백(선택/삭제/수정/무시) 기록 — 계좌별 학습 입력. 주문 아님.
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;

  const body = await req.json().catch(() => ({} as any));
  const ticker = String(body?.ticker ?? "").trim();
  const action = String(body?.action ?? "").trim();
  if (!ticker || !["selected", "removed", "modified", "ignored"].includes(action)) {
    return NextResponse.json({ ok: false, error: "ticker + action(selected|removed|modified|ignored) 필요" }, { status: 400 });
  }
  const args = ["--account", String(id), "--feedback", action, "--ticker", ticker];
  if (body?.request_key) args.push("--theme", String(body.request_key));
  try {
    const out = await runPy("stock_reco", args);
    return NextResponse.json({ ...out, readonly: true });
  } catch (e: any) {
    return NextResponse.json({ ok: false, error: e?.message ?? "피드백 실패" }, { status: 500 });
  }
}
