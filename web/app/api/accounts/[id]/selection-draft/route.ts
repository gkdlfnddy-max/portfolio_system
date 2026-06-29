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

// 세부 선정 위저드의 작업중 draft 저장/복원 — 백엔드 selection_draft CLI 만 실행한다.
// ⚠️ draft 저장 전용: policy(목표비중)·주문에 반영하지 않는다(웹=조회/입력 전용, 자동 적용 0).
async function runDraft(args: string[]) {
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(root, ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, ["-m", "main_mission.portfolio_os.selection_draft", ...args], {
        cwd: root, timeout: 20000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 2 * 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "{}";
      return NextResponse.json(JSON.parse(line));
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      return NextResponse.json({ ok: false, error: "draft 처리 실패: " + (e?.message ?? "") }, { status: 500 });
    }
  }
  return NextResponse.json({ ok: false, error: "python 미발견" }, { status: 500 });
}

// 저장된 선정 draft 복원(계좌별). RBAC: 로그인(401) → 접근권한(403) → 앱 잠금(401).
export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;
  return runDraft(["--account", String(id), "--load"]);
}

// 선정 draft 자동 저장(고른 종목·개별주 carve·초안 승인 표시). 저장만 — 자동 적용 금지.
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;

  const body = await req.json().catch(() => ({} as any));
  const payload = {
    picks: Array.isArray(body.picks) ? body.picks : [],
    equity_option: String(body.equity_option ?? "none"),
    acknowledged: !!body.acknowledged,
    proposal_id: body.proposal_id ? String(body.proposal_id) : null,
  };
  // 복잡한 JSON(picks 배열)은 base64 로 인코딩해 CLI 인자로 안전 전달(shell 미경유).
  const b64 = Buffer.from(JSON.stringify(payload), "utf-8").toString("base64");
  return runDraft(["--account", String(id), "--save", "--payload-b64", b64]);
}
