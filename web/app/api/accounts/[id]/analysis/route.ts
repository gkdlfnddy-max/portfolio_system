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

const PY_CANDIDATES = () => [
  path.resolve(process.cwd(), "..", ".venv", "bin", "python"),
  "python",
  "python3",
  "py",
];

// 백엔드 python 엔진 1개를 CLI 로 스폰해 마지막 JSON 줄을 파싱(운영 truth=DB, mock 없음).
// 실패/미발견 시 null 반환 — 호출측이 "데이터 없음"으로 정직 표기.
async function runEngine(mod: string, extraArgs: string[]): Promise<any | null> {
  const root = path.resolve(process.cwd(), "..");
  const args = ["-m", `main_mission.portfolio_os.${mod}`, ...extraArgs];
  for (const py of PY_CANDIDATES()) {
    try {
      const { stdout } = await pexec(py, args, {
        cwd: root, timeout: 30000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 4 * 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "null";
      try { return JSON.parse(line); } catch { return null; }
    } catch (e: any) {
      if (e?.code === "ENOENT") continue; // 다음 python 후보
      return null; // 모듈 오류 → 정직하게 null(데이터 없음)
    }
  }
  return null;
}

// GET — 관점 분석(6축/관점/후보) 조회 전용. 자동 주문/적용 0.
//   소스: perspective_variants.generate(A/B/C), portfolio_impact.different_interpretations,
//         decline_scan.scan_account_universe(6축), investor_objective.get, user_views.list_views.
// RBAC: 일반 user 는 자기 계좌만(타계좌 403), 미로그인 401 — requireAccountAccessAndUnlocked.
export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req); if (ag) return ag;

  const sid = String(id);
  // 엔진을 병렬 스폰(독립적). draft 저장 안 함(--no-save) — 조회 전용.
  const [variants, interpretations, scan, objectiveRaw, viewsRaw] = await Promise.all([
    runEngine("perspective_variants", ["--account", sid, "--generate", "--no-save"]),
    runEngine("portfolio_impact", ["--account", sid, "--interpretations"]),
    runEngine("decline_scan", ["--account", sid]),
    runEngine("investor_objective", ["--account", sid, "--get"]),
    runEngine("user_views", ["--account", sid, "--list"]),
  ]);

  // investor_objective --get 은 {ok,is_set,objective}, user_views --list 은 {ok,views}.
  const objective = objectiveRaw?.objective ?? null;
  const objective_set = objectiveRaw?.is_set ?? false;
  const views = Array.isArray(viewsRaw?.views) ? viewsRaw.views : [];

  return NextResponse.json({
    ok: true,
    account_index: id,
    objective,
    objective_set,
    views,
    // 관점별 A/B/C 후보(요약·왜 맞는지·비중·장점·위험·언제 깨지는지·추가확인).
    variants: variants?.ok ? variants : null,
    // 같은 데이터 다른 해석(공통 사실/내 관점/관점별 해석/선택 후보).
    interpretations: interpretations?.ok ? interpretations : null,
    // 하락 징후 6축 스캔(가용/부족 축·overall confidence·보수전환 후보).
    decline_scan: scan?.ok ? scan : null,
    // 어떤 것도 자동 적용/주문하지 않음 — 사용자 승인 전 policy 미반영.
    requires_user_approval: true,
    auto_applied: false,
    auto_order_created: false,
    // 6축 화면 필수 고지(판단 보조·데이터 없는 축 제외·자동주문 없음·승인 전 미반영).
    disclaimer: "투자 판단 보조 · 데이터 없는 축 제외 · 자동 주문 없음 · 사용자 승인 전 policy 미반영 · confidence 낮으면 단정 안 함.",
  });
}

// 중전제 분석: 내 생각·관심 → 핵심 아이디어 + 테마 의견 + 개선 제안 + AI 종합 의견.
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });
  const az = await requireAccountAccessAndUnlocked(id); if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;
  const body = await req.json().catch(() => ({}));
  const args = ["-m", "main_mission.portfolio_os.analysis", "--account", String(id), "--analyze"];
  if (body.interests != null) args.push("--interests", String(body.interests));
  if (body.views != null) args.push("--views", String(body.views));
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, args, {
        cwd: root, timeout: 20000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 2 * 1024 * 1024,
      });
      const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop() ?? "{}";
      return NextResponse.json(JSON.parse(line));
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      return NextResponse.json({ ok: false, error: "분석 실패: " + (e?.message ?? "") }, { status: 500 });
    }
  }
  return NextResponse.json({ ok: false, error: "python 미발견" }, { status: 500 });
}
