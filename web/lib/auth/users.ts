// 사용자 로그인 코어 — 비밀번호 해시(scrypt)·세션·잠금·비번 재설정.
// 보안 불변: 비밀번호 평문 / reset token 원문은 DB·로그·쿠키 어디에도 저장/출력하지 않는다.
// PIN auth(auth_sessions, account.ts)와 별개 레이어: 로그인=사용자 식별, PIN=계좌 2차 보호.
import { cookies, headers } from "next/headers";
import { randomBytes, scryptSync, timingSafeEqual, createHash } from "node:crypto";
import { q } from "./db";
import { MAX_FAILED, SESSION_IDLE_MS } from "./config";

// ───────────────────────── 상수 ─────────────────────────
const KEYLEN = 64;
// 사용자 로그인 세션 쿠키 — PIN 세션(pos_session)과 분리한다.
export const USER_SESSION_COOKIE = "pos_user";
// 로그인 세션 수명(절대 만료). 슬라이딩 idle 은 last_seen 으로 별도 검사.
const USER_SESSION_TTL_MS = (() => {
  const raw = process.env.AUTH_USER_SESSION_TTL_MS;
  const n = raw ? Number(raw) : NaN;
  return Number.isFinite(n) && n > 0 ? n : 7 * 24 * 60 * 60_000; // 기본 7일
})();
// 임시(reset) 토큰 수명.
const RESET_TOKEN_TTL_MS = (() => {
  const raw = process.env.AUTH_RESET_TOKEN_TTL_MS;
  const n = raw ? Number(raw) : NaN;
  return Number.isFinite(n) && n > 0 ? n : 60 * 60_000; // 기본 1시간
})();

// ───────────────────────── 타입 ─────────────────────────
export type Role = "admin" | "user";
export type UserStatus = "active" | "disabled" | "pending" | "locked";

export type UserRow = {
  user_id: string; // BIGINT → pg 는 string 으로 반환
  login_id: string | null;
  email: string;
  display_name: string | null;
  password_hash: string;
  password_algo: string;
  role: Role;
  status: UserStatus;
  reset_required: boolean;
  failed_logins: number;
  last_login_at: string | null;
  created_at: string;
};

// 외부로 노출 가능한(해시 제외) 사용자 형태.
export type SafeUser = {
  user_id: string;
  login_id: string | null;
  email: string;
  display_name: string | null;
  role: Role;
  status: UserStatus;
  reset_required: boolean;
};

export function toSafeUser(u: UserRow): SafeUser {
  return {
    user_id: u.user_id,
    login_id: u.login_id ?? null,
    email: u.email,
    display_name: u.display_name,
    role: u.role,
    status: u.status,
    reset_required: u.reset_required,
  };
}

// ───────────────────────── 해시 ─────────────────────────
// 저장 형식: "<salt_hex>:<hash_hex>" — pin.ts 와 동일한 scrypt, 단일 컬럼에 salt 동봉.
export function hashPassword(password: string): string {
  const salt = randomBytes(16).toString("hex");
  const hash = scryptSync(password, salt, KEYLEN).toString("hex");
  return `${salt}:${hash}`;
}

export function verifyPassword(password: string, stored: string): boolean {
  if (!stored || !stored.includes(":")) return false;
  const [salt, hash] = stored.split(":");
  if (!salt || !hash) return false;
  let expected: Buffer;
  try {
    expected = Buffer.from(hash, "hex");
  } catch {
    return false;
  }
  if (expected.length !== KEYLEN) return false;
  const actual = scryptSync(password, salt, KEYLEN);
  return timingSafeEqual(actual, expected);
}

function sha256(v: string): string {
  return createHash("sha256").update(v).digest("hex");
}

// 헤더(IP/UA) 원문 저장 금지 — sha256 해시만.
function requestHashes(): { ipHash: string | null; uaHash: string | null } {
  const h = headers();
  const fwd = h.get("x-forwarded-for") ?? "";
  const ip = fwd.split(",")[0]?.trim() || h.get("x-real-ip") || "";
  const ua = h.get("user-agent") ?? "";
  return { ipHash: ip ? sha256(ip) : null, uaHash: ua ? sha256(ua) : null };
}

