// 인증 해시 자가 점검 — scryptSync 라운드트립 + 평문 누출 없음 확인.
// PIN 평문은 절대 출력하지 않는다. PASS/FAIL 만 출력.
import { randomBytes, scryptSync, timingSafeEqual } from "node:crypto";

const KEYLEN = 64;

function hashPin(pin) {
  const salt = randomBytes(16).toString("hex");
  const hash = scryptSync(pin, salt, KEYLEN).toString("hex");
  return { hash, salt, algo: "scrypt" };
}

function verifyPin(pin, hash, salt) {
  const expected = Buffer.from(hash, "hex");
  if (expected.length !== KEYLEN) return false;
  const actual = scryptSync(pin, salt, KEYLEN);
  return timingSafeEqual(actual, expected);
}

function fail(msg) {
  console.log("FAIL: " + msg);
  process.exit(1);
}

const PIN = randomBytes(3).readUIntBE(0, 3).toString().padStart(6, "0").slice(0, 6);
const WRONG = PIN === "000000" ? "111111" : "000000";

const { hash, salt, algo } = hashPin(PIN);

// 1) 올바른 PIN 검증.
if (!verifyPin(PIN, hash, salt)) fail("correct pin did not verify");
// 2) 틀린 PIN 거부.
if (verifyPin(WRONG, hash, salt)) fail("wrong pin verified");
// 3) 알고리즘 라벨.
if (algo !== "scrypt") fail("algo label mismatch");
// 4) 평문 누출 없음 — hash/salt 어디에도 PIN 평문이 들어있으면 안 됨.
if (hash.includes(PIN) || salt.includes(PIN)) fail("plaintext pin leaked into stored hash/salt");
// 5) 해시 길이.
if (Buffer.from(hash, "hex").length !== KEYLEN) fail("hash length wrong");

// 선택: DATABASE_URL 이 있으면 연결 가능 여부만 확인(쓰기 없음).
if (process.env.DATABASE_URL) {
  try {
    const { Pool } = await import("pg");
    const pool = new Pool({ connectionString: process.env.DATABASE_URL, max: 1 });
    await pool.query("SELECT 1");
    await pool.end();
    console.log("PASS (db reachable)");
  } catch {
    console.log("PASS (hash ok; db unreachable — skipped)");
  }
} else {
  console.log("PASS (hash ok; no DATABASE_URL — db check skipped)");
}
