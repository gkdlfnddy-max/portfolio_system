// Track K — admin dashboard / 계좌 RBAC 자가 점검.
//   1) 정적 가드 검증(항상): /api/admin/dashboard 가 requireAdmin 으로 가드되고,
//      /api/accounts/[id]/history-series, /advice-history 가 계좌 RBAC(requireLoginAndAccount)로 가드됨.
//   2) 라이브 HTTP 검증(옵션): BASE_URL(+선택적 쿠키) 주어지면 실제 403 응답 확인.
//      - 비admin / 무세션 → /api/admin/dashboard 401 또는 403
//      - user 세션(NONADMIN_COOKIE) + 접근권한 없는 계좌 → history-series 403
// 평문 자격증명/쿠키는 출력하지 않는다. PASS/FAIL 만.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(__dirname, "..");

let failed = 0;
const check = (name, cond, extra = "") => {
  console.log(`${cond ? "PASS" : "FAIL"}: ${name}${extra ? " — " + extra : ""}`);
  if (!cond) failed++;
};

// ── 1) 정적 가드 검증 ──
function readSrc(rel) {
  try {
    return readFileSync(path.join(webRoot, rel), "utf8");
  } catch {
    return "";
  }
}

const dash = readSrc("app/api/admin/dashboard/route.ts");
check("admin/dashboard route exists", dash.length > 0);
check("admin/dashboard guarded by requireAdmin", /requireAdmin\s*\(/.test(dash));
check("admin/dashboard returns Denied on non-admin (isDenied → return)", /isDenied\(admin\)\)\s*return\s+admin/.test(dash));
check("admin/dashboard reads operational DB only (no anthropic/mock)", !/anthropic|ANTHROPIC|Math\.random|mockSeries|fakeChart/i.test(dash));

// 계좌 RBAC 가드 = 로그인+계좌접근 검사. 정규 순서 도입 후 결합 헬퍼
// (requireAccountAccessAndUnlocked/AndReauth)도 내부에서 requireLoginAndAccount 를 호출하므로 동등하게 인정.
const RBAC_GUARD = /require(LoginAndAccount|AccountAccess(AndUnlocked|AndReauth)?)\s*\(/;
const hist = readSrc("app/api/accounts/[id]/history-series/route.ts");
check("history-series route exists", hist.length > 0);
check("history-series guarded by account RBAC (login+account)", RBAC_GUARD.test(hist));

const advice = readSrc("app/api/accounts/[id]/advice-history/route.ts");
check("advice-history route exists", advice.length > 0);
check("advice-history guarded by account RBAC", RBAC_GUARD.test(advice));

// ── 2) 라이브 HTTP 검증 (옵션) ──
const BASE = process.env.BASE_URL;
if (BASE) {
  const adminUrl = `${BASE.replace(/\/$/, "")}/api/admin/dashboard`;
  // (a) 무세션(쿠키 없음) → admin dashboard 401/403.
  try {
    const r = await fetch(adminUrl, { headers: { "cache-control": "no-store" } });
    check("[live] no-session → admin/dashboard denied (401/403)", r.status === 401 || r.status === 403, `status=${r.status}`);
  } catch (e) {
    check("[live] admin/dashboard reachable", false, e?.message ?? "?");
  }

  // (b) 비admin user 세션 → admin dashboard 403.
  const userCookie = process.env.NONADMIN_COOKIE;
  if (userCookie) {
    try {
      const r = await fetch(adminUrl, { headers: { cookie: userCookie } });
      check("[live] non-admin user → admin/dashboard 403", r.status === 403, `status=${r.status}`);
    } catch (e) {
      check("[live] non-admin admin/dashboard call ok", false, e?.message ?? "?");
    }

    // (c) 비admin user → 접근권한 없는 타 계좌 history-series 403.
    const otherIdx = process.env.OTHER_ACCOUNT_INDEX ?? "999";
    const histUrl = `${BASE.replace(/\/$/, "")}/api/accounts/${otherIdx}/history-series?days=30`;
    try {
      const r = await fetch(histUrl, { headers: { cookie: userCookie } });
      check("[live] non-admin → other account history-series 403", r.status === 403, `status=${r.status}`);
    } catch (e) {
      check("[live] history-series call ok", false, e?.message ?? "?");
    }

    // (d) admin 쿠키 주어지면 admin dashboard 200.
    const adminCookie = process.env.ADMIN_COOKIE;
    if (adminCookie) {
      try {
        const r = await fetch(adminUrl, { headers: { cookie: adminCookie } });
        check("[live] admin → admin/dashboard 200", r.status === 200, `status=${r.status}`);
      } catch (e) {
        check("[live] admin admin/dashboard call ok", false, e?.message ?? "?");
      }
    } else {
      console.log("INFO: ADMIN_COOKIE 미설정 → admin 200 검증 건너뜀.");
    }
  } else {
    console.log("INFO: NONADMIN_COOKIE 미설정 → user 세션 403 검증 건너뜀(무세션 검증만 실행).");
  }
} else {
  console.log("INFO: BASE_URL 미설정 → 라이브 HTTP 검증 건너뜀(정적 가드 검증만 실행).");
}

if (failed > 0) {
  console.log(`\n${failed} check(s) FAILED.`);
  process.exit(1);
}
console.log("\nALL PASS.");
