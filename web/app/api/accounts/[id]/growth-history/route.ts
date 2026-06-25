import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";
import { requireUnlocked } from "@/lib/auth/guard";
import { requireAccountUnlocked } from "@/lib/auth/account";
import { requireLoginAndAccount } from "@/lib/auth/rbac";

const pexec = promisify(execFile);
export const dynamic = "force-dynamic";

function accId(id: string): number | null {
  const n = parseInt(id, 10);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

// portfolio_history CLI(--growth) 실행 — 운영 truth(DB)만. mock 없음.
// 계좌 격리: --account 인자가 그대로 growth_history(account_index) 로 들어가
//   evidence/lesson 후보는 계좌별로만 조회되고, promoted lesson 은 **익명화된 공통(개인/계좌 0)** 만 노출된다.
// 반환 growth: evidence(stance/freshness/confidence) + lesson_candidates + promoted_lessons(익명) + regression.
async function runGrowth(id: number, limit: number) {
  const root = path.resolve(process.cwd(), "..");
  const args = [
    "-m", "main_mission.portfolio_os.portfolio_history",
    "--account", String(id), "--growth", "--limit", String(limit),
  ];
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, args, {
        cwd: root, timeout: 30000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "null";
      try { return NextResponse.json({ growth: JSON.parse(line) }); }
      catch { return NextResponse.json({ growth: null }); }
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      return NextResponse.json({ growth: null, error: "growth history 실패: " + (e?.message ?? "unknown") }, { status: 500 });
    }
  }
  return NextResponse.json({ growth: null, error: "python 미발견" }, { status: 500 });
}

// 성장(evidence/lesson/regression) 이력 조회. history-series 와 동일한 잠금 가드.
//   requireLoginAndAccount(id) 필수 — 타계좌 접근은 여기서 403(FORBIDDEN).
export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id); if (id === null) return NextResponse.json({ error: "invalid id" }, { status: 400 });
  const az = await requireLoginAndAccount(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;

  const url = new URL(req.url);
  const rawLimit = parseInt(url.searchParams.get("limit") ?? "20", 10);
  const limit = Number.isInteger(rawLimit) && rawLimit >= 1 && rawLimit <= 100 ? rawLimit : 20;
  return runGrowth(id, limit);
}
