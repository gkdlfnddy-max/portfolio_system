import { NextResponse } from "next/server";
import { getAccountView } from "@/lib/server/portfolioDb";
import { requireUnlocked } from "@/lib/auth/guard";
import { requireAccountUnlocked } from "@/lib/auth/account";
import { requireUser, requireAccountAccess, isDenied } from "@/lib/auth/rbac";

export const dynamic = "force-dynamic";

// 조회 전용 — DB(account snapshot)만 읽는다. KIS 호출 없음.
export async function GET(req: Request, { params }: { params: { id: string } }) {
  // 1차: 로그인(사용자 식별). 2차: 계좌 RBAC(미접근 403, URL 직접 차단).
  // CEO 보안 모델 = 로그인 + RBAC (PIN 전면 제거). requireAccountUnlocked 는 no-op(통과)으로 유지.
  const user = await requireUser();
  if (isDenied(user)) return user;
  const index = parseInt(params.id, 10);
  if (!Number.isInteger(index) || index < 1) {
    return NextResponse.json({ error: "invalid id" }, { status: 400 });
  }
  const access = await requireAccountAccess(user, index);
  if (isDenied(access)) return access;
  const ag = await requireAccountUnlocked(index, req);
  if (ag) return ag;
  const view = await getAccountView(index);
  if (!view) return NextResponse.json({ error: "not found" }, { status: 404 });
  return NextResponse.json(view);
}
