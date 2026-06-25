import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";
import { getAccounts } from "@/lib/server/portfolioDb";
import { addAccount, addKiwoomAccount, type AccountMode } from "@/lib/server/envStore";
import { requireUnlocked, requireRecentReauth } from "@/lib/auth/guard";
import { requireUser, isDenied, listAccessibleAccounts } from "@/lib/auth/rbac";

const pexec = promisify(execFile);

export const dynamic = "force-dynamic";

// 조회: DB 의 accounts 테이블만. RBAC — 일반 user 는 본인 할당 계좌만, admin 은 전체.
export async function GET() {
  // 로그인(사용자 식별) + RBAC 필터만. CEO 보안 모델 = 로그인 + RBAC (PIN 전면 제거).
  const user = await requireUser();
  if (isDenied(user)) return user;

  const all = await getAccounts();
  const accessible = await listAccessibleAccounts(user);
  const accounts = accessible === "all" ? all : all.filter((a) => accessible.includes(a.account_index));
  // is_admin: 홈 라벨("전체 계좌"/"내 계좌")용. 실제 차단은 위 서버 authz(accessible 필터)가 담당.
  return NextResponse.json({ is_admin: accessible === "all", accounts });
}

// 생성: 자격증명(KIS)은 .env 에 기록(envStore) → 민감 작업이므로 재인증 필요.
// 계좌 생성은 admin 만(신규 계좌는 권한 부여 전 어떤 user 에도 보이지 않음).
export async function POST(req: Request) {
  const user = await requireUser();
  if (isDenied(user)) return user;
  if (user.role !== "admin") {
    return NextResponse.json({ ok: false, error: "계좌 생성은 관리자만 가능합니다.", code: "FORBIDDEN" }, { status: 403 });
  }
  const denied = await requireRecentReauth();
  if (denied) return denied;
  let body: any;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "잘못된 요청" }, { status: 400 });
  }
  // 멀티 브로커 — broker 에 따라 KIS / 키움 자격증명을 *분리된* 변수에 기록(혼용 금지).
  const broker = String(body.broker ?? "kis").trim().toLowerCase();
  const alias = String(body.alias ?? "").trim();
  const mode = String(body.mode ?? "paper").trim() as AccountMode;
  const appKey = String(body.appKey ?? "").trim();
  const appSecret = String(body.appSecret ?? "").trim();
  const accountNo = String(body.accountNo ?? "").trim();
  const productCode = String(body.productCode ?? "01").trim();

  if (!["kis", "kiwoom"].includes(broker)) {
    return NextResponse.json({ ok: false, error: "broker 는 kis|kiwoom 만 지원합니다." }, { status: 400 });
  }
  if (!alias || !appKey || !appSecret || !accountNo) {
    return NextResponse.json(
      { ok: false, error: "별칭 · APP Key · APP Secret · 계좌번호는 필수입니다." },
      { status: 400 },
    );
  }
  if (!["mock", "paper", "live"].includes(mode)) {
    return NextResponse.json({ ok: false, error: "mode 는 mock|paper|live" }, { status: 400 });
  }

  let index: number;
  try {
    index =
      broker === "kiwoom"
        ? await addKiwoomAccount({ alias, mode, appKey, appSecret, accountNo })
        : await addAccount({ alias, mode, appKey, appSecret, accountNo, productCode });
  } catch (e: any) {
    return NextResponse.json({ ok: false, error: ".env 기록 실패: " + (e?.message ?? "unknown") }, { status: 500 });
  }

  // DB 반영(메타 + 잔고) — 백엔드 job 트리거. 실패해도 계좌 자체는 생성됨.
  const root = path.resolve(process.cwd(), "..");
  for (const py of [path.resolve(process.cwd(), "..", ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      await pexec(py, ["-m", "main_mission.portfolio_os.broker.sync_job", "--account", String(index)], {
        cwd: root, timeout: 30000, env: { ...process.env, PYTHONIOENCODING: "utf-8" }, maxBuffer: 1024 * 1024,
      });
      break;
    } catch (e: any) {
      if (e?.code === "ENOENT") continue;
      break; // job 실패는 무시(계좌는 생성됨, 이후 수동 동기화 가능)
    }
  }

  return NextResponse.json({ ok: true, index });
}
