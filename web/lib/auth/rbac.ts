// RBAC — 사용자/계좌 권한 enforce. 서버에서 강제(프론트 숨김에 의존하지 않음).
//   admin  : 전체 계좌 접근.
//   user   : user_account_access 에 행이 있는 account_index 만. 없으면 403.
// requireUser / requireAccountAccess 는 라우트 핸들러 안에서 호출하고,
// 반환 Denied(NextResponse)가 truthy 면 그대로 return 한다.
import { NextResponse } from "next/server";
import { q } from "./db";
import { getUserFromSession, logAuthEvent, type UserRow } from "./users";
import { requireUnlocked, requireRecentReauth } from "./guard";

export type Denied = NextResponse;
export type AccessRole = "owner" | "manager" | "viewer";

// ── 401: 로그인 필요 ──
// 통과 시 user 반환, 실패 시 NextResponse(401).
export async function requireUser(): Promise<UserRow | Denied> {
  const user = await getUserFromSession();
  if (!user) {
    return NextResponse.json(
      { ok: false, error: "로그인이 필요합니다.", code: "UNAUTHENTICATED" },
      { status: 401 },
    );
  }
  return user;
}

// ── admin 전용 ──
export async function requireAdmin(): Promise<UserRow | Denied> {
  const u = await requireUser();
  if (u instanceof NextResponse) return u;
  if (u.role !== "admin") {
    return NextResponse.json(
      { ok: false, error: "관리자 권한이 필요합니다.", code: "FORBIDDEN" },
      { status: 403 },
    );
  }
  return u;
}

export function isDenied(v: unknown): v is Denied {
  return v instanceof NextResponse;
}

// 사용자가 특정 계좌에 부여된 access_role(있으면) 반환. admin 은 항상 'owner' 취급.
export async function getAccessRole(user: UserRow, accountIndex: number): Promise<AccessRole | null> {
  if (user.role === "admin") return "owner";
  const res = await q<{ access_role: AccessRole }>(
    `SELECT access_role FROM portfolio.user_account_access
       WHERE user_id = $1 AND account_index = $2 LIMIT 1`,
    [user.user_id, accountIndex],
  );
  return res.rows[0]?.access_role ?? null;
}

// ── 403: 계좌 접근 권한 enforce ──
// admin 통과. 일반 user 는 user_account_access 에 행이 없으면 403.
// 통과 시 access_role 반환.
export async function requireAccountAccess(
  user: UserRow,
  accountIndex: number,
): Promise<AccessRole | Denied> {
  if (!Number.isInteger(accountIndex) || accountIndex < 1) {
    return NextResponse.json(
      { ok: false, error: "잘못된 계좌 식별자입니다.", code: "INVALID_ACCOUNT" },
      { status: 400 },
    );
  }
  const role = await getAccessRole(user, accountIndex);
  if (!role) {
    return NextResponse.json(
      { ok: false, error: "해당 계좌에 대한 접근 권한이 없습니다.", code: "FORBIDDEN" },
      { status: 403 },
    );
  }
  return role;
}

// admin=전체 account_index, user=할당분만. accounts 메타는 호출측에서 이 목록으로 필터.
export async function listAccessibleAccounts(user: UserRow): Promise<number[] | "all"> {
  if (user.role === "admin") return "all";
  const res = await q<{ account_index: number }>(
    `SELECT account_index FROM portfolio.user_account_access WHERE user_id = $1 ORDER BY account_index`,
    [user.user_id],
  );
  return res.rows.map((r) => r.account_index);
}

