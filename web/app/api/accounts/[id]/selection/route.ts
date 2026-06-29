import { NextResponse } from "next/server";
import { execFile } from "child_process";
import { promisify } from "util";
import path from "path";

import { requireAccountUnlocked } from "@/lib/auth/account";
import { requireAccountAccessAndUnlocked } from "@/lib/auth/rbac";

// 종목/ETF 선정 화면 데이터 라우트 (조회 전용).
// - RBAC: 로그인(401) → 계좌 접근권한(403) → 앱 PIN(401).
// - 백엔드 CLI 를 spawn 하여 실 DB 기반 데이터를 가져온다(웹은 조회 전용·하드코딩 0).
//   · 방어 구성: bond_bucket --account N
//       → { ok, cash_band, breakdown:{ pure_cash_pct, govbond_pct, short_govbond_pct,
//           long_govbond_pct, risk_asset_pct, duration_pref, ... }, govbond_etf_candidates:[...] }
//   · bucket 비교: security_selection --account N --compare <bucket>
//       → { ok, bucket, label, kind, candidate_count, comparison:[{ ticker, name,
//           asset_class, data_availability{...}, cost, volatility, view_fit, confidence, ... }] }
// - 위 CLI/서브커맨드가 아직 없으면 graceful "준비 중"(pending) 으로 응답한다(에러 페이지 X).
// - 자동 주문·policy 적용 없음: 이 라우트는 GET(조회)만 제공한다.
export const dynamic = "force-dynamic";

const pexec = promisify(execFile);

// security_selection 의 bucket 키와 정확히 일치해야 한다(--buckets 로 확인).
const BUCKETS = ["global_core", "robotics", "semiconductor", "semiconductor_inverse", "treasury"] as const;
type Bucket = (typeof BUCKETS)[number];

