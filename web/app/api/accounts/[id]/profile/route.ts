import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";
import { getProfile } from "@/lib/server/portfolioDb";
import { requireAccountUnlocked, requireAccountReauth } from "@/lib/auth/account";
import { requireAccountAccessAndUnlocked, requireAccountAccessAndReauth } from "@/lib/auth/rbac";

const pexec = promisify(execFile);
export const dynamic = "force-dynamic";

function accId(id: string): number | null {
  const n = parseInt(id, 10);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;
  return NextResponse.json({ profile: getProfile(id) });
}

// 저장(전략 저장)은 민감 작업 — 재인증 필요. 백엔드 Python(profile.py)이 DB에 기록.
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndReauth(id); if (az) return az;
  const ag = await requireAccountReauth(id, "strategy", req);
  if (ag) return ag;
  const body = await req.json().catch(() => ({}));
  const payload = JSON.stringify({
    posture_text: body.posture_text ?? "",
    risk_tolerance: body.risk_tolerance ?? "",
    short_policy: body.short_policy ?? "",
    cash_min_pct: body.cash_min_pct ?? "",
    cash_max_pct: body.cash_max_pct ?? "",
    horizon: body.horizon ?? "",
    interests_text: body.interests_text ?? "",
    views_text: body.views_text ?? "",
    individual_cap_pct: body.individual_cap_pct ?? "",
    individual_count: body.individual_count ?? "",
    region_pref: body.region_pref ?? "",
    rebalance_pace: body.rebalance_pace ?? "",
    bond_target_pct: body.bond_target_pct ?? "",      // 방어자산 대비 국채 비율(%) — 누락되어 저장 안 되던 버그 수정
    bond_duration_pref: body.bond_duration_pref ?? "", // 채권 듀레이션(단기/중기/장기)
    doc: body.doc ?? null,
    refined_by: body.refined_by ?? "user",
  });
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(
        py,
        ["-m", "main_mission.portfolio_os.profile", "--account", String(id), "--json", payload],
        { cwd: root, timeout: 15000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 1024 * 1024 },
      );
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "{}";
      const out = JSON.parse(line);
      return NextResponse.json(out, { status: out.ok ? 200 : 400 });
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      return NextResponse.json({ ok: false, error: "저장 실패: " + (e?.message ?? "unknown") }, { status: 500 });
    }
  }
  return NextResponse.json({ ok: false, error: "python 미발견" }, { status: 500 });
}
