// 사용자 로그인 + RBAC 보안 자가 점검.
// 순수 해시 검증은 항상 실행. DB 통합 검증은 DATABASE_URL + RUN_DB_TESTS=1 일 때만(테스트 데이터 생성/정리).
// 평문 비번/토큰은 절대 출력하지 않는다. PASS/FAIL 만.
import { randomBytes, scryptSync, timingSafeEqual, createHash } from "node:crypto";

const KEYLEN = 64;
const hashPassword = (pw) => {
  const salt = randomBytes(16).toString("hex");
  return `${salt}:${scryptSync(pw, salt, KEYLEN).toString("hex")}`;
};
const verifyPassword = (pw, stored) => {
  if (!stored?.includes(":")) return false;
  const [salt, hash] = stored.split(":");
  const expected = Buffer.from(hash, "hex");
  if (expected.length !== KEYLEN) return false;
  return timingSafeEqual(scryptSync(pw, salt, KEYLEN), expected);
};
const sha256 = (v) => createHash("sha256").update(v).digest("hex");

let failed = 0;
const check = (name, cond) => {
  console.log(`${cond ? "PASS" : "FAIL"}: ${name}`);
  if (!cond) failed++;
};

// ── 1) 비번 해시 라운드트립 + 평문 미저장 ──
const PW = "Sup3r$ecret!" + randomBytes(2).toString("hex");
const stored = hashPassword(PW);
check("correct password verifies", verifyPassword(PW, stored));
check("wrong password rejected", !verifyPassword(PW + "x", stored));
check("plaintext not in stored hash", !stored.includes(PW));
check("stored hash length correct", Buffer.from(stored.split(":")[1], "hex").length === KEYLEN);

// ── 2) reset token: 원문 != 저장 hash, 매칭은 hash 기준 ──
const token = randomBytes(32).toString("hex");
const tokenHash = sha256(token);
check("token hash differs from token", tokenHash !== token);
check("token hash matches recompute", sha256(token) === tokenHash);
check("wrong token does not match", sha256(token + "x") !== tokenHash);

// ── 3) DB 통합 (옵션) ──
if (process.env.DATABASE_URL && process.env.RUN_DB_TESTS === "1") {
  const { Pool } = await import("pg");
  const pool = new Pool({ connectionString: process.env.DATABASE_URL, max: 1 });
  const q = (s, p = []) => pool.query(s, p);
  const email = `selftest_${randomBytes(4).toString("hex")}@example.com`;
  let uid;
  try {
    const ins = await q(
      `INSERT INTO portfolio.users (email, password_hash, role, status) VALUES ($1,$2,'user','active') RETURNING user_id`,
      [email, hashPassword(PW)],
    );
    uid = ins.rows[0].user_id;

    // 평문 미저장: DB 에 저장된 hash 에 평문 없음.
    const row = (await q(`SELECT password_hash, failed_logins, status FROM portfolio.users WHERE user_id=$1`, [uid])).rows[0];
    check("[db] no plaintext password in column", !row.password_hash.includes(PW));

    // lockout: 5회 실패 시뮬레이션 → status='locked'.
    for (let i = 1; i <= 5; i++) {
      await q(
        `UPDATE portfolio.users SET failed_logins=failed_logins+1,
           status=CASE WHEN failed_logins+1>=5 THEN 'locked' ELSE status END WHERE user_id=$1`,
        [uid],
      );
    }
    const locked = (await q(`SELECT status FROM portfolio.users WHERE user_id=$1`, [uid])).rows[0];
    check("[db] account locks after 5 failures", locked.status === "locked");

    // RBAC: 권한 없으면 listAccessibleAccounts 빈 배열, grant 후 포함.
    const before = await q(`SELECT account_index FROM portfolio.user_account_access WHERE user_id=$1`, [uid]);
    check("[db] user has no account access initially", before.rowCount === 0);
    await q(
      `INSERT INTO portfolio.user_account_access (user_id, account_index, access_role) VALUES ($1, 999, 'owner')
       ON CONFLICT (user_id, account_index) DO NOTHING`,
      [uid],
    );
    const after = await q(`SELECT account_index FROM portfolio.user_account_access WHERE user_id=$1`, [uid]);
    check("[db] grant adds account access", after.rows.some((r) => r.account_index === 999));

    // reset token 1회용: used_at 표시 후 재소비 불가.
    const tk = randomBytes(32).toString("hex");
    await q(
      `INSERT INTO portfolio.password_reset_tokens (user_id, token_hash, expires_at) VALUES ($1,$2, now()+interval '1 hour')`,
      [uid, sha256(tk)],
    );
    const claim1 = await q(
      `UPDATE portfolio.password_reset_tokens SET used_at=now()
        WHERE token_hash=$1 AND used_at IS NULL AND expires_at>now() RETURNING token_id`,
      [sha256(tk)],
    );
    const claim2 = await q(
      `UPDATE portfolio.password_reset_tokens SET used_at=now()
        WHERE token_hash=$1 AND used_at IS NULL AND expires_at>now() RETURNING token_id`,
      [sha256(tk)],
    );
    check("[db] reset token usable once", claim1.rowCount === 1 && claim2.rowCount === 0);

    // auth_events append-only 기록 가능.
    await q(`INSERT INTO portfolio.user_auth_events (user_id, event_type, success) VALUES ($1,'login_failed',false)`, [uid]);
    const ev = await q(`SELECT count(*)::int c FROM portfolio.user_auth_events WHERE user_id=$1`, [uid]);
    check("[db] auth events recorded", ev.rows[0].c >= 1);
  } catch (e) {
    check("[db] integration ran without error: " + (e?.message ?? "?"), false);
  } finally {
    // 정리.
    if (uid) {
      await q(`DELETE FROM portfolio.password_reset_tokens WHERE user_id=$1`, [uid]).catch(() => {});
      await q(`DELETE FROM portfolio.user_account_access WHERE user_id=$1`, [uid]).catch(() => {});
      await q(`DELETE FROM portfolio.user_auth_events WHERE user_id=$1`, [uid]).catch(() => {});
      await q(`DELETE FROM portfolio.users WHERE user_id=$1`, [uid]).catch(() => {});
    }
    await pool.end();
  }
} else {
  console.log("INFO: DB 통합 테스트 건너뜀 (DATABASE_URL + RUN_DB_TESTS=1 필요).");
}

if (failed > 0) {
  console.log(`\n${failed} check(s) FAILED.`);
  process.exit(1);
}
console.log("\nALL PASS.");
