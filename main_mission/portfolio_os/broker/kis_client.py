"""KisHttpClient — KIS Open API 저수준 HTTP (표준 라이브러리만).

책임 (api_adapter.md §4):
  - 토큰 발급/캐시/자동 갱신 (만료 5분 전 선제, 1분당 1회 제한 존중 → 파일 캐시)
  - rate limit (mode별 토큰버킷: paper=5/s, live=20/s, 25% 헤드룸)
  - hashkey 서명 (주문용)
  - 타임아웃/에러 → is_healthy=False (루프 ABORT, 안전 A3)
  - 로그 마스킹: app_key/secret/token/계좌번호 원문 금지 (§26)

외부 라이브러리 없음 (urllib). 자격증명은 .env 에서만 로드.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any

from . import kis_endpoints as ep

ROOT = Path(__file__).resolve().parents[3]
TOKEN_CACHE_DIR = ROOT / "data"

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    if load_dotenv is not None:
        load_dotenv(env)
    else:
        # python-dotenv 미설치 환경(예: 시스템 python 으로 실행되는 web 라우트) 폴백.
        from ..envfallback import load_env_file
        load_env_file(env)


def mask(value: str | None, keep: int = 4) -> str:
    """비밀값 마스킹 — 앞 keep 자리만 노출."""
    if not value:
        return "(empty)"
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


class KisConfigError(RuntimeError):
    pass


class KisHttpClient:
    def __init__(self, mode: ep.Mode | None = None, account_index: int | None = None) -> None:
        """account_index 가 주어지면 .env 의 KIS_ACCOUNT_{n}_* 를 사용.
        없으면 primary(KIS_APP_KEY 등 — 최근 추가 계좌가 미러됨)."""
        _load_env()
        self.account_index = account_index

        if account_index is not None and 1 <= account_index <= 50:
            pre = f"KIS_ACCOUNT_{account_index}_"
            acct_mode = os.getenv(pre + "MODE", "").strip().lower()
            self.mode = (mode or acct_mode or os.getenv("KIS_MODE", "paper")).strip().lower()  # type: ignore
            self.app_key = os.getenv(pre + "APP_KEY", "").strip()
            self.app_secret = os.getenv(pre + "APP_SECRET", "").strip()
            self.account_no = os.getenv(pre + "ACCOUNT_NO", "").strip()
            self.account_prod = os.getenv(pre + "PRODUCT_CODE", "01").strip()
        else:
            self.mode = (mode or os.getenv("KIS_MODE", "paper")).strip().lower()  # type: ignore
            self.app_key = os.getenv("KIS_APP_KEY", "").strip()
            self.app_secret = os.getenv("KIS_APP_SECRET", "").strip()
            self.account_no = os.getenv("KIS_ACCOUNT_NO", "").strip()
            self.account_prod = os.getenv("KIS_ACCOUNT_PRODUCT_CODE", "01").strip()

        if self.mode not in ("paper", "live"):
            raise KisConfigError(f"mode 는 paper|live — got {self.mode!r}")
        self.base = ep.base_url(self.mode)

        self._healthy = True
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._lock = Lock()
        # 토큰버킷 — 25% 헤드룸
        self._rate = max(1, int(ep.RATE_LIMIT_PER_SEC[self.mode] * 0.75))
        self._calls: deque[float] = deque()

    # ------------------------------------------------------------------
    @property
    def is_healthy(self) -> bool:
        return self._healthy

    def require_credentials(self) -> None:
        missing = [
            name for name, val in (
                ("KIS_APP_KEY", self.app_key),
                ("KIS_APP_SECRET", self.app_secret),
                ("KIS_ACCOUNT_NO", self.account_no),
            ) if not val
        ]
        if missing:
            raise KisConfigError(
                "다음 .env 값이 비어 있습니다: " + ", ".join(missing)
                + " — docs/portfolio/kis_onboarding.md 참고."
            )

    def credential_summary(self) -> dict[str, str]:
        """마스킹된 자격증명 요약 (로그/진단용)."""
        return {
            "mode": self.mode,
            "base_url": self.base,
            "app_key": mask(self.app_key),
            "app_secret": mask(self.app_secret),
            "account_no": mask(self.account_no, keep=2),
            "account_prod": self.account_prod,
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
        return TOKEN_CACHE_DIR / f"kis_token_{self.mode}{suffix}.json"

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
                "appsecret": self.app_secret,
            }
            try:
                resp = self._raw_post(ep.PATH_TOKEN, body, headers={"content-type": "application/json"})
            except Exception as exc:
                self._healthy = False
                raise RuntimeError(f"토큰 발급 실패 (broker unhealthy, A3): {exc}") from exc
            token = resp.get("access_token")
            if not token:
                self._healthy = False
                raise RuntimeError(f"토큰 응답에 access_token 없음: {resp}")
            self._token = token
            self._token_exp = time.time() + int(resp.get("expires_in", 86400))
            self._save_cached_token()
            return token

    # --- low-level HTTP -------------------------------------------------
    def _raw_post(self, path: str, body: dict[str, Any], headers: dict[str, str], timeout: int = 10) -> dict:
        data = json.dumps(body).encode("utf-8")
        for _attempt in range(4):
            self._throttle()
            req = urllib.request.Request(self.base + path, data=data, method="POST")
            for k, v in headers.items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                # KIS 서버측 초당 거래건수 초과(EGW00201) → 백오프 후 재시도(주문 아님·조회/토큰 안전)
                if ("EGW00201" in detail or "초당 거래건수" in detail) and _attempt < 3:
                    time.sleep(0.7 * (2 ** _attempt))
                    continue
                raise RuntimeError(f"HTTP {e.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                self._healthy = False
                raise RuntimeError(f"네트워크 오류 (unhealthy): {e}") from None
        raise RuntimeError("HTTP 재시도 초과(rate limit)")

    def _auth_headers(self, tr_id: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.ensure_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",  # 개인
        }
        if extra:
            h.update(extra)
        return h

    def get(self, path: str, tr_id: str, params: dict[str, str], timeout: int = 10) -> dict:
        query = urllib.parse.urlencode(params)
        for _attempt in range(4):
            self._throttle()
            req = urllib.request.Request(f"{self.base}{path}?{query}", method="GET")
            for k, v in self._auth_headers(tr_id).items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")
                # KIS 서버측 초당 거래건수 초과(EGW00201) → 백오프 후 재시도(읽기 전용 조회)
                if ("EGW00201" in detail or "초당 거래건수" in detail) and _attempt < 3:
                    time.sleep(0.7 * (2 ** _attempt))
                    continue
                raise RuntimeError(f"HTTP {e.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                self._healthy = False
                raise RuntimeError(f"네트워크 오류 (unhealthy): {e}") from None
        raise RuntimeError("HTTP 재시도 초과(rate limit)")

    def post(self, path: str, tr_id: str, body: dict[str, Any], hashkey: str | None = None, timeout: int = 10) -> dict:
        extra = {"hashkey": hashkey} if hashkey else None
        return self._raw_post(path, body, headers=self._auth_headers(tr_id, extra), timeout=timeout)

    def hashkey(self, body: dict[str, Any]) -> str:
        """주문 body 서명 (KIS 요구). 주문 전송에만 사용."""
        resp = self._raw_post(ep.PATH_HASHKEY, body, headers={
            "content-type": "application/json; charset=utf-8",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        })
        h = resp.get("HASH")
        if not h:
            raise RuntimeError(f"hashkey 발급 실패: {resp}")
        return h
