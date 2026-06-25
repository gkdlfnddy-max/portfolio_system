import { NextResponse } from "next/server";
import { requireAdmin, isDenied } from "@/lib/auth/rbac";
import { adminResetPassword, setUserStatus, getUserById, toSafeUser, type UserStatus } from "@/lib/auth/users";

export const dynamic = "force-dynamic";

// PATCH: admin 행위 — action=reset_pw | disable | enable | set_status.
//   reset_pw   → 임시 비번 1회 반환(평문, 로그 금지). reset_required=true.
//   disable    → status='disabled' + 세션 무효화.
//   enable     → status='active'.
//   set_status → body.status 로 지정.
export async function PATCH(req: Request, { params }: { params: { uid: string } }) {
  const admin = await requireAdmin();
  if (isDenied(admin)) return admin;
  const targetId = String(params.uid);

  let body: any;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "잘못된 요청" }, { status: 400 });
  }
  const action = String(body.action ?? "");

  if (action === "reset_pw") {
    const r = await adminResetPassword(admin.user_id, targetId);
    if (!r.ok) return NextResponse.json({ ok: false, code: r.code }, { status: 404 });
    // tempPassword 는 1회 응답만 — 호출 admin 이 전달. 로그/DB 평문 저장 안 함.
    return NextResponse.json({ ok: true, temp_password: r.tempPassword });
  }

  if (action === "disable" || action === "enable" || action === "set_status") {
    const status: UserStatus =
      action === "disable" ? "disabled" : action === "enable" ? "active" : (String(body.status ?? "active") as UserStatus);
    if (!["active", "disabled", "pending", "locked"].includes(status)) {
      return NextResponse.json({ ok: false, error: "invalid status" }, { status: 400 });
    }
    // 자기 자신(admin) 비활성화/잠금 방지 — 마지막 admin 잠김 사고 차단.
    if (String(admin.user_id) === targetId && status !== "active") {
      return NextResponse.json({ ok: false, error: "자기 자신 계정은 비활성화할 수 없습니다." }, { status: 400 });
    }
    const r = await setUserStatus(admin.user_id, targetId, status);
    if (!r.ok) return NextResponse.json({ ok: false, code: r.code }, { status: 404 });
    return NextResponse.json({ ok: true });
  }

  return NextResponse.json({ ok: false, error: "알 수 없는 action" }, { status: 400 });
}

// GET: 단일 사용자 + 권한 목록.
export async function GET(_req: Request, { params }: { params: { uid: string } }) {
  const admin = await requireAdmin();
  if (isDenied(admin)) return admin;
  const u = await getUserById(String(params.uid));
  if (!u) return NextResponse.json({ ok: false, error: "not found" }, { status: 404 });
  return NextResponse.json({ ok: true, user: toSafeUser(u) });
}
