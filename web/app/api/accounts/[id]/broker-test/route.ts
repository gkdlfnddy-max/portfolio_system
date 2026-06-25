import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";

import { requireAccountReauth } from "@/lib/auth/account";
import { requireAccountAccessAndReauth } from "@/lib/auth/rbac";
import { runGuarded, guardKey, detectRateLimit, withAccountLock } from "@/lib/server/kisGuard";

const pexec = promisify(execFile);

export const dynamic = "force-dynamic";

// conn_test CLI(Python) 1회 실행 → 마지막 JSON 라인 파싱. KIS 호출은 여기서만 발생.
async function runConnTest(id: number): Promise<{ test: any }> {
  const root = path.resolve(process.cwd(), "..");
  const args = ["-m", "main_mission.portfolio_os.broker.conn_test", "--account", String(id)];

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
      let test: any = {};
      try { test = JSON.parse(line); } catch { /* ignore */ }
      return { test };
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

// Broker 연결 테스트(stage별) — KIS/키움 공통. 실 broker 를 호출하므로 민감 작업(재인증 필요).
// Python conn_test CLI 를 스폰해 stage 결과(JSON)만 받는다. 비밀값은 결과에 없음(CLI 가 마스킹).
// 주문은 호출하지 않음(주문 2차 보류). 키 미설정이면 CLI 가 credential stage 에서 정직 실패.
// 구조적 완화: sync 와 같은 kisGuard(single-flight + TTL)로 같은 계좌 KIS 호출을 줄인다.
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = parseInt(params.id, 10);
  if (!Number.isInteger(id) || id < 1) {
    return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  }
  // 1) 로그인 + 계좌 RBAC + 앱 재인증창
  const az = await requireAccountAccessAndReauth(id);
  if (az) return az;
  // 2) 계좌별 PIN (rebalance 요구 플래그 — 연결/조회를 트리거하는 민감 작업)
  const ag = await requireAccountReauth(id, "rebalance", req);
  if (ag) return ag;

  try {
    // 같은 계좌의 KIS 작업(sync/test)을 직렬화: 자동 sync 진행 중이면 그 뒤에 순차 실행되어
    // 동시에 KIS 를 때리지 않는다. test 결과 자체는 op 별 single-flight/TTL 캐시.
    const { value, source } = await withAccountLock(id, () =>
      runGuarded(guardKey(id, "broker-test"), () => runConnTest(id)),
    );
    const rl = detectRateLimit(value.test);
    return NextResponse.json({ ok: true, test: value.test, cached: source !== "fresh", rateLimited: rl });
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
      { ok: false, error: "연결 테스트 실패: " + (e?.message ?? "unknown") },
      { status: 500 },
    );
  }
}
