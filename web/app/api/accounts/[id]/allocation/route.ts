import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";

import { requireAccountUnlocked, requireAccountReauth } from "@/lib/auth/account";
import { requireAccountAccessAndUnlocked, requireAccountAccessAndReauth } from "@/lib/auth/rbac";

const pexec = promisify(execFile);
export const dynamic = "force-dynamic";

function accId(id: string): number | null {
  const n = parseInt(id, 10);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

async function runPy(mod: string, args: string[]) {
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, ["-m", `main_mission.portfolio_os.${mod}`, ...args], {
        cwd: root, timeout: 30000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 4 * 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "{}";
      return JSON.parse(line);
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      throw e;
    }
  }
  throw new Error("python 미발견");
}

export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;
  try {
    return NextResponse.json(await runPy("selection", ["--account", String(id), "--options"]));
  } catch (e: any) {
    return NextResponse.json({ ok: false, error: e?.message ?? "조회 실패" }, { status: 500 });
  }
}

export async function POST(req: Request, { params }: { params: { id: string } }) {
  // select/generate(목표비중 확정·생성)은 민감 작업 — 재인증 필요.
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndReauth(id); if (az) return az;
  const ag = await requireAccountReauth(id, "rebalance", req);
  if (ag) return ag;
  const body = await req.json().catch(() => ({}));
  const action = body.action;
  try {
    if (action === "generate") return NextResponse.json(await runPy("allocation", ["--account", String(id), "--generate"]));
    if (action === "cancel") return NextResponse.json(await runPy("selection", ["--account", String(id), "--cancel"]));
    if (action === "select") {
      if (!body.proposal_id || !body.variant)
        return NextResponse.json({ ok: false, error: "proposal_id·variant 필요" }, { status: 400 });
      const out = await runPy("selection", ["--account", String(id), "--select", String(body.proposal_id), String(body.variant)]);
      return NextResponse.json(out, { status: out.ok ? 200 : 400 });
    }
    return NextResponse.json({ ok: false, error: "action: generate|select|cancel" }, { status: 400 });
  } catch (e: any) {
    return NextResponse.json({ ok: false, error: e?.message ?? "실패" }, { status: 500 });
  }
}