// ───────────────────────── 감사 로그(append-only) ─────────────────────────
export type AuthEventType =
  | "signup"
  | "login_success"
  | "login_failed"
  | "logout"
  | "password_changed"
  | "password_reset_requested"
  | "password_reset_completed"
  | "admin_password_reset"
  | "account_access_granted"
  | "account_access_revoked";

export async function logAuthEvent(
  userId: string | null,
  eventType: AuthEventType,
  success: boolean,
  reason: string | null,
): Promise<void> {
  const { ipHash, uaHash } = requestHashes();
  try {
    await q(
      `INSERT INTO portfolio.user_auth_events
         (user_id, event_type, success, reason, ip_hash, user_agent_hash, created_at)
       VALUES ($1, $2, $3, $4, $5, $6, now())`,
      [userId, eventType, success, reason, ipHash, uaHash],
    );
  } catch {
    // 감사 로그 실패가 인증 흐름을 막지 않도록 best-effort.
  }
}

// ───────────────────────── 조회 ─────────────────────────
function normEmail(email: string): string {
  return email.trim().toLowerCase();
}

export async function getUserByEmail(email: string): Promise<UserRow | null> {
  const res = await q<UserRow>(
    `SELECT * FROM portfolio.users WHERE email = $1 LIMIT 1`,
    [normEmail(email)],
  );
  return res.rows[0] ?? null;
}

// 로그인 식별자 = login_id 또는 email (권장안 A). 둘 다 허용.
export async function getUserByLoginOrEmail(identifier: string): Promise<UserRow | null> {
  const id = (identifier ?? "").trim();
  const res = await q<UserRow>(
    `SELECT * FROM portfolio.users WHERE login_id = $1 OR email = $2 LIMIT 1`,
    [id, normEmail(id)],
  );
  return res.rows[0] ?? null;
}

export async function getUserById(userId: string): Promise<UserRow | null> {
  const res = await q<UserRow>(
    `SELECT * FROM portfolio.users WHERE user_id = $1 LIMIT 1`,
    [userId],
  );
  return res.rows[0] ?? null;
}

// ───────────────────────── 생성 ─────────────────────────
export type CreateUserInput = {
  email: string;
  password: string;
  role?: Role;
  display_name?: string | null;
  reset_required?: boolean;
  login_id?: string | null;   // 선택 — 일반 signup 은 미지정(email 로그인), admin 등 구분용
};

export class UserError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

export async function createUser(input: CreateUserInput): Promise<SafeUser> {
  const email = normEmail(input.email);
  if (!email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
    throw new UserError("INVALID_EMAIL", "올바른 이메일 형식이 아닙니다.");
  }
  if (!input.password || input.password.length < 8) {
    throw new UserError("WEAK_PASSWORD", "비밀번호는 8자 이상이어야 합니다.");
  }
  const role: Role = input.role === "admin" ? "admin" : "user";
  const passwordHash = hashPassword(input.password); // 평문 미저장
  try {
    const res = await q<UserRow>(
      `INSERT INTO portfolio.users
         (email, display_name, password_hash, password_algo, role, status, reset_required, login_id)
       VALUES ($1, $2, $3, 'scrypt', $4, 'active', $5, $6)
       RETURNING *`,
      [email, input.display_name ?? null, passwordHash, role, input.reset_required ?? false,
       input.login_id ? input.login_id.trim() : null],
    );
    return toSafeUser(res.rows[0]);
  } catch (e: any) {
    if (e?.code === "23505") {
      throw new UserError("EMAIL_TAKEN", "이미 등록된 이메일입니다.");
    }
    throw e;
  }
}

// ───────────────────────── 로그인 ─────────────────────────
export type LoginResult =
  | { ok: true; user: SafeUser }
  | { ok: false; code: "INVALID" | "DISABLED" | "LOCKED" };

