import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";

import { requireAccountUnlocked } from "@/lib/auth/account";
import { requireAccountAccessAndUnlocked } from "@/lib/auth/rbac";

// 분할 진입 자동 생성 — 사용자는 **분할 횟수(+기간)만** 입력하면 시스템이 저점 지정가 사다리 draft 를 만든다.
// 백엔드 exec_plan.build_split_plan 호출(주문 없음·초안). 자동 주문/적용 0.
export const dynamic = "force-dynamic";
const pexec = promisify(execFile);

function accId(id: string): number | null {
  const n = parseInt(id, 10);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

async function runPy(args: string[]) {
  const root = path.resolve(process.cwd(), "..");
  let lastErr: any = null;
  for (const py of [path.resolve(root, ".venv", "bin", "python"), "python", "python3"]) {
    try {
      const { stdout } = await pexec(py, ["-m", "main_mission.portfolio_os.exec_plan", ...args], {
        cwd: root, timeout: 30000,
        env: { ...process.env, PYTHONIOENCODING: "utf-8" },
        maxBuffer: 4 * 1024 * 1024,
      });
      const text = stdout.trim();
      try { return JSON.parse(text); } catch {
        const s = text.indexOf("{"), e = text.lastIndexOf("}");
        if (s >= 0 && e > s) return JSON.parse(text.slice(s, e + 1));
        throw new Error("CLI JSON 파싱 실패");
      }
    } catch (e: any) {
      if (e?.code === "ENOENT" && !e?.stderr) { lastErr = e; continue; }
      throw e;
    }
  }
  throw lastErr ?? new Error("python 미발견");
}

export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });

  const az = await requireAccountAccessAndUnlocked(id);
  if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;

  let body: any = {};
  try { body = await req.json(); } catch { body = {}; }

  const rounds = Math.max(1, Math.min(10, parseInt(String(body?.rounds ?? 3), 10) || 3));
  const period = Math.max(1, Math.min(120, parseInt(String(body?.period_days ?? 14), 10) || 14));
  const equityOption = ["none", "5", "10"].includes(String(body?.equity_option)) ? String(body.equity_option) : "none";
  // 예수금은 백엔드가 최신 스냅샷에서 자동 조회(사용자는 횟수만 입력). 명시값 있으면 우선.
  const cash = Number(body?.cash_krw);
  // picks: [{bucket, ticker, asset_class}] → {bucket: [ticker,...]}. 개별주(stock)는 'individual' 키(carve 대상).
  const picksByBucket: Record<string, string[]> = {};
  for (const p of Array.isArray(body?.picks) ? body.picks : []) {
    if (!p?.bucket || !p?.ticker) continue;
    const b = String(p.asset_class ?? "") === "stock" ? "individual" : String(p.bucket);
    (picksByBucket[b] ||= []);
    if (!picksByBucket[b].includes(String(p.ticker))) picksByBucket[b].push(String(p.ticker));
  }
  if (Object.keys(picksByBucket).length === 0) {
    return NextResponse.json({ ok: false, error: "선택된 후보가 없습니다 — 종목을 먼저 고르세요." }, { status: 400 });
  }

  try {
    const args = [
      "--account", String(id),
      "--picks", JSON.stringify(picksByBucket),
      "--rounds", String(rounds),
      "--period", String(period),
      "--equity-option", equityOption,
    ];
    if (Number.isFinite(cash) && cash > 0) args.push("--cash", String(Math.round(cash)));
    const plan = await runPy(args);
    return NextResponse.json({ ...plan, readonly: true, auto_order: false });
  } catch (e: any) {
    return NextResponse.json({ ok: false, error: e?.message ?? "분할 plan 생성 실패" }, { status: 500 });
  }
}
