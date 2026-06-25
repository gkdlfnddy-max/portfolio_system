// 보안 설정 SSOT — 타임아웃/한도를 코드 곳곳에 흩뿌리지 않고 여기서만 정의한다.
// 각 값은 process.env 로 재정의 가능(기본값은 아래).
// PIN(앱/계좌) 전면 제거(CEO 결정) — 접근 통제는 로그인 + RBAC 만. PIN 관련 설정은 폐기했다.

function envMs(name: string, def: number): number {
  const raw = process.env[name];
  if (!raw) return def;
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : def;
}

function envInt(name: string, def: number): number {
  const raw = process.env[name];
  if (!raw) return def;
  const n = parseInt(raw, 10);
  return Number.isInteger(n) && n > 0 ? n : def;
}

// 단일 사용자(CEO 위임 모델) — multi-user 아님.
export const USER_ID = "ceo";

// 로그인 세션이 유휴 상태로 만료되기까지(마지막 활동 기준).
export const SESSION_IDLE_MS = envMs("AUTH_SESSION_IDLE_MS", 15 * 60_000);
// 로그인 잠금 전 최대 실패 횟수.
export const MAX_FAILED = envInt("AUTH_MAX_FAILED", 5);

export const SESSION_COOKIE = "pos_session";
