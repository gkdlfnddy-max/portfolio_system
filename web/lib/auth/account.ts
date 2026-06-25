// 계좌 가드 — PIN(2차 보안) 전면 제거(CEO 결정). 접근 통제는 로그인 + RBAC 만.
// 과거 계좌별 PIN(account_security_settings / account_auth_sessions) 로직은 폐기했다.
//   · DB 테이블/데이터는 건드리지 않는다(미사용으로 둠).
//   · 아래 두 가드는 라우트가 import 하므로 no-op(통과)으로 유지한다.
//   · live 주문 hard lock(KIS_LIVE_CONFIRM)은 이 레이어와 무관하게 별도 유지.
import { NextResponse } from "next/server";

export type Denied = NextResponse;

// 계좌별 PIN 전면 제거 — 로그인 + RBAC(requireLoginAndAccount)로 접근 통제. 항상 통과(null).
export async function requireAccountUnlocked(
  _accountId: number,
  _req?: unknown,
): Promise<Denied | null> {
  return null;
}

// 민감 작업 재인증 게이트도 제거 — 항상 통과(null). (live 주문 하드락은 별개로 유지)
export async function requireAccountReauth(
  _accountId: number,
  _requirement: "strategy" | "rebalance" | "order_approval" | "always",
  _req?: unknown,
): Promise<Denied | null> {
  return null;
}
