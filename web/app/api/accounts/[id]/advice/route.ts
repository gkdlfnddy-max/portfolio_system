import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";

import { requireAccountUnlocked } from "@/lib/auth/account";
import { requireAccountAccessAndUnlocked } from "@/lib/auth/rbac";

const pexec = promisify(execFile);
export const dynamic = "force-dynamic";

function accId(id: string): number | null {
  const n = parseInt(id, 10);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

async function runPy(args: string[]) {
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, ["-m", "main_mission.portfolio_os.advice", ...args], {
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

// 조언 생성 (컨셉 → 개선 제안). 출처: rule | benchmark | lesson(메모리).
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;
  const body = await req.json().catch(() => ({}));
  const concept = String(body.concept ?? "").trim();
  if (!concept) return NextResponse.json({ ok: false, error: "concept 필요" }, { status: 400 });
  try {
    return NextResponse.json(await runPy(["--account", String(id), "--generate", concept]));
  } catch (e: any) {
    return NextResponse.json({ ok: false, error: e?.message ?? "조언 생성 실패" }, { status: 500 });
  }
}

// 반영/보류 결정 저장.
export async function PATCH(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;
  const body = await req.json().catch(() => ({}));
  if (body.advice_id == null) return NextResponse.json({ ok: false, error: "advice_id 필요" }, { status: 400 });
  try {
    return NextResponse.json(await runPy(["--account", String(id), "--decide", String(body.advice_id), body.accept ? "accept" : "reject"]));
  } catch (e: any) {
    return NextResponse.json({ ok: false, error: e?.message ?? "결정 저장 실패" }, { status: 500 });
  }
}
