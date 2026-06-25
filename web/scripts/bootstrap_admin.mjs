// 첫 admin 부트스트랩 — 1회 실행. 일반 signup 은 role='user' 고정이므로 admin 은 여기서만 생성.
// 사용법:
//   BOOTSTRAP_ADMIN_EMAIL=a@b.com BOOTSTRAP_ADMIN_PW='...' node scripts/bootstrap_admin.mjs
//   또는: node scripts/bootstrap_admin.mjs <email> <password>
// 보안: 비밀번호 평문을 출력/로그하지 않는다. hash 만 저장. 이미 admin 이 있으면 거부(자가승격 방지).
import { randomBytes, scryptSync } from "node:crypto";

const KEYLEN = 64;
function hashPassword(password) {
  const salt = randomBytes(16).toString("hex");
  const hash = scryptSync(password, salt, KEYLEN).toString("hex");
  return `${salt}:${hash}`;
}

function die(msg) {
  console.error("ERROR: " + msg);
  process.exit(1);
}

const email = (process.env.BOOTSTRAP_ADMIN_EMAIL ?? process.argv[2] ?? "").trim().toLowerCase();
const password = process.env.BOOTSTRAP_ADMIN_PW ?? process.argv[3] ?? "";
const displayName = (process.env.BOOTSTRAP_ADMIN_NAME ?? "Admin").trim();

if (!email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) die("유효한 이메일이 필요합니다(BOOTSTRAP_ADMIN_EMAIL).");
if (!password || password.length < 8) die("비밀번호는 8자 이상이어야 합니다(BOOTSTRAP_ADMIN_PW).");
if (!process.env.DATABASE_URL) die("DATABASE_URL 미설정.");

const { Pool } = await import("pg");
const pool = new Pool({ connectionString: process.env.DATABASE_URL, max: 1 });

try {
  const existing = await pool.query("SELECT count(*)::int AS c FROM portfolio.users WHERE role = 'admin'");
  if (existing.rows[0].c > 0) die("이미 admin 이 존재합니다. 부트스트랩은 1회만 허용됩니다.");

  const dup = await pool.query("SELECT 1 FROM portfolio.users WHERE email = $1", [email]);
  if (dup.rowCount > 0) die("해당 이메일이 이미 등록되어 있습니다.");

  await pool.query(
    `INSERT INTO portfolio.users (email, display_name, password_hash, password_algo, role, status, reset_required)
     VALUES ($1, $2, $3, 'scrypt', 'admin', 'active', false)`,
    [email, displayName, hashPassword(password)],
  );
  await pool.query(
    `INSERT INTO portfolio.user_auth_events (user_id, event_type, success, reason)
     SELECT user_id, 'signup', true, 'bootstrap_admin' FROM portfolio.users WHERE email = $1`,
    [email],
  );
  console.log(`OK: admin 계정 생성됨 (${email}). 비밀번호는 출력하지 않습니다.`);
} catch (e) {
  die(e?.message ?? "unknown");
} finally {
  await pool.end();
}
