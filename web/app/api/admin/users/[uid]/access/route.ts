import { NextResponse } from "next/server";
import { requireAdmin, isDenied, grantAccess, revokeAccess, listUserAccess } from "@/lib/auth/rbac";
import { getUserById } from "@/lib/auth/users";

export const dynamic = "force-dynamic";

// GET: 대상 사용자의 계좌 권한 목록.
export async function GET(_req: Request, { params }: { params: { uid: string } }) {
  const admin = await requireAdmin();
  if (isDenied(admin)) return admin;
  const target = await getUserById(String(params.uid));
  if (!target) return NextResponse.json({ ok: false, error: "not found" }, { status: 404 });
  return NextResponse.json({ ok: true, access: await listUserAccess(target.user_id) });
}

// POST: grant. body {account_index, role?}
export async function POST(req: Request, { params }: { params: { uid: string } }) {
  const admin = await requireAdmin();
  if (isDenied(admin)) return admin;
  const target = await getUserById(String(params.uid));
  if (!target) return NextResponse.json({ ok: false, error: "not found" }, { status: 404 });
  let body: any;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "잘못된 요청" }, { status: 400 });
  }
  const accountIndex = parseInt(String(body.account_index), 10);
  const role = body.role ?? "owner";
  const r = await grantAccess(admin, target.user_id, accountIndex, role);
  if (!r.ok) return NextResponse.json({ ok: false, code: r.code }, { status: 400 });
  return NextResponse.json({ ok: true });
}

// DELETE: revoke. body {account_index} (또는 ?account_index=)
export async function DELETE(req: Request, { params }: { params: { uid: string } }) {
  const admin = await requireAdmin();
  if (isDenied(admin)) return admin;
  const target = await getUserById(String(params.uid));
  if (!target) return NextResponse.json({ ok: false, error: "not found" }, { status: 404 });
  let accountIndex = NaN;
  try {
    const body = await req.json();
    accountIndex = parseInt(String(body.account_index), 10);
  } catch {
    const url = new URL(req.url);
    accountIndex = parseInt(url.searchParams.get("account_index") ?? "", 10);
  }
  const r = await revokeAccess(admin, target.user_id, accountIndex);
  if (!r.ok) return NextResponse.json({ ok: false, code: r.code }, { status: 400 });
  return NextResponse.json({ ok: true });
}
