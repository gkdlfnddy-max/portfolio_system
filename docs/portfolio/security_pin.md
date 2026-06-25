# 간편 비밀번호 / PIN 인증 (Security · PIN) — Portfolio OS

> 목표: 로컬 접속 시 PIN 으로 계좌·전략·대시보드 접근을 보호한다.
> **UI 숨김이 아니라 서버 라우트/middleware 에서 강제**한다. live 주문 하드락은 PIN 과 **별개**로 유지(CLAUDE.md §15, §16).
> 평문 PIN 저장·로그·localStorage **금지**. Anthropic API 미사용(§17).

관련: [postgres_migration.md](postgres_migration.md) (auth tables) · [safety_rules.md](safety_rules.md)

---

## 1. 목표 흐름

```
로컬 접속 → PIN 입력 → (서버 검증) → 계좌/전략/대시보드 접근
   → 미사용 시 자동 잠금 → 민감 작업 시 재인증 → 실패 초과 시 잠금
```

- 첫 접속/새 브라우저 = PIN 입력 필요.
- 잠금 상태에서는 보호 API 가 401/잠금 응답.
- 민감 작업(전략 저장·주문 관련·live)은 최근 재인증을 추가 요구.

---

## 2. 해시 & 세션 원칙

- **해시**: `scrypt` (Node 내장 `crypto`, 무외부 의존) 기본. 대안 `argon2id` / `bcrypt`.
  - salt + 파라미터(N,r,p) 저장. 비교는 `crypto.timingSafeEqual` (상수시간).
- **평문 PIN 금지**: PIN 평문을 DB·로그·localStorage·sessionStorage·URL·audit payload 어디에도 저장하지 않는다. 검증 직후 메모리에서 폐기.
- **세션**: 성공 시 `session_id` 를 **httpOnly + Secure + SameSite** 쿠키로 발급 → 서버가 `portfolio.auth_sessions` 행으로 검증.
  - 쿠키엔 **성공 여부/세션 id 만**. PIN·해시·권한 평문을 클라이언트에 두지 않는다.
  - 모든 보호 라우트는 매 요청 세션 행을 서버에서 조회·검증(만료·잠금·재인증 시각 확인).

---

## 3. DB (schema `portfolio`)

### `user_security_settings`
| 컬럼 | 설명 |
|---|---|
| `id` | PK |
| `pin_hash` | scrypt/argon2 해시 (평문 아님) |
| `pin_salt` | salt |
| `kdf` | `scrypt` \| `argon2id` \| `bcrypt` |
| `kdf_params` | JSONB (N,r,p 등) |
| `failed_attempts` | 연속 실패 수 |
| `locked_until` | 잠금 해제 시각 (UTC) |
| `pin_set_at` | PIN 설정 시각 |
| `updated_at` | 갱신 시각 |

### `auth_sessions`
| 컬럼 | 설명 |
|---|---|
| `session_id` | PK (랜덤, 쿠키와 매칭) |
| `created_at` | 생성 시각 |
| `last_active_at` | 마지막 활동 (자동잠금 기준) |
| `reauth_at` | 마지막 재인증 시각 (민감작업 윈도우 기준) |
| `expires_at` | 만료 |
| `status` | `active` \| `locked` \| `expired` \| `revoked` |
| `ip` / `user_agent` | 새 브라우저 식별(평문 PIN 아님) |

### `auth_events` (append-only)
| 컬럼 | 설명 |
|---|---|
| `id` | PK |
| `event` | `login_success` \| `login_fail` \| `lock` \| `unlock` \| `reauth` \| `auto_lock` \| `logout` |
| `session_id` | 연관 세션(있으면) |
| `reason` | 사유 (PIN 값은 절대 미포함) |
| `created_at` | 시각 |

> `auth_events` 는 삭제 금지(append-only). PIN 평문·해시는 어떤 event 에도 기록하지 않는다.

---

## 4. 세션 정책 (설정값 — 하드코딩 금지)

> 모든 임계값은 security config / DB(`user_security_settings`·config)로 관리. 코드 상수 하드코딩 금지(§18).

| 정책 | 기본값 |
|---|---|
| 미사용 자동잠금 | **15분** (`last_active_at` 초과 시 lock) |
| 새 브라우저 | 재PIN 필요 (세션 없음 = 인증 요구) |
| 민감작업 재인증 윈도우 | **5분** 내 재인증 필요 (`reauth_at` 기준) |
| 실패 잠금 | 실패 **5회** → **10분** 잠금 (`locked_until`) |
| live 관련 작업 | **항상** 재인증 |

---

## 5. 보호 대상 / 재인증 작업

### 보호 대상 API (세션 필요)
- 계좌 조회·동기화 API
- 전략(investor profile / policy / allocation) 조회·저장 API
- 대시보드·history·decision·risk 조회 API
- 주문/제안 관련 API
- 인증 변경(PIN 변경) API

### 재인증 필요 작업 (최근 재인증 윈도우 내여야 통과)
- 전략 저장(profile.save / policy 반영 / selected allocation 확정)
- 주문 제안 생성·승인 관련 작업
- **live 관련 모든 작업** (항상 재인증)
- PIN 변경·보안 설정 변경

---

## 6. 강제·금지 원칙

- **서버 강제**: 접근 제어는 UI 숨김이 아니라 서버 라우트/middleware 에서 세션·재인증·잠금을 검사해 거부한다.
- **live 하드락 유지**: live 주문 하드락(`KIS_LIVE_CONFIRM`, CEO 체크리스트)은 PIN 과 **독립**으로 항상 유지. PIN 통과가 live 를 열어주지 않는다.
- **개발 우회 금지**: 개발/디버그용 인증 우회 플래그·백도어 금지. 모든 환경에서 동일 경로.
- **약한 PIN 차단**: `000000`, `111111`, `123456` 등 자명한 PIN 설정 거부. 최소 길이·반복/연속 패턴 거부.
- **평문 금지 재확인**: PIN 평문은 저장·로그·localStorage 금지. 클라이언트 세션엔 성공 여부만.
