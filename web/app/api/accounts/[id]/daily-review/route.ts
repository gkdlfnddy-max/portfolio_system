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

// daily_review CLI 실행 (venv python 우선). 운영 truth(DB)만 사용 — mock 없음.
async function run(id: number, generate: boolean) {
  const root = path.resolve(process.cwd(), "..");
  const args = ["-m", "main_mission.portfolio_os.daily_review", "--account", String(id)];
  if (!generate) args.push("--show");
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, args, {
        cwd: root, timeout: 30000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "null";
      try { return NextResponse.json({ review: JSON.parse(line) }); }
      catch { return NextResponse.json({ review: null }); }
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      return NextResponse.json({ review: null, error: "daily review 실패: " + (e?.message ?? "unknown") }, { status: 500 });
    }
  }
  return NextResponse.json({ review: null, error: "python 미발견" }, { status: 500 });
}

// 최신 리뷰 조회 (관망도 정상 결과로 표시).
export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id); if (id === null) return NextResponse.json({ error: "invalid id" }, { status: 400 });
  const az = await requireLoginAndAccount(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;
  return run(id, false);
}

// 오늘 점검 실행 (selected allocation+drift 에서만 예약 후보 생성 — 모듈이 강제).
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id); if (id === null) return NextResponse.json({ error: "invalid id" }, { status: 400 });
  const az = await requireLoginAndAccount(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;
  return run(id, true);
}
