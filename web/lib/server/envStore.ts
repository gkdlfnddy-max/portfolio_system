// 서버 전용 — 루트 .env 의 KIS 계좌 정보 읽기/기록.
// 계좌별: KIS_ACCOUNT_{n}_ALIAS/MODE/APP_KEY/APP_SECRET/ACCOUNT_NO/PRODUCT_CODE
// 비밀값은 .env 에만 저장. 목록 조회 시 마스킹해서 반환 (브라우저로 원문 노출 금지).
import { promises as fs } from "fs";
import path from "path";

// next start/dev 의 cwd 는 web/ → 루트 .env 는 상위.
const ENV_PATH = path.resolve(process.cwd(), "..", ".env");

export type AccountMode = "mock" | "paper" | "live";

export type KisAccountInput = {
  alias: string;
  mode: AccountMode;
  appKey: string;
  appSecret: string;
  accountNo: string;
  productCode: string;
};

export type KisAccountView = {
  index: number;
  alias: string;
  mode: string;
  accountNoMasked: string;
  hasCredentials: boolean;
};

// 키움 자격증명 입력 — KIS 와 *분리된* 변수에 저장(혼용 금지).
export type KiwoomAccountInput = {
  alias: string;
  mode: AccountMode;
  appKey: string;
  appSecret: string;
  accountNo: string;
};

async function readEnvText(): Promise<string> {
  try {
    return await fs.readFile(ENV_PATH, "utf8");
  } catch {
    return "";
  }
}

function parseEnv(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const i = t.indexOf("=");
    if (i < 0) continue;
    out[t.slice(0, i).trim()] = t.slice(i + 1).trim();
  }
  return out;
}

export async function readAccount(index: number): Promise<KisAccountView | null> {
  const all = await readAccounts();
  return all.find((a) => a.index === index) ?? null;
}

export async function readAccounts(): Promise<KisAccountView[]> {
  const env = parseEnv(await readEnvText());
  const accounts: KisAccountView[] = [];
  for (let n = 1; n <= 50; n++) {
    const alias = env[`KIS_ACCOUNT_${n}_ALIAS`];
    const key = env[`KIS_ACCOUNT_${n}_APP_KEY`];
    const kiwoomKey = env[`KIWOOM_ACCOUNT_${n}_APP_KEY`];
    if (alias === undefined && key === undefined && kiwoomKey === undefined) continue;
    // broker 별 자격증명은 분리된 변수에서 확인(혼용 금지). kiwoom 계좌는 KIWOOM_* 사용.
    const broker = (env[`KIS_ACCOUNT_${n}_BROKER`] || "kis").trim().toLowerCase();
    const acc = env[`KIS_ACCOUNT_${n}_ACCOUNT_NO`] ?? env[`KIWOOM_ACCOUNT_${n}_ACCOUNT_NO`] ?? "";
    const hasCredentials =
      broker === "kiwoom"
        ? !!(kiwoomKey && env[`KIWOOM_ACCOUNT_${n}_APP_SECRET`])
        : !!(key && env[`KIS_ACCOUNT_${n}_APP_SECRET`]);
    accounts.push({
      index: n,
      alias: alias || `계좌 ${n}`,
      mode: env[`KIS_ACCOUNT_${n}_MODE`] || env[`KIWOOM_ACCOUNT_${n}_MODE`] || "paper",
      accountNoMasked: acc ? acc.slice(0, 2) + "******" : "(미입력)",
      hasCredentials,
    });
  }
  return accounts;
}

function setVar(text: string, key: string, value: string): string {
  const re = new RegExp(`^${key}=.*$`, "m");
  if (re.test(text)) return text.replace(re, `${key}=${value}`);
  return text.replace(/\s*$/, "") + `\n${key}=${value}\n`;
}

export async function addAccount(inp: KisAccountInput): Promise<number> {
  let text = await readEnvText();
  const existing = await readAccounts();
  const n = existing.reduce((m, a) => Math.max(m, a.index), 0) + 1;

  let block = `\n# --- KIS account ${n} (${inp.alias}) — 웹에서 기록 ---\n`;
  const fields: [string, string][] = [
    ["ALIAS", inp.alias],
    ["MODE", inp.mode],
    ["APP_KEY", inp.appKey],
    ["APP_SECRET", inp.appSecret],
    ["ACCOUNT_NO", inp.accountNo],
    ["PRODUCT_CODE", inp.productCode || "01"],
  ];
  for (const [k, v] of fields) block += `KIS_ACCOUNT_${n}_${k}=${v}\n`;
  text = text.replace(/\s*$/, "") + "\n" + block;

  // 방금 추가한 계좌를 활성(primary)으로 미러 → python kis_check 가 바로 사용.
  text = setVar(text, "KIS_MODE", inp.mode);
  text = setVar(text, "KIS_APP_KEY", inp.appKey);
  text = setVar(text, "KIS_APP_SECRET", inp.appSecret);
  text = setVar(text, "KIS_ACCOUNT_NO", inp.accountNo);
  text = setVar(text, "KIS_ACCOUNT_PRODUCT_CODE", inp.productCode || "01");

  await fs.writeFile(ENV_PATH, text, "utf8");
  return n;
}

// 키움 계좌 추가. KIS 와 *분리된* KIWOOM_* 변수에 자격증명 기록(혼용 금지).
// account_index 는 KIS 와 공유(accounts 테이블/sync_job 인덱싱) — 그래서 메타(ALIAS/MODE/
// ACCOUNT_NO/BROKER)는 KIS_ACCOUNT_{n}_* 에도 기록하되, APP_KEY/APP_SECRET 같은 *비밀*은
// KIWOOM_* 에만 둔다(KIS_ACCOUNT_{n}_APP_KEY/_SECRET 에 절대 기록하지 않음).
export async function addKiwoomAccount(inp: KiwoomAccountInput): Promise<number> {
  let text = await readEnvText();
  const existing = await readAccounts();
  const n = existing.reduce((m, a) => Math.max(m, a.index), 0) + 1;

  let block = `\n# --- Kiwoom account ${n} (${inp.alias}) — 웹에서 기록 (KIS 와 분리) ---\n`;
  // broker 메타 + account_no 는 공유 인덱스(KIS_ACCOUNT_{n}_*)에 — sync_job/factory 가 읽음.
  // BROKER=kiwoom 으로 factory 가 키움 adapter 를 주입한다. (비밀은 여기에 두지 않음)
  const meta: [string, string][] = [
    ["BROKER", "kiwoom"],
    ["ALIAS", inp.alias],
    ["MODE", inp.mode],
    ["ACCOUNT_NO", inp.accountNo],
  ];
  for (const [k, v] of meta) block += `KIS_ACCOUNT_${n}_${k}=${v}\n`;
  // 키움 *비밀*은 KIWOOM_* 전용 변수에만 — kiwoom_adapter 가 읽는 계약.
  const cred: [string, string][] = [
    ["APP_KEY", inp.appKey],
    ["APP_SECRET", inp.appSecret],
    ["ACCOUNT_NO", inp.accountNo],
    ["MODE", inp.mode],
  ];
  for (const [k, v] of cred) block += `KIWOOM_ACCOUNT_${n}_${k}=${v}\n`;
  text = text.replace(/\s*$/, "") + "\n" + block;

  await fs.writeFile(ENV_PATH, text, "utf8");
  return n;
}
