import { NextResponse } from "next/server";
import { createUser, createSession, UserError, logAuthEvent } from "@/lib/auth/users";

export const dynamic = "force-dynamic";

// 일반 signup 은 role='user' 고정(admin 자가승격 금지).
export async function POST(req: Request) {
  let body: any;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "잘못된 요청" }, { status: 400 });
  }
  const email = String(body.email ?? "").trim();
  const password = String(body.password ?? "");
  const displayName = body.display_name != null ? String(body.display_name).trim() : null;

  try {
    const user = await createUser({ email, password, role: "user", display_name: displayName });
    await logAuthEvent(user.user_id, "signup", true, null);
    await createSession(user.user_id);
    return NextResponse.json({ ok: true, user });
  } catch (e: any) {
    if (e instanceof UserError) {
      return NextResponse.json({ ok: false, error: e.message, code: e.code }, { status: 400 });
    }
    return NextResponse.json({ ok: false, error: "회원가입 실패" }, { status: 500 });
  }
}
