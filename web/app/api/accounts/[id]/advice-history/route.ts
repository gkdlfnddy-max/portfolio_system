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

// portfolio_history CLI(--advice) 실행 — 운영 truth(DB)만. mock 없음.
// 계좌 격리: --account 인자가 그대로 advice_history(account_index) 로 들어가 계좌별 이력만 반환.
// 반환 advice: events(적용/수정/무시/저장 타임라인 + evidence/lesson 카운트) + counts(분류 통계).
async function runAdvice(id: number, limit: number) {
  const root = path.resolve(process.cwd(), "..");
  const args = [
    "-m", "main_mission.portfolio_os.portfolio_history",
    "--account", String(id), "--advice", "--limit", String(limit),
  ];
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, args, {
        cwd: root, timeout: 30000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "null";
      try { return NextResponse.json({ advice: JSON.parse(line) }); }
      catch { return NextResponse.json({ advice: null }); }
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      return NextResponse.json({ advice: null, error: "advice history 실패: " + (e?.message ?? "unknown") }, { status: 500 });
    }
  }
  return NextResponse.json({ advice: null, error: "python 미발견" }, { status: 500 });
}

// 조언 적용/무시 이력 조회. history-series 와 동일한 잠금 가드(홈 공개+계좌 PIN).
export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id); if (id === null) return NextResponse.json({ error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;

  const url = new URL(req.url);
  const rawLimit = parseInt(url.searchParams.get("limit") ?? "50", 10);
  const limit = Number.isInteger(rawLimit) && rawLimit >= 1 && rawLimit <= 500 ? rawLimit : 50;
  return runAdvice(id, limit);
}
