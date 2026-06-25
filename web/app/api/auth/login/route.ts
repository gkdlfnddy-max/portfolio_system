import { NextResponse } from "next/server";
import { verifyLogin, createSession } from "@/lib/auth/users";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  let body: any;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "잘못된 요청" }, { status: 400 });
  }
  const email = String(body.email ?? "").trim();
  const password = String(body.password ?? "");
  if (!email || !password) {
    return NextResponse.json({ ok: false, error: "이메일/비밀번호를 입력하세요." }, { status: 400 });
  }

  const result = await verifyLogin(email, password);
  if (!result.ok) {
    // enumeration 방지: INVALID 는 자격증명 불일치로 통일. LOCKED/DISABLED 만 별도 안내.
    if (result.code === "LOCKED") {
      return NextResponse.json(
        { ok: false, error: "계정이 잠겼습니다. 관리자에게 문의하거나 비밀번호를 재설정하세요.", code: "LOCKED" },
        { status: 423 },
      );
    }
    if (result.code === "DISABLED") {
      return NextResponse.json(
        { ok: false, error: "비활성화된 계정입니다.", code: "DISABLED" },
        { status: 403 },
      );
    }
    return NextResponse.json(
      { ok: false, error: "이메일 또는 비밀번호가 올바르지 않습니다.", code: "INVALID" },
      { status: 401 },
    );
  }

  await createSession(result.user.user_id);
  // reset_required 면 프론트가 비번 변경 화면으로 유도.
  return NextResponse.json({ ok: true, user: result.user, reset_required: result.user.reset_required });
}