// 실패 시 failed_logins++ , MAX_FAILED 이상이면 status='locked'. disabled/locked 는 차단.
// 사용자 존재여부를 응답으로 구분하지 않는다(INVALID 통일) — enumeration 방지.
export async function verifyLogin(identifier: string, password: string): Promise<LoginResult> {
  const user = await getUserByLoginOrEmail(identifier);  // login_id 또는 email 허용
  if (!user) {
    await logAuthEvent(null, "login_failed", false, "no_such_user");
    return { ok: false, code: "INVALID" };
  }

  if (user.status === "disabled") {
    await logAuthEvent(user.user_id, "login_failed", false, "disabled");
    return { ok: false, code: "DISABLED" };
  }
  if (user.status === "locked") {
    await logAuthEvent(user.user_id, "login_failed", false, "locked");
    return { ok: false, code: "LOCKED" };
  }

  const good = verifyPassword(password, user.password_hash);
  if (!good) {
    const nextFails = user.failed_logins + 1;
    const lock = nextFails >= MAX_FAILED;
    await q(
      `UPDATE portfolio.users
         SET failed_logins = $2,
             status = CASE WHEN $3 THEN 'locked' ELSE status END,
             updated_at = now()
       WHERE user_id = $1`,
      [user.user_id, nextFails, lock],
    );
    await logAuthEvent(user.user_id, "login_failed", false, lock ? "locked_now" : "bad_password");
    return { ok: false, code: lock ? "LOCKED" : "INVALID" };
  }

  // 성공: failed_logins 리셋 + last_login_at 갱신.
  await q(
    `UPDATE portfolio.users
       SET failed_logins = 0, last_login_at = now(), updated_at = now()
     WHERE user_id = $1`,
    [user.user_id],
  );
  await logAuthEvent(user.user_id, "login_success", true, null);
  return { ok: true, user: toSafeUser(user) };
}

// ───────────────────────── 세션 ─────────────────────────
const userCookieOpts = (maxAgeSec: number) => ({
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
  maxAge: maxAgeSec,
});

// opaque session_id 생성 + httpOnly 쿠키 설정.
export async function createSession(userId: string): Promise<string> {
  const sessionId = randomBytes(32).toString("hex");
  const { ipHash, uaHash } = requestHashes();
  const ttlSec = Math.floor(USER_SESSION_TTL_MS / 1000);
  await q(
    `INSERT INTO portfolio.user_sessions
       (session_id, user_id, created_at, expires_at, last_seen_at, ip_hash, user_agent_hash)
     VALUES ($1, $2, now(), now() + ($3 || ' seconds')::interval, now(), $4, $5)`,
    [sessionId, userId, String(ttlSec), ipHash, uaHash],
  );
  cookies().set(USER_SESSION_COOKIE, sessionId, userCookieOpts(ttlSec));
  return sessionId;
}

// 현재 쿠키 → 사용자. 만료/철회/유휴초과/계정비활성이면 null.
export async function getUserFromSession(): Promise<UserRow | null> {
  const sid = cookies().get(USER_SESSION_COOKIE)?.value;
  if (!sid) return null;

  const res = await q<{ user_id: string; expires_at: string; last_seen_at: string; revoked_at: string | null }>(
    `SELECT user_id, expires_at, last_seen_at, revoked_at
       FROM portfolio.user_sessions WHERE session_id = $1 LIMIT 1`,
    [sid],
  );
  const sess = res.rows[0];
  if (!sess || sess.revoked_at) return null;
  const now = Date.now();
  if (sess.expires_at && Date.parse(sess.expires_at) <= now) return null;
  if (sess.last_seen_at && now - Date.parse(sess.last_seen_at) > SESSION_IDLE_MS) return null;

  const user = await getUserById(sess.user_id);
  if (!user) return null;
  if (user.status === "disabled" || user.status === "locked") return null;

  // 활동 갱신(슬라이딩 idle).
  await q(
    `UPDATE portfolio.user_sessions SET last_seen_at = now() WHERE session_id = $1 AND revoked_at IS NULL`,
    [sid],
  );
  return user;
}

// 로그아웃 — 현재 세션 철회 + 쿠키 제거.
export async function destroySession(): Promise<void> {
  const sid = cookies().get(USER_SESSION_COOKIE)?.value;
  if (sid) {
    await q(
      `UPDATE portfolio.user_sessions SET revoked_at = now() WHERE session_id = $1 AND revoked_at IS NULL`,
      [sid],
    );
  }
  cookies().delete(USER_SESSION_COOKIE);
}

