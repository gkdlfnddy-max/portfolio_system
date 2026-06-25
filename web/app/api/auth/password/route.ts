import { NextResponse } from "next/server";
import {
  changePassword,
  firstLoginSetPassword,
  createResetToken,
  consumeResetToken,
  getUserFromSession,
} from "@/lib/auth/users";

export const dynamic = "force-dynamic";

// 단일 라우트, action 으로 분기:
//   change       : 로그인 상태 + 현재 비번 검증 → 신규 비번.        body {action, current, next}
//   first_login  : 로그인 상태 + reset_required → 신규 비번.        body {action, next}
//   reset_request: 비로그인 — 이메일로 1회용 토큰 발급.            body {action, email}
//   reset_consume: 비로그인 — 토큰 + 신규 비번.                    body {action, token, next}
export async function POST(req: Request) {
  let body: any;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "잘못된 요청" }, { status: 400 });
  }
  const action = String(body.action ?? "change");

  if (action === "change") {
    const user = await getUserFromSession();
    if (!user) return NextResponse.json({ ok: false, error: "로그인이 필요합니다.", code: "UNAUTHENTICATED" }, { status: 401 });
    const r = await changePassword(user.user_id, String(body.current ?? ""), String(body.next ?? ""));
    if (!r.ok) {
      const status = r.code === "BAD_CURRENT" ? 403 : 400;
      return NextResponse.json({ ok: false, code: r.code }, { status });
    }
    return NextResponse.json({ ok: true });
  }

  if (action === "first_login") {
    const user = await getUserFromSession();
    if (!user) return NextResponse.json({ ok: false, error: "로그인이 필요합니다.", code: "UNAUTHENTICATED" }, { status: 401 });
    const r = await firstLoginSetPassword(user.user_id, String(body.next ?? ""));
    if (!r.ok) return NextResponse.json({ ok: false, code: r.code }, { status: 400 });
    return NextResponse.json({ ok: true });
  }

  if (action === "reset_request") {
    const email = String(body.email ?? "").trim();
    // 계정 존재여부 비노출 — 항상 동일 응답. dev 편의를 위해 NODE_ENV!=production 일 때만 토큰 반환.
    const token = email ? await createResetToken(email) : null;
    const resp: any = { ok: true, message: "해당 이메일로 재설정 안내를 보냈습니다(존재할 경우)." };
    if (process.env.NODE_ENV !== "production" && token) resp.dev_token = token;
    return NextResponse.json(resp);
  }

  if (action === "reset_consume") {
    const r = await consumeResetToken(String(body.token ?? ""), String(body.next ?? ""));
    if (!r.ok) {
      const status = r.code === "WEAK_PASSWORD" ? 400 : 400;
      return NextResponse.json({ ok: false, code: r.code }, { status });
    }
    return NextResponse.json({ ok: true });
  }

  return NextResponse.json({ ok: false, error: "알 수 없는 action" }, { status: 400 });
}
