import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";

import { requireAccountUnlocked } from "@/lib/auth/account";
import { requireAccountAccessAndUnlocked } from "@/lib/auth/rbac";

// 선택 종목의 일봉(가격) 데이터 적재 — 분할 지정가 계산의 전제. read-only(주문 0).
// 백엔드 price_history --fetch-daily(KIS 일봉) 호출. KRX 만 성공, 해외(미구현)는 graceful 미연동.
export const dynamic = "force-dynamic";
const pexec = promisify(execFile);

function accId(id: string): number | null {
  const n = parseInt(id, 10);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

async function runPy(args: string[]) {
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(root, ".venv", "bin", "python"), "python", "python3"]) {
    try {
      const { stdout } = await pexec(py, ["-m", "main_mission.portfolio_os.price_history", ...args], {
        cwd: root, timeout: 120000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 4 * 1024 * 1024,
      });
      const text = stdout.trim();
      try { return JSON.parse(text); } catch {
        const s = text.indexOf("{"), e = text.lastIndexOf("}");
        if (s >= 0 && e > s) return JSON.parse(text.slice(s, e + 1));
        throw new Error("CLI JSON 파싱 실패");
      }
    } catch (e: any) {
      if (e?.code === "ENOENT" && !e?.stderr) continue;
      throw e;
    }
  }
  throw new Error("python 미발견");
}

// 선택 종목 일봉 적재(KRX). live 계좌라도 일봉 조회는 read-only(주문 아님).
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;

  const body = await req.json().catch(() => ({} as any));
  const raw = Array.isArray(body?.tickers) ? body.tickers : [];
  // 정규화: 문자열·중복 제거·길이 제한(과도한 호출 방지).
  const tickers = Array.from(new Set(raw.map((t: any) => String(t ?? "").trim()).filter(Boolean))).slice(0, 40);
  if (tickers.length === 0) {
    return NextResponse.json({ ok: false, error: "적재할 종목이 없습니다." }, { status: 400 });
  }
  const count = Math.max(60, Math.min(300, parseInt(String(body?.count ?? 120), 10) || 120));

  try {
    const out = await runPy([
      "--fetch-daily", "--account", String(id),
      "--codes", tickers.join(","), "--count", String(count),
    ]);
    return NextResponse.json({ ...out, readonly: true });
  } catch (e: any) {
    return NextResponse.json({ ok: false, error: e?.message ?? "가격 적재 실패" }, { status: 500 });
  }
}
