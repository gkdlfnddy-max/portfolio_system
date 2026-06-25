# 인증/인가 응답코드 (SSOT)

> 본 문서는 웹 API 의 인증·인가 응답코드의 **단일 출처(SSOT)** 다.
> 코드 구현: [web/lib/auth/rbac.ts](../../web/lib/auth/rbac.ts) · [web/lib/auth/guard.ts](../../web/lib/auth/guard.ts) · [web/lib/auth/users.ts](../../web/lib/auth/users.ts)
> 프론트 표시는 보조일 뿐 — **실제 차단(authz)은 항상 서버**에서 enforce 한다.

---

## 1. 게이트 순서 (정규)

계좌 API 는 아래 순서로 차단한다. 앞 단계에서 막히면 뒤 단계는 검사하지 않는다.

| 순서 | 단계 | 통과 실패 시 | HTTP | code |
|---|---|---|---|---|
| 1 | 로그인 | 미로그인 | 401 | `UNAUTHENTICATED` |
| 2 | 계좌 RBAC | 권한 없는 계좌 | 403 | `FORBIDDEN` |
| 3 | 앱 PIN | PIN 미해제 | 401 | `PIN_REQUIRED` |
| 3' | 앱 PIN(민감작업) | 재인증 창 만료 | 403 | `REAUTH_REQUIRED` |
| 4 | 계좌별 PIN | 계좌 PIN 미해제 | 401 | `ACCOUNT_LOCKED` |
| 4' | 계좌별 PIN(민감작업) | 계좌 PIN 재인증 만료 | 403 | `ACCOUNT_REAUTH_REQUIRED` |

> **2(RBAC)가 3(PIN)보다 먼저**다. 권한 없는 계좌는 PIN 을 묻기 전에 막는다(존재 비노출).
> 묶음 헬퍼: `requireLoginAndAccount` (1+2), `requireAccountAccessAndUnlocked` (1+2+3),
> `requireAccountAccessAndReauth` (1+2+3').

---

## 2. 코드 표 (전체)

| code | HTTP | 의미 | 발생 위치 | 프론트 처리 |
|---|---|---|---|---|
| `UNAUTHENTICATED` | 401 | 로그인 필요 | rbac.requireUser | `/login?next=…` 으로 이동 (LoginGate) |
| `FORBIDDEN` | 403 | 권한 없음(계좌 RBAC / admin 전용) | rbac.requireAccountAccess / requireAdmin | 빈 화면 + 안내, 권한 요청 유도 |
| `PIN_REQUIRED` | 401 | 앱 PIN 미해제 | guard.requireUnlocked | PIN 잠금 해제 화면(PinGate) |
| `REAUTH_REQUIRED` | 403 | 앱 PIN 민감작업 재인증 창 만료 | guard.requireRecentReauth | PIN 재입력 모달 |
| `ACCOUNT_LOCKED` | 401 | 계좌별 PIN 미해제 | account.requireAccountUnlocked/Reauth | 계좌 PIN 입력(AccountPinGate) |
| `ACCOUNT_REAUTH_REQUIRED` | 403 | 계좌별 PIN 민감작업 재인증 만료 | account.requireAccountReauth | 계좌 PIN 재입력 |
| `INVALID_ACCOUNT` | 400 | 잘못된 account id | rbac.requireAccountAccess | 입력 오류 |
| `BAD_CURRENT` | 403 | 현재 비밀번호 불일치 | users.changePassword | "현재 비밀번호가 올바르지 않습니다." |
| `WEAK_PASSWORD` | 400 | 비밀번호 정책 위반(8자 미만 등) | users.changePassword / firstLoginSetPassword / consumeResetToken | "비밀번호는 8자 이상이어야 합니다." |
| `NOT_REQUIRED` | 400 | first_login 인데 reset_required 아님 | users.firstLoginSetPassword | 일반 변경 화면으로 안내 |
| `NOT_FOUND` | 400 | 사용자 없음 | users | "사용자를 찾을 수 없습니다." |
| `INVALID_OR_EXPIRED` | 400 | 리셋 토큰 무효/만료 | users.consumeResetToken | "재설정 링크가 만료되었습니다." |

> 401 vs 403 규칙:
> - **401** = "너가 누군지 모름/세션 잠김" → 로그인하거나 PIN 을 풀면 풀린다. (`UNAUTHENTICATED`, `PIN_REQUIRED`)
> - **403** = "누군지는 알지만 이 자원/작업은 안 됨" → 재로그인으로 안 풀린다. (`FORBIDDEN`, `REAUTH_REQUIRED`, `BAD_CURRENT`)
> - **423** = 자원 자체가 잠김(계좌별 PIN). (`ACCOUNT_LOCKED`)

---

## 3. 응답 형태

성공·실패 모두 동일한 봉투(envelope)를 쓴다.

```jsonc
// 실패
{ "ok": false, "error": "사람이 읽는 한글 메시지", "code": "FORBIDDEN" }
// 성공
{ "ok": true, ... }
```

- `code` 는 **머신 판별용(영문 상수)**, `error` 는 **사용자 표시용(한글)**.
- 프론트는 분기는 `code` 로, 표시는 `error`(없으면 code 매핑 fallback)로 한다.

---

## 4. reset_required (초기/임시 비밀번호)

- admin 이 사용자를 만들거나 비번을 리셋하면 `reset_required = true` + 임시 비번.
- 해당 사용자는 로그인은 되지만 **비밀번호 변경 전까지 다른 화면 접근 차단**.
  - LoginGate 가 `reset_required && !/security/password` 이면 `/security/password?first=1` 로 보낸다.
  - 비번 화면(`first_login` action)으로 새 비번을 설정하면 `reset_required=false` + 전 세션 철회 → 재로그인.
- 일반 변경(`change` action)도 성공 시 **전 세션 철회**되므로 재로그인이 필요하다.

---

> 변경 시 본 문서를 먼저 갱신(SSOT)하고 코드/프론트를 맞춘다.
