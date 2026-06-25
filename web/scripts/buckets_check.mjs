// buckets_check — 차트(Track3) 데이터 불변식 가드.
// buckets.ts 는 TS 라 직접 import 가 어려워, toBuckets 의 분류 규칙을 최소 복제해
// 같은 불변식을 검증한다(합계/방어 정의/채권 0% 표시/테마·헤지 분리).
// 규칙이 바뀌면 lib/allocation/buckets.ts 와 함께 이 파일도 갱신해야 한다.
// PASS/FAIL 만 출력한다.

const round1 = (n) => Math.round(n * 10) / 10;

// classify: kind → bucket_type (buckets.ts classify 미러)
function classify(r) {
  if (r.kind === "cash") return "pure_cash";
  if (r.kind === "bond") return "bond";
  if (r.kind === "anchor") return "core_etf";
  if (r.kind === "tilt") return "theme";
  if (r.kind === "hedge") return "hedge";
  return "other";
}

// toBuckets 미러: rows → buckets, 채권 0% 항상 보장.
function toBuckets(rows) {
  const buckets = rows.map((r) => ({ bucket_type: classify(r), pct: r.weight_pct }));
  if (!buckets.some((b) => b.bucket_type === "bond")) {
    const ci = buckets.findIndex((b) => b.bucket_type === "pure_cash");
    const bondB = { bucket_type: "bond", pct: 0 };
    if (ci >= 0) buckets.splice(ci + 1, 0, bondB);
    else buckets.push(bondB);
  }
  return buckets;
}

const sumPct = (b) => round1(b.reduce((a, x) => a + x.pct, 0));
const defensivePct = (b) =>
  round1(
    b
      .filter((x) => x.bucket_type === "pure_cash" || x.bucket_type === "bond")
      .reduce((a, x) => a + x.pct, 0)
  );
const riskPct = (b) => round1(100 - defensivePct(b));

function fail(msg) {
  console.log("FAIL: " + msg);
  process.exit(1);
}

// Case A: 채권 입력 없음 — bond 0% 가 자동 삽입되어야 한다.
{
  const rows = [
    { kind: "cash", ref: null, weight_pct: 30 },
    { kind: "anchor", ref: "global", weight_pct: 40 },
    { kind: "tilt", ref: "AI", weight_pct: 20 },
    { kind: "hedge", ref: "inverse", weight_pct: 10 },
  ];
  const b = toBuckets(rows);
  if (sumPct(b) !== 100) fail("A sum !== 100 (got " + sumPct(b) + ")");
  const bond = b.find((x) => x.bucket_type === "bond");
  if (!bond) fail("A bond bucket missing when input has no bond");
  if (bond.pct !== 0) fail("A inserted bond pct !== 0");
  if (defensivePct(b) !== 30) fail("A defensive should be cash+bond=30 (got " + defensivePct(b) + ")");
  if (riskPct(b) !== 70) fail("A risk should be 70 (got " + riskPct(b) + ")");
  // 테마와 헤지는 서로 다른 slice 여야 한다.
  if (!b.some((x) => x.bucket_type === "theme")) fail("A theme slice missing");
  if (!b.some((x) => x.bucket_type === "hedge")) fail("A hedge slice missing");
  if (b.filter((x) => x.bucket_type === "theme" || x.bucket_type === "hedge").length !== 2)
    fail("A theme/hedge not separated into 2 distinct slices");
  // bond 0% 는 cash 바로 뒤에 위치.
  const ci = b.findIndex((x) => x.bucket_type === "pure_cash");
  if (b[ci + 1].bucket_type !== "bond") fail("A bond not inserted right after pure_cash");
}

// Case B: 채권 입력 있음 — 방어 = cash + bond.
{
  const rows = [
    { kind: "cash", ref: null, weight_pct: 20 },
    { kind: "bond", ref: "KTB", weight_pct: 15 },
    { kind: "anchor", ref: "global", weight_pct: 50 },
    { kind: "tilt", ref: "robotics", weight_pct: 15 },
  ];
  const b = toBuckets(rows);
  if (sumPct(b) !== 100) fail("B sum !== 100 (got " + sumPct(b) + ")");
  const bonds = b.filter((x) => x.bucket_type === "bond");
  if (bonds.length !== 1) fail("B should keep single existing bond (no duplicate insert)");
  if (defensivePct(b) !== 35) fail("B defensive should be cash20+bond15=35 (got " + defensivePct(b) + ")");
  if (riskPct(b) !== 65) fail("B risk should be 65 (got " + riskPct(b) + ")");
}

// Case C: 보수/기준/공격 3안 — 합계 100 & 방어+위험=100 불변.
{
  const presets = {
    보수: [
      { kind: "cash", ref: null, weight_pct: 40 },
      { kind: "bond", ref: "KTB", weight_pct: 20 },
      { kind: "anchor", ref: "global", weight_pct: 30 },
      { kind: "tilt", ref: "AI", weight_pct: 10 },
    ],
    기준: [
      { kind: "cash", ref: null, weight_pct: 20 },
      { kind: "anchor", ref: "global", weight_pct: 50 },
      { kind: "tilt", ref: "AI", weight_pct: 25 },
      { kind: "hedge", ref: "inverse", weight_pct: 5 },
    ],
    공격: [
      { kind: "cash", ref: null, weight_pct: 10 },
      { kind: "anchor", ref: "global", weight_pct: 40 },
      { kind: "tilt", ref: "AI", weight_pct: 30 },
      { kind: "tilt", ref: "robotics", weight_pct: 20 },
    ],
  };
  for (const [name, rows] of Object.entries(presets)) {
    const b = toBuckets(rows);
    if (sumPct(b) !== 100) fail("C[" + name + "] sum !== 100 (got " + sumPct(b) + ")");
    if (round1(defensivePct(b) + riskPct(b)) !== 100)
      fail("C[" + name + "] defensive+risk !== 100");
    if (!b.some((x) => x.bucket_type === "bond")) fail("C[" + name + "] bond bucket missing in legend");
  }
}

console.log("PASS");
