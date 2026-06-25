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

// Claude 분석 전문가 상담: 질문 → 입력 방법·권장값·메모리 근거 + 적용 제안.
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;
  const body = await req.json().catch(() => ({}));
  const question = String(body.question ?? "").trim();
  if (!question) return NextResponse.json({ ok: false, error: "질문을 입력하세요" }, { status: 400 });
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, ["-m", "main_mission.portfolio_os.consult", "--account", String(id), "--ask", question], {
        cwd: root, timeout: 20000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 2 * 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "{}";
      return NextResponse.json(JSON.parse(line));
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      return NextResponse.json({ ok: false, error: "상담 실패: " + (e?.message ?? "") }, { status: 500 });
    }
  }
  return NextResponse.json({ ok: false, error: "python 미발견" }, { status: 500 });
}
