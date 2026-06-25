import { NextResponse } from "next/server";
import { getUserFromSession, toSafeUser } from "@/lib/auth/users";
import { listAccessibleAccounts } from "@/lib/auth/rbac";

export const dynamic = "force-dynamic";

// 현재 로그인 사용자 + 접근 가능 계좌(필터용). 비로그인이면 user=null(200).
export async function GET() {
  const user = await getUserFromSession();
  if (!user) return NextResponse.json({ ok: true, user: null });
  const accessible = await listAccessibleAccounts(user);
  return NextResponse.json({
    ok: true,
    user: toSafeUser(user),
    accessible_accounts: accessible, // "all" | number[]
  });
}
