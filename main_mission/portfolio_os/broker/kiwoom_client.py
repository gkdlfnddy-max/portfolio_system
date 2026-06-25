"""KiwoomHttpClient — 키움증권 REST API 저수준 HTTP (표준 라이브러리만).

멀티 브로커 원칙: KIS(kis_client.py)와 **완전 분리된 독립 클라이언트**. KIS 코드에
키움 분기를 끼워넣지 않는다. KIS 와 구조(토큰 캐시·rate limit·마스킹)는 동일하게 맞춘다.

책임:
  - OAuth 토큰 발급/캐시/자동 갱신 (만료 5분 전 선제, 파일 캐시)
  - REST GET/POST 래퍼 (api-id 헤더 = TR ID)
  - 타임아웃/네트워크 오류 → is_healthy=False (루프 ABORT, 안전 A3)
  - 로그 마스킹: app_key/secret/token/계좌번호 원문 출력 금지 (안전 §8)

자격증명: .env 의 KIWOOM_ACCOUNT_{n}_APP_KEY / _APP_SECRET / _ACCOUNT_NO (평문 로그/DB 금지).
키 미설정 시 KiwoomConfigError 로 **명확히** 실패 — 비밀은 노출하지 않는다.

조사 출처(2026-06, WebSearch/WebFetch 검증):
  - base url: live=https://api.kiwoom.com / paper(mock)=https://mockapi.kiwoom.com  (✅ 검증)
  - 토큰 endpoint: POST /oauth2/token, body{grant_type=client_credentials, appkey, secretkey}  (✅ 검증)
  - 토큰 응답: token / token_type / expires_dt(YYYYMMDDHHMMSS)  (✅ 검증)
  - api-id 헤더로 TR ID 전달, cont-yn / next-key 페이징 헤더  (✅ 검증)
  세부 endpoint/응답필드는 kiwoom_endpoints 주석 + docs/portfolio/kiwoom_onboarding.md 참고.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any

from .kis_client import ROOT, TOKEN_CACHE_DIR, mask, _load_env

# --- 검증된 상수 (조사 2026-06) -------------------------------------------------
PAPER_BASE_URL = "https://mockapi.kiwoom.com"   # 모의투자 (✅ 검증)
LIVE_BASE_URL = "https://api.kiwoom.com"        # 실전 (✅ 검증)

PAPER_WS_URL = "wss://mockapi.kiwoom.com:10000"  # 참고 (실시간 — 1차 미사용)
LIVE_WS_URL = "wss://api.kiwoom.com:10000"

PATH_TOKEN = "/oauth2/token"                    # POST, au10001 (✅ 검증)

# api-id(TR) → endpoint 경로. (✅ 경로 검증 / ⚠️ 일부 응답 필드는 docs '확인 필요')
PATH_ACCOUNT = "/api/dostk/acnt"                # 계좌(예수금 kt00001 / 잔고 kt00018) (✅ 검증)
PATH_STOCK_INFO = "/api/dostk/stkinfo"          # 주식기본정보 ka10001 (✅ 검증)

API_DEPOSIT = "kt00001"      # 예수금상세현황요청 (✅ 검증)
API_BALANCE = "kt00018"      # 계좌평가잔고내역요청 (✅ 검증)
API_STOCK_INFO = "ka10001"  # 주식기본정보요청 (✅ 검증)

# 초당 호출 한도(보수적 추정 — KIS 와 동일하게 토큰버킷에 25% 헤드룸 적용).
# 공식 수치 미확정 → 보수적 5/s. docs 에 '확인 필요' 명시.
RATE_LIMIT_PER_SEC = {"paper": 5, "live": 5}


class KiwoomConfigError(RuntimeError):
    """키움 REST 키 미설정/오류 — 조회/주문 차단 (비밀 미노출)."""


def base_url(mode: str) -> str:
    return LIVE_BASE_URL if mode == "live" else PAPER_BASE_URL


class KiwoomHttpClient:
    """키움 REST 저수준 HTTP. 외부 라이브러리 없음 (urllib). 자격증명은 .env 전용."""

    def __init__(self, mode: str = "paper", account_index: int | None = None) -> None:
        _load_env()
        self.account_index = account_index
        self.mode = (mode or "paper").strip().lower()
        if self.mode not in ("paper", "live"):
            raise KiwoomConfigError(f"키움 mode 는 paper|live — got {self.mode!r}")

        pre = f"KIWOOM_ACCOUNT_{account_index}_" if account_index else "KIWOOM_"
        self._prefix = pre
        self.app_key = os.getenv(pre + "APP_KEY", "").strip()
        self.app_secret = os.getenv(pre + "APP_SECRET", "").strip()
        self.account_no = os.getenv(pre + "ACCOUNT_NO", "").strip()

        self.base = base_url(self.mode)
        self._healthy = True
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._lock = Lock()
        self._rate = max(1, int(RATE_LIMIT_PER_SEC[self.mode] * 0.75))
        self._calls: deque[float] = deque()

    # ------------------------------------------------------------------
    @property
    def is_healthy(self) -> bool:
        return self._healthy

    def configured(self) -> bool:
        return bool(self.app_key and self.app_secret)

    def require_credentials(self) -> None:
        if not self.configured():
            raise KiwoomConfigError(
                "키움 REST 자격증명 미설정 — .env 의 "
                f"{self._prefix}APP_KEY / {self._prefix}APP_SECRET 가 필요합니다. "
                "(키움 REST 앱키 발급·모의투자 신청 후. docs/portfolio/kiwoom_onboarding.md 참고. "
                "평문 키는 로그/DB 저장 금지.)"
            )

    def credential_summary(self) -> dict[str, str]:
        """마스킹된 자격증명 요약 (로그/진단용 — 원문 금지)."""
        return {
            "broker": "kiwoom",
            "mode": self.mode,
            "base_url": self.base,
            "app_key": mask(self.app_key),
            "app_secret": mask(self.app_secret),
            "account_no": mask(self.account_no, keep=2),
        }

    # --- rate limit -----------------------------------------------------
    def _throttle(self) -> None:
        now = time.monotonic()
        while self._calls and now - self._calls[0] > 1.0:
            self._calls.popleft()
        if len(self._calls) >= self._rate:
            sleep = 1.0 - (now - self._calls[0])
            if sleep > 0:
                time.sleep(sleep)
        self._calls.append(time.monotonic())

    # --- token ----------------------------------------------------------
    def _token_cache_path(self) -> Path:
        suffix = f"_{self.account_index}" if self.account_index else ""
        return TOKEN_CACHE_DIR / f"kiwoom_token_{self.mode}{suffix}.json"

    def _load_cached_token(self) -> bool:
        p = self._token_cache_path()
        if not p.exists():
            return False
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("base") == self.base and data.get("app_key_head") == mask(self.app_key):
                if data.get("expires_at", 0) - time.time() > 300:  # 5분 헤드룸
                    self._token = data["access_token"]
                    self._token_exp = data["expires_at"]
                    return True
        except Exception:
            return False
        return False

    def _save_cached_token(self) -> None:
        try:
            TOKEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self._token_cache_path().write_text(json.dumps({
                "base": self.base,
                "app_key_head": mask(self.app_key),
                "access_token": self._token,
                "expires_at": self._token_exp,
            }), encoding="utf-8")
        except Exception:
            pass  # 캐시 실패는 치명적 아님

    @staticmethod
    def _parse_expires(resp: dict[str, Any]) -> float:
        """expires_dt(YYYYMMDDHHMMSS) 우선, 없으면 expires_in(초). 둘 다 없으면 +6h 보수."""
        dt = str(resp.get("expires_dt", "")).strip()
        if dt:
            try:
                # 키움 expires_dt 는 KST 기준 문자열. naive→UTC epoch 근사(보수적으로 충분).
                ts = datetime.strptime(dt, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
                if ts > time.time():
                    return ts
            except Exception:
                pass
        try:
            return time.time() + int(resp.get("expires_in", 21600))
        except Exception:
            return time.time() + 21600

    def ensure_token(self) -> str:
        with self._lock:
            if self._token and self._token_exp - time.time() > 300:
                return self._token
            if self._load_cached_token():
                return self._token  # type: ignore
            self.require_credentials()
            body = {
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "secretkey": self.app_secret,  # 키움은 'secretkey' (KIS 의 appsecret 과 다름)
            }
            try:
                resp = self._raw_post(PATH_TOKEN, body, headers={
                    "content-type": "application/json;charset=UTF-8",
                })
            except Exception as exc:
                self._healthy = False
                raise RuntimeError(f"키움 토큰 발급 실패 (broker unhealthy, A3): {exc}") from exc
            # 키움 응답: return_code(0=정상) / token / token_type / expires_dt
            if str(resp.get("return_code", "0")) not in ("0", ""):
                self._healthy = False
                raise RuntimeError(
                    f"키움 토큰 응답 오류 return_code={resp.get('return_code')} "
                    f"msg={resp.get('return_msg')}"
                )
            token = resp.get("token") or resp.get("access_token")
            if not token:
                self._healthy = False
                raise RuntimeError(f"키움 토큰 응답에 token 없음 (return_msg={resp.get('return_msg')})")
            self._token = token
            self._token_exp = self._parse_expires(resp)
            self._save_cached_token()
            return token

    # --- low-level HTTP -------------------------------------------------
    def _raw_post(self, path: str, body: dict[str, Any], headers: dict[str, str], timeout: int = 10) -> dict:
        self._throttle()
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            self._healthy = False
            raise RuntimeError(f"네트워크 오류 (unhealthy): {e}") from None

    def _auth_headers(self, api_id: str, cont_yn: str = "N", next_key: str = "") -> dict[str, str]:
        return {
            "content-type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.ensure_token()}",
            "api-id": api_id,
            "cont-yn": cont_yn,
            "next-key": next_key,
        }

    def request(self, path: str, api_id: str, body: dict[str, Any] | None = None,
                cont_yn: str = "N", next_key: str = "", timeout: int = 10) -> dict:
        """키움 REST 호출 (대부분 POST + api-id 헤더 + JSON body)."""
        return self._raw_post(
            path, body or {}, headers=self._auth_headers(api_id, cont_yn, next_key), timeout=timeout
        )
