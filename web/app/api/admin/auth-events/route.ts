import { NextResponse } from "next/server";
import { requireAdmin, isDenied } from "@/lib/auth/rbac";
import { q } from "@/lib/auth/db";

export const dynamic = "force-dynamic";

// GET: append-only 인증 감사 로그 조회(admin). ?user_id= 필터, ?limit= (기본 100, 최대 500).
export async function GET(req: Request) {
  const admin = await requireAdmin();
  if (isDenied(admin)) return admin;
  const url = new URL(req.url);
  const userId = url.searchParams.get("user_id");
  const limit = Math.min(Math.max(parseInt(url.searchParams.get("limit") ?? "100", 10) || 100, 1), 500);

  const where = userId ? `WHERE user_id = $1` : ``;
  const args: unknown[] = userId ? [userId, limit] : [limit];
  const limitParam = userId ? `$2` : `$1`;
  const res = await q(
    `SELECT event_id, user_id, event_type, success, reason, created_at
       FROM portfolio.user_auth_events
       ${where}
       ORDER BY created_at DESC
       LIMIT ${limitParam}`,
    args,
  );
  return NextResponse.json({ ok: true, events: res.rows });
}
