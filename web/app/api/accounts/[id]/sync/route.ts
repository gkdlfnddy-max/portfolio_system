import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";

import { requireAccountReauth } from "@/lib/auth/account";
import { requireAccountAccessAndReauth } from "@/lib/auth/rbac";
import { runGuarded, guardKey, detectRateLimit, withAccountLock } from "@/lib/server/kisGuard";

const pexec = promisify(execFile);

export const dynamic = "force-dynamic";

// sync job(Python) 1회 실행 → 마지막 JSON 라인 파싱. KIS 호출은 여기서만 발생.
async function runSyncJob(id: number): Promise<{ job: any }> {
  const root = path.resolve(process.cwd(), "..");
  const args = ["-m", "main_mission.portfolio_os.broker.sync_job", "--account", String(id)];

  let lastErr: any = null;
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, args, {
        cwd: root,
        timeout: 30000,
        env: { ...process.env, PYTHONIOENCODING: "utf-8" },
        maxBuffer: 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "{}";
      let job: any = {};
      try { job = JSON.parse(line); } catch { /* ignore */ }
      return { job };
    } catch (e: any) {
      if (e?.code === "ENOENT") { lastErr = e; continue; }
      throw e; // 실제 실행 실패 → 상위에서 rate-limit 여부 판정
    }
  }
  const err: any = new Error("python 실행 파일 미발견 (PATH)");
  err.__noPython = true;
  err.__cause = lastErr;
  throw err;
}

// 동기화 트리거(KIS 호출) — 민감 작업이므로 재인증 필요.
// 백엔드 sync job(Python)을 실행해 KIS→DB 저장만 시킨다.
// 구조적 완화: single-flight + short TTL 캐시(kisGuard)로 동시/연속 KIS 호출을 줄인다.
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = parseInt(params.id, 10);
  if (!Number.isInteger(id) || id < 1) {
    return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  }
  const az = await requireAccountAccessAndReauth(id); if (az) return az;
  const ag = await requireAccountReauth(id, "rebalance", req);
  if (ag) return ag;

  try {
    // 같은 계좌의 KIS 작업(sync/test)을 직렬화 + sync 결과는 single-flight/TTL 캐시.
    const { value, source } = await withAccountLock(id, () =>
      runGuarded(guardKey(id, "sync"), () => runSyncJob(id)),
    );
    // job 결과 안에 rate-limit 신호가 섞여 있어도(부분 실패) 호출부가 대기 처리하도록 알림.
    const rl = detectRateLimit(value.job);
    return NextResponse.json({ ok: true, job: value.job, cached: source !== "fresh", rateLimited: rl });
  } catch (e: any) {
    if (e?.__noPython) {
      return NextResponse.json({ ok: false, error: "python 실행 파일 미발견 (PATH)" }, { status: 500 });
    }
    // EGW00201 / 초당 거래건수 초과 / HTTP 500(rate) → 실패가 아닌 "재시도 대기" 로 표준화.
    if (detectRateLimit(e?.message) || detectRateLimit(e?.stderr) || detectRateLimit(e?.stdout)) {
      return NextResponse.json(
        { ok: false, rateLimited: true, retryAfterMs: 5000, error: "요청이 많아 잠시 후 자동 재시도합니다." },
        { status: 429 },
      );
    }
    return NextResponse.json(
      { ok: false, error: "sync job 실패: " + (e?.message ?? "unknown") },
      { status: 500 },
    );
  }
}
