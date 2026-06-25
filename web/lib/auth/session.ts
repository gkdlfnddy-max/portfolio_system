// 서버 세션 — httpOnly 쿠키(pos_session)에 opaque session_id 만 담는다(PIN/평문 없음).
// 세션 상태(만료/유휴/철회)는 portfolio.auth_sessions 행으로 서버에서 검증한다.
import { cookies, headers } from "next/headers";
import { randomBytes, createHash } from "node:crypto";
import { q } from "./db";
import { SESSION_COOKIE, SESSION_IDLE_MS, USER_ID } from "./config";

export type SessionRow = {
  id: number;
  session_id: string;
  user_id: string;
  unlocked_at: string;
  expires_at: string | null;
  last_seen_at: string;
  ip_hash: string | null;
  user_agent_hash: string | null;
  scope: string | null;
  revoked_at: string | null;
  created_at: string;
};

function sha256(v: string): string {
  return createHash("sha256").update(v).digest("hex");
}

// 헤더 값(IP/UA)은 원문 저장 금지 — sha256 해시만.
function requestHashes(): { ipHash: string | null; uaHash: string | null } {
  const h = headers();
  const fwd = h.get("x-forwarded-for") ?? "";
  const ip = fwd.split(",")[0]?.trim() || h.get("x-real-ip") || "";
  const ua = h.get("user-agent") ?? "";
  return {
    ipHash: ip ? sha256(ip) : null,
    uaHash: ua ? sha256(ua) : null,
  };
}

const cookieOpts = (maxAgeSec: number) => ({
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
  maxAge: maxAgeSec,
});

// 잠금 해제 성공 시 새 세션 생성. session_id 는 opaque 난수.
export async function createSession(scope = "full"): Promise<SessionRow> {
  const sessionId = randomBytes(32).toString("hex");
  const { ipHash, uaHash } = requestHashes();
  const idleSec = Math.floor(SESSION_IDLE_MS / 1000);

  const res = await q<SessionRow>(
    `INSERT INTO portfolio.auth_sessions
       (session_id, user_id, unlocked_at, expires_at, last_seen_at, ip_hash, user_agent_hash, scope, created_at)
     VALUES ($1, $2, now(), now() + ($3 || ' seconds')::interval, now(), $4, $5, $6, now())
     RETURNING *`,
    [sessionId, USER_ID, String(idleSec), ipHash, uaHash, scope],
  );

  cookies().set(SESSION_COOKIE, sessionId, cookieOpts(idleSec));
  return res.rows[0];
}

// 현재 쿠키 → 세션 행. 만료/철회/유휴 초과면 null.
export async function getSession(): Promise<SessionRow | null> {
  const sid = cookies().get(SESSION_COOKIE)?.value;
  if (!sid) return null;

  const res = await q<SessionRow>(
    `SELECT * FROM portfolio.auth_sessions WHERE session_id = $1 AND user_id = $2 LIMIT 1`,
    [sid, USER_ID],
  );
  const row = res.rows[0];
  if (!row) return null;
  if (row.revoked_at) return null;

  const now = Date.now();
  if (row.expires_at && Date.parse(row.expires_at) <= now) return null;
  // 유휴 검증: 마지막 활동이 SESSION_IDLE_MS 이내여야 한다.
  if (row.last_seen_at && now - Date.parse(row.last_seen_at) > SESSION_IDLE_MS) return null;

  return row;
}

// 활동 갱신 — last_seen_at / expires_at 슬라이딩.
export async function touch(sessionId: string): Promise<void> {
  const idleSec = Math.floor(SESSION_IDLE_MS / 1000);
  await q(
    `UPDATE portfolio.auth_sessions
       SET last_seen_at = now(), expires_at = now() + ($2 || ' seconds')::interval
     WHERE session_id = $1 AND revoked_at IS NULL`,
    [sessionId, String(idleSec)],
  );
}

// 재인증 — 민감 작업 직전 unlocked_at 갱신(재인증 창 리셋).
export async function markReauth(sessionId: string): Promise<void> {
  await q(
    `UPDATE portfolio.auth_sessions SET unlocked_at = now(), last_seen_at = now()
     WHERE session_id = $1 AND revoked_at IS NULL`,
    [sessionId],
  );
}

// 잠금/로그아웃 — 세션 철회 + 쿠키 제거.
export async function revoke(): Promise<void> {
  const sid = cookies().get(SESSION_COOKIE)?.value;
  if (sid) {
    await q(
      `UPDATE portfolio.auth_sessions SET revoked_at = now() WHERE session_id = $1 AND revoked_at IS NULL`,
      [sid],
    );
  }
  cookies().delete(SESSION_COOKIE);
}

export { sha256, requestHashes };