// 해당 사용자의 모든 세션 철회(비번 변경/리셋 후 강제 로그아웃).
export async function revokeAllSessions(userId: string): Promise<void> {
  await q(
    `UPDATE portfolio.user_sessions SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL`,
    [userId],
  );
}

// ───────────────────────── 비밀번호 변경 ─────────────────────────
export async function changePassword(
  userId: string,
  current: string,
  next: string,
): Promise<{ ok: true } | { ok: false; code: string }> {
  if (!next || next.length < 8) return { ok: false, code: "WEAK_PASSWORD" };
  const user = await getUserById(userId);
  if (!user) return { ok: false, code: "NOT_FOUND" };
  if (!verifyPassword(current, user.password_hash)) {
    await logAuthEvent(userId, "password_changed", false, "bad_current");
    return { ok: false, code: "BAD_CURRENT" };
  }
  await q(
    `UPDATE portfolio.users
       SET password_hash = $2, password_algo = 'scrypt', password_updated_at = now(),
           reset_required = false, updated_at = now()
     WHERE user_id = $1`,
    [userId, hashPassword(next)],
  );
  await revokeAllSessions(userId); // 기존 세션 전부 무효화
  await logAuthEvent(userId, "password_changed", true, null);
  return { ok: true };
}

// reset_required 인 사용자가 첫 로그인 시 비번을 새로 설정(현재 비번 없이 임시→신규).
// 현재 비밀번호 검증은 verifyLogin 으로 이미 통과한 임시 비번 흐름 전제.
export async function firstLoginSetPassword(
  userId: string,
  next: string,
): Promise<{ ok: true } | { ok: false; code: string }> {
  if (!next || next.length < 8) return { ok: false, code: "WEAK_PASSWORD" };
  const user = await getUserById(userId);
  if (!user) return { ok: false, code: "NOT_FOUND" };
  if (!user.reset_required) return { ok: false, code: "NOT_REQUIRED" };
  await q(
    `UPDATE portfolio.users
       SET password_hash = $2, password_algo = 'scrypt', password_updated_at = now(),
           reset_required = false, updated_at = now()
     WHERE user_id = $1`,
    [userId, hashPassword(next)],
  );
  await revokeAllSessions(userId);
  await logAuthEvent(userId, "password_changed", true, "first_login");
  return { ok: true };
}

// ───────────────────────── 관리자 비번 리셋 ─────────────────────────
// 임시 비밀번호를 생성해 hash 만 저장 + reset_required=true. 평문 임시비번을 1회 반환(로그 금지).
export async function adminResetPassword(
  adminUserId: string,
  targetUserId: string,
): Promise<{ ok: true; tempPassword: string } | { ok: false; code: string }> {
  const target = await getUserById(targetUserId);
  if (!target) return { ok: false, code: "NOT_FOUND" };
  const tempPassword = randomBytes(9).toString("base64url"); // 12자 내외, 1회용
  await q(
    `UPDATE portfolio.users
       SET password_hash = $2, password_algo = 'scrypt', password_updated_at = now(),
           reset_required = true,
           status = CASE WHEN status = 'locked' THEN 'active' ELSE status END,
           failed_logins = 0, updated_at = now()
     WHERE user_id = $1`,
    [targetUserId, hashPassword(tempPassword)],
  );
  await revokeAllSessions(targetUserId);
  await logAuthEvent(adminUserId, "admin_password_reset", true, `target=${targetUserId}`);
  return { ok: true, tempPassword };
}

// 계정 활성/비활성 토글.
export async function setUserStatus(
  adminUserId: string,
  targetUserId: string,
  status: UserStatus,
): Promise<{ ok: true } | { ok: false; code: string }> {
  const target = await getUserById(targetUserId);
  if (!target) return { ok: false, code: "NOT_FOUND" };
  await q(
    `UPDATE portfolio.users SET status = $2, updated_at = now() WHERE user_id = $1`,
    [targetUserId, status],
  );
  if (status === "disabled") await revokeAllSessions(targetUserId);
  await logAuthEvent(adminUserId, status === "disabled" ? "login_failed" : "login_success", true, `status_set=${status} target=${targetUserId}`);
  return { ok: true };
}

