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

async function runPy(args: string[]) {
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(root, ".venv", "bin", "python"), "python", "python3"]) {
    try {
      const { stdout } = await pexec(py, ["-m", "main_mission.portfolio_os.stock_reco", ...args], {
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

export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;

  const url = new URL(req.url);
  const n = Math.max(1, Math.min(30, parseInt(url.searchParams.get("n") ?? "10", 10) || 10));
  const extra = (url.searchParams.get("extra") ?? "").trim();

  try {
    const args = ["--account", String(id), "--n", String(n)];
    if (extra) args.push("--extra", extra);
    const out = await runPy(args);
    return NextResponse.json({ ...out, readonly: true });
  } catch (e: any) {
    return NextResponse.json({ ok: false, error: e?.message ?? "추천 실패" }, { status: 500 });
  }
}
