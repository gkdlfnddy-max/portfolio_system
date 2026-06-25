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

// decision/route.ts 와 동일한 멀티 바이너리 + repo-root cwd 패턴.
async function runPy(args: string[]) {
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, ["-m", "main_mission.portfolio_os.theme_suggestions", ...args], {
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

// 관심 분야 AI 후보 제안 — neutral(자동반영 없음). GET=제안, POST=사용자 행동 기록.
export async function GET(_req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, _req);
  if (ag) return ag;
  try {
    const out = await runPy(["--account", String(id), "--suggest"]);
    return NextResponse.json(out, { status: out.ok ? 200 : 400 });
  } catch (e: any) {
    return NextResponse.json({ ok: false, error: e?.message ?? "후보 제안 실패" }, { status: 500 });
  }
}

// 사용자 행동 기록 — added_to_research / ignored / applied_to_draft / saved_to_policy / rejected.
// **[조사 후보로 추가]는 policy 직접 반영이 아니다** — applied_to_policy 는 saved_to_policy 일 때만.
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;
  const body = await req.json().catch(() => ({}));
  if (body.candidate_id == null || !body.user_action) {
    return NextResponse.json({ ok: false, error: "candidate_id, user_action 필요" }, { status: 400 });
  }
  try {
    const out = await runPy([
      "--record", "--account", String(id),
      "--candidate-id", String(body.candidate_id),
      "--user-action", String(body.user_action),
    ]);
    return NextResponse.json(out, { status: out.ok ? 200 : 400 });
  } catch (e: any) {
    return NextResponse.json({ ok: false, error: e?.message ?? "행동 기록 실패" }, { status: 500 });
  }
}
