// kisGuard — KIS 호출 구조적 완화 (single-flight + short TTL cache + rate-limit 감지).
//
// 목적: 같은 계좌에서 자동 sync(AccountSync) ↔ 수동 연결 테스트(BrokerTestPanel) 가
//   동시/연속으로 KIS 를 때려 "초당 거래건수 초과(EGW00201)" 로 거부당하는 문제를
//   백오프 재시도(이미 python kis_client 에 있음) 외에 *호출 자체를 줄여* 근본 완화한다.
//
// 동작:
//   - single-flight: 같은 key 에 in-flight 호출이 있으면 새 KIS 호출을 띄우지 않고
//     그 Promise 를 공유한다 (동시 요청 = 1 KIS 호출).
//   - short TTL cache: 직전 성공 결과를 N초간 캐시 → 연속 요청은 KIS 재호출 없이 캐시 반환.
//   - rate-limit 감지: KIS 의 EGW00201 / "초당 거래건수" / HTTP 500 신호를 표준화하여
//     호출부가 "실패" 가 아닌 "잠시 후 재시도(wait)" UI 로 처리하도록 한다.
//
// 비밀값(secret/token)은 이 모듈에서 절대 로깅/저장하지 않는다. 캐시에는 호출부가 넘긴
// 결과 객체(이미 마스킹된 stage/job JSON)만 담는다.

// dev 의 hot-reload 로 모듈이 재평가되어도 in-flight/캐시가 살아남도록 globalThis 에 고정.
type CacheEntry = { value: unknown; at: number };
type GuardState = {
  inflight: Map<string, Promise<unknown>>;
  cache: Map<string, CacheEntry>;
  // 계좌별 직렬화 mutex: 같은 계좌에서 *서로 다른* KIS op(sync vs test)이
  // 동시에 KIS 를 때리지 않도록 순차 실행시킨다 (초당 거래건수 완화).
  accountChain: Map<string, Promise<unknown>>;
};

const g = globalThis as unknown as { __kisGuard?: GuardState };
const state: GuardState =
  g.__kisGuard ??
  (g.__kisGuard = { inflight: new Map(), cache: new Map(), accountChain: new Map() });

// 기본 TTL (초). sync/balance/test 결과는 짧게만 캐시한다 (15s).
export const DEFAULT_TTL_MS = 15_000;

export type RateLimitInfo = { rateLimited: true; retryAfterMs: number; message: string };

// 문자열에서 KIS rate-limit 신호를 감지한다 (EGW00201 / 초당 거래건수 / 한도 초과).
export function detectRateLimit(text: unknown): boolean {
  if (text == null) return false;
  const s = (typeof text === "string" ? text : JSON.stringify(text)).toLowerCase();
  return (
    s.includes("egw00201") ||
    s.includes("초당 거래건수") ||
    s.includes("초당거래건수") ||
    s.includes("거래건수 초과") ||
    s.includes("거래건수를 초과") ||
    s.includes("rate limit") ||
    s.includes("ratelimit") ||
    s.includes("too many requests") ||
    s.includes("429")
  );
}

// 캐시 키: 계좌 + 작업(op). op 예: "sync" | "broker-test".
export function guardKey(accountId: number | string, op: string): string {
  return `${op}:${accountId}`;
}

function freshFromCache(key: string, ttlMs: number): { hit: true; value: unknown } | { hit: false } {
  const e = state.cache.get(key);
  if (e && Date.now() - e.at < ttlMs) return { hit: true, value: e.value };
  return { hit: false };
}

export type RunResult<T> = {
  value: T;
  source: "cache" | "inflight" | "fresh";
};

// single-flight + TTL 캐시로 fn(KIS 호출) 을 감싼다.
//   - ttlMs 안에 성공 캐시가 있으면 캐시 반환 (KIS 호출 안 함).
//   - 같은 key 에 in-flight 가 있으면 그 Promise 공유 (KIS 호출 안 함).
//   - 둘 다 없으면 fn 을 1회 실행하고 성공 시 캐시에 저장.
// 실패(throw)는 캐시하지 않는다 (다음 요청이 재시도할 수 있게).
export async function runGuarded<T>(
  key: string,
  fn: () => Promise<T>,
  opts?: { ttlMs?: number; bypassCache?: boolean },
): Promise<RunResult<T>> {
  const ttlMs = opts?.ttlMs ?? DEFAULT_TTL_MS;

  if (!opts?.bypassCache) {
    const c = freshFromCache(key, ttlMs);
    if (c.hit) return { value: c.value as T, source: "cache" };
  }

  const existing = state.inflight.get(key);
  if (existing) return { value: (await existing) as T, source: "inflight" };

  const p = (async () => {
    const v = await fn();
    state.cache.set(key, { value: v, at: Date.now() });
    return v;
  })();
  state.inflight.set(key, p);
  try {
    const v = await p;
    return { value: v, source: "fresh" };
  } finally {
    state.inflight.delete(key);
  }
}

// 같은 계좌 KIS 작업을 직렬화한다 (op 가 달라도 동시에 KIS 를 때리지 않게).
//   - 같은 accountId 의 작업은 직전 작업이 끝난 뒤 순차로 실행된다.
//   - 결과/예외는 호출자에게 그대로 전달된다.
export async function withAccountLock<T>(accountId: number | string, fn: () => Promise<T>): Promise<T> {
  const ak = `acct:${accountId}`;
  const prev = state.accountChain.get(ak) ?? Promise.resolve();
  // 앞 작업의 성패와 무관하게 다음 작업을 잇는다.
  const run = prev.catch(() => undefined).then(() => fn());
  state.accountChain.set(ak, run);
  try {
    return await run;
  } finally {
    // 내가 마지막이면 체인 정리 (메모리 누수 방지).
    if (state.accountChain.get(ak) === run) state.accountChain.delete(ak);
  }
}

// 캐시 무효화 (예: 강제 새로고침 트리거 시 호출부에서 사용 가능).
export function invalidate(key: string): void {
  state.cache.delete(key);
}