// ── 권한 부여/회수 (admin 행위) ──
export async function grantAccess(
  adminUser: UserRow,
  targetUserId: string,
  accountIndex: number,
  role: AccessRole = "owner",
): Promise<{ ok: true } | { ok: false; code: string }> {
  if (!Number.isInteger(accountIndex) || accountIndex < 1) return { ok: false, code: "INVALID_ACCOUNT" };
  if (!["owner", "manager", "viewer"].includes(role)) return { ok: false, code: "INVALID_ROLE" };
  await q(
    `INSERT INTO portfolio.user_account_access (user_id, account_index, access_role, created_by)
     VALUES ($1, $2, $3, $4)
     ON CONFLICT (user_id, account_index)
       DO UPDATE SET access_role = EXCLUDED.access_role`,
    [targetUserId, accountIndex, role, adminUser.user_id],
  );
  await logAuthEvent(adminUser.user_id, "account_access_granted", true, `user=${targetUserId} acct=${accountIndex} role=${role}`);
  return { ok: true };
}

export async function revokeAccess(
  adminUser: UserRow,
  targetUserId: string,
  accountIndex: number,
): Promise<{ ok: true } | { ok: false; code: string }> {
  if (!Number.isInteger(accountIndex) || accountIndex < 1) return { ok: false, code: "INVALID_ACCOUNT" };
  await q(
    `DELETE FROM portfolio.user_account_access WHERE user_id = $1 AND account_index = $2`,
    [targetUserId, accountIndex],
  );
  await logAuthEvent(adminUser.user_id, "account_access_revoked", true, `user=${targetUserId} acct=${accountIndex}`);
  return { ok: true };
}

// 라우트 1줄 가드 — 로그인 + 계좌 RBAC 를 한 번에. 통과 시 null, 실패 시 Denied.
//   const g = await requireLoginAndAccount(id); if (g) return g;
// 미접근(권한 행 없음) 시 403 — URL 직접 접근을 서버에서 차단.
export async function requireLoginAndAccount(accountIndex: number): Promise<Denied | null> {
  const user = await requireUser();
  if (user instanceof NextResponse) return user;
  const access = await requireAccountAccess(user, accountIndex);
  if (access instanceof NextResponse) return access;
  return null;
}

// ── 정규 게이트 순서 SSOT ──
// 계좌 API 가 일관된 순서/코드로 차단하도록 한 곳에 묶는다.
// 표는 docs/portfolio/auth_response_codes.md 참고.
//   1) 로그인       → 미로그인 401 UNAUTHENTICATED
//   2) 계좌 RBAC    → 권한 없음 403 FORBIDDEN  (PIN 보다 먼저 — 권한 없는 계좌는 PIN 묻기 전에 막는다)
//   3) 앱 PIN       → 미해제 401 PIN_REQUIRED
// account.ts 의 계좌별 PIN(ACCOUNT_LOCKED 등)은 이 다음 단계(라우트가 별도 호출)로 둔다.
//
//   const g = await requireAccountAccessAndUnlocked(id); if (g) return g;
export async function requireAccountAccessAndUnlocked(accountIndex: number): Promise<Denied | null> {
  const az = await requireLoginAndAccount(accountIndex);
  if (az) return az;
  const pin = await requireUnlocked();
  if (pin) return pin;
  return null;
}

// 민감 작업용 — 위와 동일 순서이되 앱 PIN 단계를 (재)인증 창 검사로 강화.
//   미해제 401 PIN_REQUIRED / 창 만료 403 REAUTH_REQUIRED.
export async function requireAccountAccessAndReauth(accountIndex: number): Promise<Denied | null> {
  const az = await requireLoginAndAccount(accountIndex);
  if (az) return az;
  const reauth = await requireRecentReauth();
  if (reauth) return reauth;
  return null;
}

// 특정 사용자의 계좌 권한 목록.
export async function listUserAccess(
  targetUserId: string,
): Promise<{ account_index: number; access_role: AccessRole }[]> {
  const res = await q<{ account_index: number; access_role: AccessRole }>(
    `SELECT account_index, access_role FROM portfolio.user_account_access
       WHERE user_id = $1 ORDER BY account_index`,
    [targetUserId],
  );
  return res.rows;
}
