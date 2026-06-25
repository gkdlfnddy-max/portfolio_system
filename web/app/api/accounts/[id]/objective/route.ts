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

// 투자 목적/성향 = "최선"의 기준을 정하는 토대. 백엔드 investor_objective CLI 만 실행한다.
// 웹=조회/입력 전용. 자동 적용 없음 — 저장만(allocation/관점별 후보가 읽어 씀).
async function runObjective(args: string[]) {
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, ["-m", "main_mission.portfolio_os.investor_objective", ...args], {
        cwd: root, timeout: 20000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 2 * 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "{}";
      return NextResponse.json(JSON.parse(line));
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      return NextResponse.json({ ok: false, error: "목적 처리 실패: " + (e?.message ?? "") }, { status: 500 });
    }
  }
  return NextResponse.json({ ok: false, error: "python 미발견" }, { status: 500 });
}

// 목적/성향 + "최선 기준" 조회 (계좌별). RBAC: requireAccountAccess(타계좌 403).
export async function GET(_req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, _req); if (ag) return ag;
  // criteria 가 목적 + 산출된 "최선 기준"을 함께 담아 반환한다(미설정이면 정직하게 표시).
  return runObjective(["--account", String(id), "--criteria"]);
}

// 목적/성향 저장. 자동 적용 금지 — 저장만.
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;

  const body = await req.json().catch(() => ({} as any));
  // 구조화 payload 만 통과(백엔드가 검증/정규화). JSON 인자로 전달.
  const payload = {
    investment_goal: body.investment_goal,
    horizon: body.horizon,
    risk_tolerance: body.risk_tolerance,
    loss_aversion: body.loss_aversion,
    prefers: body.prefers,
    allows: body.allows,
    region_pref: body.region_pref,
    market_view: body.market_view,
    note: body.note,
  };
  return runObjective(["--account", String(id), "--set", "--json", JSON.stringify(payload)]);
}
