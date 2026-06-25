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

// 사용자(CEO) 투자 견해 = 1급 입력. 백엔드 user_views CLI 만 실행한다.
// 프론트는 브로커 API 를 직접 호출하지 않는다(웹=조회/입력 전용). 자동 적용 없음 — 저장만.
async function runViews(args: string[]) {
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, ["-m", "main_mission.portfolio_os.user_views", ...args], {
        cwd: root, timeout: 20000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 2 * 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "{}";
      return NextResponse.json(JSON.parse(line));
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      return NextResponse.json({ ok: false, error: "견해 처리 실패: " + (e?.message ?? "") }, { status: 500 });
    }
  }
  return NextResponse.json({ ok: false, error: "python 미발견" }, { status: 500 });
}

// 견해 목록 (계좌별, active). RBAC: requireAccountAccess(타계좌 403).
export async function GET(_req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, _req); if (ag) return ag;
  return runViews(["--account", String(id), "--list"]);
}

// 견해 추가 / 변경(supersede) / 보관(archive). 자동 적용 금지 — 저장만(allocation/policy draft 는 Agent3 게이트).
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;

  const body = await req.json().catch(() => ({} as any));
  const action = String(body.action ?? "add");
  const base = ["--account", String(id)];

  const optArgs = (): string[] => {
    const a: string[] = [];
    const push = (flag: string, v: unknown) => {
      if (v !== undefined && v !== null && String(v).trim() !== "") a.push(flag, String(v).trim());
    };
    push("--layer", body.layer);
    push("--theme", body.theme);
    push("--ticker", body.ticker);
    push("--etf", body.etf);
    push("--stance", body.stance);
    if (body.conviction !== undefined && body.conviction !== null && body.conviction !== "") {
      a.push("--conviction", String(body.conviction));
    }
    push("--horizon", body.horizon);
    push("--note", body.note);
    return a;
  };

  if (action === "archive") {
    const vid = parseInt(String(body.view_id ?? ""), 10);
    if (!Number.isInteger(vid)) return NextResponse.json({ ok: false, error: "view_id 필요" }, { status: 400 });
    return runViews([...base, "--archive", String(vid)]);
  }
  if (action === "update") {
    const vid = parseInt(String(body.view_id ?? ""), 10);
    if (!Number.isInteger(vid)) return NextResponse.json({ ok: false, error: "view_id 필요" }, { status: 400 });
    return runViews([...base, "--update", String(vid), ...optArgs()]);
  }
  // add (기본)
  if (!body.layer || String(body.layer).trim() === "") {
    return NextResponse.json({ ok: false, error: "layer(대전제/중전제/단기/장기)를 선택하세요" }, { status: 400 });
  }
  return runViews([...base, "--add", ...optArgs()]);
}
