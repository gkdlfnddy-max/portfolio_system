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

// 조회: 유연 투자기준(effective 정책 + 출처 + hard rule). 백엔드 policy_rules CLI 가 진리.
// 정책이 아직 없으면 정직한 빈 상태({ policy: null }) — 가짜 숫자 금지.
export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;

  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(
        py,
        ["-m", "main_mission.portfolio_os.policy_rules", "--account", String(id)],
        { cwd: root, timeout: 25000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 1024 * 1024 },
      );
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop();
      if (!line) return NextResponse.json({ policy: null }); // CLI 가 아직 출력 없음 → 빈 상태
      try {
        const out = JSON.parse(line);
        // CLI 는 {ok, effective_policy:{...}} 로 감싸므로 effective_policy 를 언랩(UI 는 flat 소비).
        const policy = out?.effective_policy ?? (out?.policy_type !== undefined ? out : null);
        return NextResponse.json({ policy });
      } catch {
        // 출력이 JSON 이 아님 — 가짜로 채우지 않고 정직히 비움.
        return NextResponse.json({ policy: null, error: "정책 JSON 파싱 실패" });
      }
    } catch (e: any) {
      if (e?.code === "ENOENT") continue; // 이 python 바이너리 없음 → 다음 후보
      return NextResponse.json({ policy: null, error: "정책 조회 실패: " + (e?.message ?? "unknown") });
    }
  }
  return NextResponse.json({ policy: null, error: "python 미발견" });
}
