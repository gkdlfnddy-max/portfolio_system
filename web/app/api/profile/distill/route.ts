import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";

import { requireUnlocked } from "@/lib/auth/guard";

const pexec = promisify(execFile);
export const dynamic = "force-dynamic";

// 컨셉(자유 입력) → 대전제 1차 정리 (규칙 기반, 계좌 무관). 저장 아님 — 제안만.
export async function POST(req: Request) {
  // 자유 텍스트 정리(저장 아님·계좌 무관). 페이지 진입은 LoginGate 가 보호.
  const body = await req.json().catch(() => ({}));
  const text = String(body.text ?? "").trim();
  if (!text) return NextResponse.json({ ok: false, error: "text 필요" }, { status: 400 });
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, ["-m", "main_mission.portfolio_os.profile", "--distill", text], {
        cwd: root, timeout: 15000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "{}";
      return NextResponse.json(JSON.parse(line));
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      return NextResponse.json({ ok: false, error: "정리 실패: " + (e?.message ?? "") }, { status: 500 });
    }
  }
  return NextResponse.json({ ok: false, error: "python 미발견" }, { status: 500 });
}
