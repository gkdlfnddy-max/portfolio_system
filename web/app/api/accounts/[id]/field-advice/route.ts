import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";

import { requireAccountAccessAndUnlocked } from "@/lib/auth/rbac";

const pexec = promisify(execFile);
export const dynamic = "force-dynamic";

function accId(id: string): number | null {
  const n = parseInt(id, 10);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

// decision/route.ts 와 동일한 멀티 바이너리 + repo-root cwd 패턴.
async function runPy(args: string[]) {
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, ["-m", "main_mission.portfolio_os.field_advisors", ...args], {
        cwd: root, timeout: 30000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 4 * 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "{}";
      return JSON.parse(line);
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      throw e;
    }
  }
  throw new Error("python 미발견");
}

// 필드별 AI 조언 생성 / 사용자 행동 기록. 조언은 임시 제안일 뿐 — 정책은 저장 시에만 바뀐다.
export async function POST(req: Request, { params }: { params: { id: string } }) {
  // 앱 PIN 가드(계좌별 가드는 다른 에이전트 담당 — 여기선 추가하지 않음).
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const body = await req.json().catch(() => ({}));

  try {
    // 모드 1: 사용자 행동 기록 (field_advice_events).
    if (body.action === "record") {
      if (body.consultation_id == null || !body.user_action) {
        return NextResponse.json({ ok: false, error: "consultation_id, user_action 필요" }, { status: 400 });
      }
      const args = [
        "--record", "--account", String(id),
        "--consultation-id", String(body.consultation_id),
        "--user-action", String(body.user_action),
        "--field", String(body.field ?? ""),
      ];
      if (body.detail != null) args.push("--detail", String(body.detail));
      return NextResponse.json(await runPy(args));
    }

    // 모드 2: 조언 생성.
    const field = String(body.field ?? "").trim();
    if (!field) return NextResponse.json({ ok: false, error: "field 필요" }, { status: 400 });
    const args = ["--account", String(id), "--field", field];
    if (body.advice_type) args.push("--advice-type", String(body.advice_type));
    if (field === "whole") {
      args.push("--interests", String(body.interests ?? ""), "--views", String(body.views ?? ""));
    } else {
      args.push("--text", String(body.text ?? ""));
    }
    const out = await runPy(args);
    return NextResponse.json(out, { status: out.ok ? 200 : 400 });
  } catch (e: any) {
    return NextResponse.json({ ok: false, error: e?.message ?? "필드 조언 실패" }, { status: 500 });
  }
}
