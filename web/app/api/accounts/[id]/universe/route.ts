import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";
import { getUniverse } from "@/lib/server/portfolioDb";
import { requireAccountUnlocked } from "@/lib/auth/account";
import { requireAccountAccessAndUnlocked } from "@/lib/auth/rbac";

const pexec = promisify(execFile);
export const dynamic = "force-dynamic";

const ROOT = path.resolve(process.cwd(), "..");

// 유니버스 변경은 백엔드 Python(KIS 검증 + DB 쓰기)으로만. 웹은 트리거 + DB 조회.
async function runUniverse(args: string[]): Promise<any> {
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, ["-m", "main_mission.portfolio_os.universe", ...args], {
        cwd: ROOT,
        timeout: 25000,
        env: { ...process.env, PYTHONIOENCODING: "utf-8" },
        maxBuffer: 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "{}";
      return JSON.parse(line);
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      return { ok: false, error: "실행 실패: " + (e?.message ?? "unknown") };
    }
  }
  return { ok: false, error: "python 미발견" };
}

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
  return NextResponse.json({ instruments: getUniverse(id) });
}

export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;
  const body = await req.json().catch(() => ({}));
  const ticker = String(body.ticker ?? "").trim();
  if (!/^\d{6}$/.test(ticker)) {
    return NextResponse.json({ ok: false, error: "종목코드는 6자리 숫자입니다 (국내주식)" }, { status: 400 });
  }
  const out = await runUniverse(["--account", String(id), "--add", ticker]);
  return NextResponse.json(out, { status: out.ok ? 200 : 400 });
}

export async function PATCH(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;
  const body = await req.json().catch(() => ({}));
  const ticker = String(body.ticker ?? "").trim();
  const weight = Number(body.weight);
  if (!ticker || !Number.isFinite(weight)) {
    return NextResponse.json({ ok: false, error: "ticker · weight 필요" }, { status: 400 });
  }
  const out = await runUniverse(["--account", String(id), "--set-weight", ticker, String(weight)]);
  return NextResponse.json(out, { status: out.ok ? 200 : 400 });
}

export async function DELETE(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;
  const body = await req.json().catch(() => ({}));
  const ticker = String(body.ticker ?? "").trim();
  if (!ticker) return NextResponse.json({ ok: false, error: "ticker 필요" }, { status: 400 });
  const out = await runUniverse(["--account", String(id), "--remove", ticker]);
  return NextResponse.json(out, { status: out.ok ? 200 : 400 });
}
