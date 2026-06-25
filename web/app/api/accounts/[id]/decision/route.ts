import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";
import { getLatestDecision } from "@/lib/server/portfolioDb";
import { requireAccountUnlocked, requireAccountReauth } from "@/lib/auth/account";
import { requireAccountAccessAndUnlocked, requireAccountAccessAndReauth } from "@/lib/auth/rbac";

const pexec = promisify(execFile);
export const dynamic = "force-dynamic";

function accId(id: string): number | null {
  const n = parseInt(id, 10);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

// 조회: 최신 의사결정 스냅샷(DB).
export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ error: "invalid id" }, { status: 400 });
  // 정규 순서: 로그인(401) → 계좌 RBAC(403). (PIN 제거 — unlock 가드는 no-op)
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;
  return NextResponse.json({ decision: getLatestDecision(id) });
}

// 생성(의사결정 계산)은 민감 작업 — 재인증 필요. 백엔드 decision.py 가 실잔고+유니버스로 계산해 DB 저장.
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  // 정규 순서(민감): 로그인(401) → RBAC(403). (PIN/재인증 제거 — 가드는 no-op. live 하드락은 별개)
  const az = await requireAccountAccessAndReauth(id); if (az) return az;
  const ag = await requireAccountReauth(id, "order_approval", req);
  if (ag) return ag;
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, ["-m", "main_mission.portfolio_os.decision", "--account", String(id)], {
        cwd: root, timeout: 25000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "{}";
      const out = JSON.parse(line);
      return NextResponse.json(out, { status: out.ok ? 200 : 400 });
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      return NextResponse.json({ ok: false, error: "계산 실패: " + (e?.message ?? "unknown") }, { status: 500 });
    }
  }
  return NextResponse.json({ ok: false, error: "python 미발견" }, { status: 500 });
}