// ───────────────────────── 비번 재설정 토큰 ─────────────────────────
// 토큰 원문은 반환만 하고 저장은 hash. 계정 존재여부를 응답으로 노출하지 않는다(호출측 책임).
export async function createResetToken(email: string): Promise<string | null> {
  const user = await getUserByEmail(email);
  if (!user) {
    // 존재하지 않아도 동일 처리 시간 흐름(대략) — 응답은 호출측에서 항상 동일하게.
    await logAuthEvent(null, "password_reset_requested", false, "no_such_user");
    return null;
  }
  // 간단 rate limit: 최근 5분 내 미사용 토큰이 3개 이상이면 추가 발급 거부.
  const recent = await q<{ cnt: string }>(
    `SELECT count(*)::text AS cnt FROM portfolio.password_reset_tokens
       WHERE user_id = $1 AND used_at IS NULL AND created_at > now() - interval '5 minutes'`,
    [user.user_id],
  );
  if (Number(recent.rows[0]?.cnt ?? 0) >= 3) {
    await logAuthEvent(user.user_id, "password_reset_requested", false, "rate_limited");
    return null;
  }
  const token = randomBytes(32).toString("hex"); // 원문(반환만)
  const tokenHash = sha256(token); // 저장
  const { ipHash, uaHash } = requestHashes();
  const ttlSec = Math.floor(RESET_TOKEN_TTL_MS / 1000);
  await q(
    `INSERT INTO portfolio.password_reset_tokens
       (user_id, token_hash, expires_at, requested_ip_hash, requested_user_agent_hash)
     VALUES ($1, $2, now() + ($3 || ' seconds')::interval, $4, $5)`,
    [user.user_id, tokenHash, String(ttlSec), ipHash, uaHash],
  );
  await logAuthEvent(user.user_id, "password_reset_requested", true, null);
  return token;
}

// 토큰 소비 — hash 매칭 + 만료/used 검사 + 1회용. 성공 시 비번 교체 + 세션 무효화.
export async function consumeResetToken(
  token: string,
  newPassword: string,
): Promise<{ ok: true } | { ok: false; code: string }> {
  if (!newPassword || newPassword.length < 8) return { ok: false, code: "WEAK_PASSWORD" };
  const tokenHash = sha256(token);
  const res = await q<{ token_id: string; user_id: string }>(
    `SELECT token_id, user_id FROM portfolio.password_reset_tokens
       WHERE token_hash = $1 AND used_at IS NULL AND expires_at > now()
       LIMIT 1`,
    [tokenHash],
  );
  const row = res.rows[0];
  if (!row) return { ok: false, code: "INVALID_OR_EXPIRED" };

  // 1회용: used_at 을 원자적으로 표시(동시 소비 방지).
  const claim = await q(
    `UPDATE portfolio.password_reset_tokens
       SET used_at = now()
     WHERE token_id = $1 AND used_at IS NULL
     RETURNING token_id`,
    [row.token_id],
  );
  if (claim.rowCount === 0) return { ok: false, code: "INVALID_OR_EXPIRED" };

  await q(
    `UPDATE portfolio.users
       SET password_hash = $2, password_algo = 'scrypt', password_updated_at = now(),
           reset_required = false,
           status = CASE WHEN status = 'locked' THEN 'active' ELSE status END,
           failed_logins = 0, updated_at = now()
     WHERE user_id = $1`,
    [row.user_id, hashPassword(newPassword)],
  );
  await revokeAllSessions(row.user_id);
  await logAuthEvent(row.user_id, "password_reset_completed", true, null);
  return { ok: true };
}

// ───────────────────────── 관리자 목록 ─────────────────────────
export async function listUsers(): Promise<SafeUser[]> {
  const res = await q<UserRow>(
    `SELECT * FROM portfolio.users ORDER BY created_at`,
  );
  return res.rows.map(toSafeUser);
}

export { sha256 as authSha256 };
