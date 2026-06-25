// 앱 PIN 가드 — PIN 전면 제거(CEO 결정). 접근 통제는 로그인 + RBAC 만.
// 과거 앱 PIN(pos_session) 게이트는 폐기했다. 아래 두 가드는 라우트/RBAC 가 import 하므로
// no-op(통과)으로 유지한다. (login=401 UNAUTHENTICATED, 계좌 RBAC=403 FORBIDDEN 은 rbac.ts 가 담당)
// live 주문 hard lock(KIS_LIVE_CONFIRM)은 이 레이어와 무관하게 별도 유지.
import { NextResponse } from "next/server";

export type Denied = NextResponse;

// 앱 PIN 게이트 제거 — 로그인 + RBAC 가 접근 통제. 항상 통과(null).
export async function requireUnlocked(): Promise<Denied | null> {
  return null;
}

// 재인증 게이트도 제거 — 항상 통과(null). 민감 작업 보호는 로그인 + RBAC + (주문 단계) live hard lock.
export async function requireRecentReauth(): Promise<Denied | null> {
  return null;
}