function accId(id: string): number | null {
  const n = parseInt(id, 10);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

// CLI 가 아직 없거나 서브커맨드 미지원 → "준비 중" 으로 처리하기 위한 신호 판별.
function isPendingError(e: any): boolean {
  const s = `${e?.stderr ?? ""}${e?.stdout ?? ""}${e?.message ?? ""}`;
  return (
    /No module named/i.test(s) ||
    /unrecognized arguments/i.test(s) ||
    /invalid choice/i.test(s) ||
    /error: argument/i.test(s) ||
    /usage:/i.test(s)
  );
}

// 백엔드 모듈 실행. .venv 우선(기존 pexec 패턴).
async function runPy(mod: string, args: string[]) {
  const root = path.resolve(process.cwd(), "..");
  let lastErr: any = null;
  for (const py of [path.resolve(root, ".venv", "bin", "python"), "python", "python3", "py"]) {
    try {
      const { stdout } = await pexec(py, ["-m", `main_mission.portfolio_os.${mod}`, ...args], {
        cwd: root,
        timeout: 30000,
        env: { ...process.env, PYTHONIOENCODING: "utf-8" },
        maxBuffer: 4 * 1024 * 1024,
      });
      // CLI 는 한 줄/여러 줄(pretty) JSON 을 출력한다. 전체 stdout 을 우선 파싱하고,
      // 앞에 로그 라인이 섞여 있으면 마지막 JSON 객체({...})를 추출해 파싱한다.
      const text = stdout.trim();
      try {
        return JSON.parse(text);
      } catch {
        const start = text.indexOf("{");
        const end = text.lastIndexOf("}");
        if (start >= 0 && end > start) return JSON.parse(text.slice(start, end + 1));
        throw new Error("CLI JSON 파싱 실패");
      }
    } catch (e: any) {
      if (e?.code === "ENOENT" && !e?.stderr) {
        lastErr = e;
        continue; // 해당 python 인터프리터 없음 → 다음 후보
      }
      throw e; // 모듈/인자 오류 등은 그대로 던진다(상위에서 pending 판별)
    }
  }
  throw lastErr ?? new Error("python 미발견");
}

// 한 단계(섹션)를 안전하게 실행: 성공 시 데이터, 미구현/오류 시 pending 마킹.
async function section(mod: string, args: string[]): Promise<{ ready: boolean; data: any | null; note?: string }> {
  try {
    const data = await runPy(mod, args);
    return { ready: true, data };
  } catch (e: any) {
    if (isPendingError(e)) {
      return { ready: false, data: null, note: "백엔드 준비 중(미연동)" };
    }
    return { ready: false, data: null, note: e?.message ?? "조회 실패" };
  }
}

export async function GET(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });

  // RBAC: 미로그인 401 / 타계좌 403 / PIN 미해제 401.
  const az = await requireAccountAccessAndUnlocked(id);
  if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;

  // Step 1: 방어자산 내부 구성(순현금/단기국채/장기국채). bond_bucket --account N (인자 없음).
  // Step 1(추가): 금리 동향 기반 국채 비중·듀레이션 추천. bond_recommendation --account N.
  //   → { rate_regime, suggested_bond_ratio_pct, suggested_duration, suggested_split,
  //       ladder, rationale[], data_source, confidence, requires_user_approval }
  // Step 1(추천형 흐름): 국채 비중 후보 A/B/C/D. bond_recommendation --account N --options.
  //   → { ok, options:[{ id, label, bond_ratio_pct, full_equiv:{ pure_cash_pct,
  //       short_govbond_pct, long_govbond_pct, govbond_pct, risk_asset_pct },
  //       rationale[], rising_rate_risk, falling_rate_benefit, fx_risk, liquidity,
  //       account_fit, confidence, system_recommended }], rate_regime, data_source,
  //       requires_user_approval, ... }  (전체환산·근거·system_recommended 강조)
  //   CLI 미구현이면 graceful "준비 중"(pending). 추천일 뿐 — 자동 적용·주문 0.
  // Step 1(국채 ETF 비교): govbond_etf --account N (비교가 기본 출력 — 후보별 역할/장점/리스크/
  //   거시·계좌 적합성/추천강도/데이터품질/대안/제외). 병렬 작업 A 가 작성한 CLI이며, 인터페이스가
  //   아직 다르거나(예: --compare 미지원) 미배선이면 section() 이 graceful "준비 중"(pending) 처리한다.
  //   → { ok, rate_regime, account_purpose, candidates:[{ ticker, name, region, duration_bucket,
  //       role, pros[], risks[], macro_fit{label,reason}, purpose_fit{label,reason},
  //       recommendation_strength, data_quality:{ price, volume, expense_ratio, duration_years,
  //       data_available }, alternatives[] }], excluded[], long_bond_volatility_warning, ... }
  //   bond_bucket 의 govbond_etf_candidates(시드)와 별개의 풍부한 비교 데이터(가격·거래량 실연동 지향,
  //   보수율/듀레이션 미연동은 "미연동/unknown"으로 정직 표기 — mock 0).
  const [defensive, bondRec, bondOptions, govbondEtf] = await Promise.all([
    section("bond_bucket", ["--account", String(id)]),
    section("bond_recommendation", ["--account", String(id)]),
    section("bond_recommendation", ["--account", String(id), "--options"]),
    section("govbond_etf", ["--account", String(id)]),
  ]);

  // Step 2–5: bucket 별 후보 비교표. security_selection --account N --compare <bucket>.
  const compares = await Promise.all(
    BUCKETS.map(async (b) => {
      const r = await section("security_selection", ["--account", String(id), "--compare", b]);
      return [b, r] as const;
    }),
  );
  const buckets: Record<string, { ready: boolean; data: any | null; note?: string }> = {};
  for (const [b, r] of compares) buckets[b] = r;

  return NextResponse.json({
    ok: true,
    account_id: id,
    defensive, // Step 1
    bond_recommendation: bondRec, // Step 1 — 금리 기반 국채 추천(제안일 뿐, 미반영)
    bond_options: bondOptions, // Step 1 — 국채 비중 후보 A/B/C/D(추천일 뿐, 미반영)
    govbond_etf: govbondEtf, // Step 1 — 국채 ETF 후보 비교(역할/장점/리스크/거시·계좌 적합성/데이터품질, 미연동이면 pending)
    buckets, // Step 2–5
    bucket_order: BUCKETS,
    // 이 화면은 조회/초안 전용이며 자동 주문·policy 적용을 수행하지 않는다.
    readonly: true,
    auto_order: false,
  });
}

