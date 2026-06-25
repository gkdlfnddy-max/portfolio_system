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

// portfolio_history CLI(--series) 실행 — 운영 truth(DB)만. mock 없음.
// 계좌 격리: --account 인자가 그대로 series(account_index) 로 들어가 계좌별 시계열만 반환.
// 반환 series 에는 총자산/현금/자산군 비중 + exposure_series(net/gross/hedge/theme)
//   + exposure(현재 확정 안) + drift_series(daily_portfolio_reviews) 가 포함된다(전부 DB 계산).
async function runSeries(id: number, days: number) {
  const root = path.resolve(process.cwd(), "..");
  const args = [
    "-m", "main_mission.portfolio_os.portfolio_history",
    "--account", String(id), "--series", "--days", String(days),
  ];
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, args, {
        cwd: root, timeout: 30000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "null";
      try { return NextResponse.json({ series: JSON.parse(line) }); }
      catch { return NextResponse.json({ series: null }); }
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      return NextResponse.json({ series: null, error: "history series 실패: " + (e?.message ?? "unknown") }, { status: 500 });
    }
  }
  return NextResponse.json({ series: null, error: "python 미발견" }, { status: 500 });
}

// 일별 추이 시계열 조회(총자산 / 자산군 비중 / 종목별). 다른 계좌 라우트와 동일한 잠금 가드.
export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id); if (id === null) return NextResponse.json({ error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;

  const url = new URL(req.url);
  const rawDays = parseInt(url.searchParams.get("days") ?? "30", 10);
  const days = Number.isInteger(rawDays) && rawDays >= 1 && rawDays <= 365 ? rawDays : 30;
  return runSeries(id, days);
}
