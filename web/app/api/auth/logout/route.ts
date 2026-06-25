import { NextResponse } from "next/server";
import { getUserFromSession, destroySession, logAuthEvent } from "@/lib/auth/users";

export const dynamic = "force-dynamic";

export async function POST() {
  const user = await getUserFromSession();
  await destroySession();
  if (user) await logAuthEvent(user.user_id, "logout", true, null);
  return NextResponse.json({ ok: true });
}