// POST: 후보 선택 → 비중 배분(draft 계산). **계산만** 수행하며 policy/주문에 반영하지 않는다.
//   본문: { picks: [{ bucket, ticker, ... }...], weighting?: "equal"|"view" }
//   호출(병렬 작업 A — weight_allocator):
//     weight_allocator --account N --picks '{"bucket":["ticker",...]}' [--weighting equal|view]
//       → { holdings:[{ticker,name?,weight_pct,bucket,...}], bucket_summary:[{key,weight_pct,
//           allocated_pct,headroom_pct,picks}], over_limit_warnings:[{level,bucket,msg,...}],
//           total_pct, total_is_100, blocked, db_write:false, auto_order_created:false }
//     weight_allocator --account N --individual-options
//       → { options:{A,B,C}, risk_asset_pct, ... }  (개별주 A/B/C 옵션 = 위험자산 안 carve)
//   CLI/서브커맨드가 아직 없거나 다르면 graceful "준비 중"(pending) 으로 응답(에러 X, 화면 안 깨짐).
//   자동 주문/적용 0: 결과는 화면 draft 비중일 뿐, 어떤 테이블에도 쓰지 않는다(db_write=false).
export async function POST(req: Request, { params }: { params: { id: string } }) {
  const id = accId(params.id);
  if (id === null) return NextResponse.json({ ok: false, error: "invalid id" }, { status: 400 });

  // RBAC: 미로그인 401 / 타계좌 403 / PIN 미해제 401 (GET 과 동일 게이트).
  const az = await requireAccountAccessAndUnlocked(id);
  if (az) return az;
  const ag = await requireAccountUnlocked(id, req);
  if (ag) return ag;

  let body: any = {};
  try {
    body = await req.json();
  } catch {
    body = {};
  }
  const rawPicks = Array.isArray(body?.picks) ? body.picks : [];
  const weighting = ["equal", "view"].includes(String(body?.weighting)) ? String(body.weighting) : "equal";
  // 개별주 carve 옵션(none|5|10) — allocate 가 위험자산에서 떼어 picks['individual'] 에 균등 배분.
  const equityOption = ["none", "5", "10"].includes(String(body?.equity_option)) ? String(body.equity_option) : "none";

  // picks 를 weight_allocator 가 받는 형태 {bucket: [ticker, ...]} 로 변환.
  //   asset_class=stock(개별주)는 'individual' 키로 모은다(carve 대상). ETF/앵커는 자기 bucket.
  const picksByBucket: Record<string, string[]> = {};
  for (const p of rawPicks) {
    if (!p || !p.ticker || !p.bucket) continue;
    const t = String(p.ticker);
    const ac = String(p.asset_class ?? "");
    const b = ac === "stock" ? "individual" : String(p.bucket);
    (picksByBucket[b] ||= []);
    if (!picksByBucket[b].includes(t)) picksByBucket[b].push(t);
  }

  // 선택이 없으면 배분 CLI 는 생략하되, 개별주 A/B/C 옵션은 항상 조회(선택과 무관한 정보).
  const wantAlloc = Object.keys(picksByBucket).length > 0;

  const [alloc, options] = await Promise.all([
    wantAlloc
      ? section("weight_allocator", [
          "--account", String(id),
          "--picks", JSON.stringify(picksByBucket),
          "--weighting", weighting,
          "--equity-option", equityOption,
        ])
      : Promise.resolve({ ready: false, data: null, note: "선택된 후보가 없습니다 — 후보를 고르면 확정안 한도 안에서 draft 비중을 계산합니다." }),
    section("weight_allocator", ["--account", String(id), "--individual-options"]),
  ]);

  return NextResponse.json({
    ok: true,
    account_id: id,
    ready: alloc.ready,
    data: alloc.data,
    note: alloc.note,
    individual_options: options, // 개별주 A/B/C 옵션(있으면 실수치, 없으면 pending)
    // 계산 전용. 자동 주문·policy 적용 없음(승인 전 미반영).
    readonly: true,
    auto_order: false,
  });
}
