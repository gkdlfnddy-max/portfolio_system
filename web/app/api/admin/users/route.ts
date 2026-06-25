import { NextResponse } from "next/server";
import { requireAdmin, isDenied } from "@/lib/auth/rbac";
import { listUsers, createUser, UserError } from "@/lib/auth/users";

export const dynamic = "force-dynamic";

// GET: 사용자 목록(admin). 비번 해시 미노출(SafeUser).
export async function GET() {
  const admin = await requireAdmin();
  if (isDenied(admin)) return admin;
  return NextResponse.json({ ok: true, users: await listUsers() });
}

// POST: admin 이 사용자 생성(role 지정 가능 — admin 도 생성 가능).
export async function POST(req: Request) {
  const admin = await requireAdmin();
  if (isDenied(admin)) return admin;
  let body: any;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "잘못된 요청" }, { status: 400 });
  }
  const email = String(body.email ?? "").trim();
  const password = String(body.password ?? "");
  const role = body.role === "admin" ? "admin" : "user";
  const displayName = body.display_name != null ? String(body.display_name).trim() : null;
  // admin 이 만든 계정은 첫 로그인 비번 변경 강제(reset_required) 가능.
  const resetRequired = body.reset_required === true;

  try {
    const user = await createUser({ email, password, role, display_name: displayName, reset_required: resetRequired });
    return NextResponse.json({ ok: true, user });
  } catch (e: any) {
    if (e instanceof UserError) {
      return NextResponse.json({ ok: false, error: e.message, code: e.code }, { status: 400 });
    }
    return NextResponse.json({ ok: false, error: "사용자 생성 실패" }, { status: 500 });
  }
}
