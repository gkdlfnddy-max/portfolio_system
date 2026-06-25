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

// 조회: 변이별(보수/기준/공격) 전략 요약. 백엔드 allocation_explain CLI 가 진리(규칙+실측 allocation).
// Anthropic/LLM API 미사용 · mock 숫자 금지 — 데이터 없으면 정직한 빈 상태({ explain: null }).
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
        ["-m", "main_mission.portfolio_os.allocation_explain", "--account", String(id)],
        { cwd: root, timeout: 25000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 1024 * 1024 },
      );
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop();
      if (!line) return NextResponse.json({ explain: null }); // CLI 출력 없음 → 빈 상태
      try {
        const out = JSON.parse(line);
        // CLI 는 {ok, options, ...} 로 감싸므로 언랩(UI 는 options 를 직접 소비).
        if (out?.ok === false) return NextResponse.json({ explain: null, error: out?.error ?? "설명 생성 실패" });
        const explain = out?.options !== undefined ? out : null;
        return NextResponse.json({ explain });
      } catch {
        return NextResponse.json({ explain: null, error: "설명 JSON 파싱 실패" });
      }
    } catch (e: any) {
      if (e?.code === "ENOENT") continue; // 이 python 바이너리 없음 → 다음 후보
      return NextResponse.json({ explain: null, error: "설명 조회 실패: " + (e?.message ?? "unknown") });
    }
  }
  return NextResponse.json({ explain: null, error: "python 미발견" });
}
