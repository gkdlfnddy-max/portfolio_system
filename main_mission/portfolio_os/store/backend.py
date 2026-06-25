"""DB 백엔드 선택자 — SQLite(기본) ↔ PostgreSQL(opt-in).

Track C 원칙:
  - 기본값은 **sqlite** (현 운영). 명시적으로 DB_BACKEND=postgres 일 때만 PG 경로 활성화.
  - DATABASE_URL 은 절대 로그/에러 메시지에 노출하지 않는다 (자격증명 포함).
  - silent fallback 금지 — PG 를 요구했는데 URL 이 없으면 **명확히 raise**.

이 모듈은 자격증명 값을 읽기만 하고 어디에도 출력하지 않는다.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore

ROOT = Path(__file__).resolve().parents[3]

_SQLITE = "sqlite"
_POSTGRES = "postgres"


def _load_env() -> None:
    """`.env` 를 로드 (override=False — os.environ 우선, 테스트 주입 보존)."""
    env = ROOT / ".env"
    if not env.exists():
        return
    if load_dotenv is not None:
        load_dotenv(env)
    else:  # pragma: no cover - 표준환경엔 dotenv 존재
        from ..envfallback import load_env_file

        load_env_file(env)


def current_backend() -> str:
    """현재 DB 백엔드 식별자. 기본 'sqlite'.

    DB_BACKEND 가 'postgres' 또는 'postgresql' 이면 'postgres' 로 정규화.
    그 외(미설정/오타 포함)는 **안전하게 sqlite** 로 떨어진다 (SQLite 기본 보호).
    """
    _load_env()
    raw = (os.getenv("DB_BACKEND") or _SQLITE).strip().lower()
    if raw in ("postgres", "postgresql", "pg"):
        return _POSTGRES
    return _SQLITE


def is_postgres() -> bool:
    """PG 경로가 활성화되었는가 (opt-in)."""
    return current_backend() == _POSTGRES


def is_sqlite() -> bool:
    return current_backend() == _SQLITE


def require_database_url() -> str:
    """PG 접속 문자열을 반환. 없으면 **명확한 에러**로 중단 (silent fallback 금지).

    주의: 반환값(자격증명 포함)을 절대 로그/print 하지 않는다.
    에러 메시지에도 URL 을 넣지 않는다.
    """
    _load_env()
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL 미설정 — DB_BACKEND=postgres 인데 .env 에 DATABASE_URL 이 없습니다. "
            "(값은 보안상 표시하지 않음)"
        )
    return url


# --- Dual-truth 가드 ------------------------------------------------------
# 하나의 운영 view 는 정확히 하나의 백엔드에서만 읽어야 한다.
# PG 와 SQLite 를 한 화면/한 의사결정에서 섞으면 진실원천이 깨진다 → hard-block.

class DualTruthError(RuntimeError):
    """한 view 가 두 백엔드를 동시에 참조하려 할 때."""


def assert_single_backend(used_backends: set[str]) -> str:
    """운영 view 가 사용한 백엔드 집합을 검증. 정확히 1개여야 한다.

    used_backends: {'sqlite'} 또는 {'postgres'} 만 허용. 섞이면 DualTruthError.
    반환: 단일 백엔드 식별자.
    """
    clean = {b for b in used_backends if b in (_SQLITE, _POSTGRES)}
    if len(clean) != 1:
        raise DualTruthError(
            f"운영 view 는 단일 백엔드만 허용 — 발견된 백엔드: {sorted(clean) or 'none'}. "
            "PG 와 SQLite 혼합 조회는 진실원천 위반(hard-block)."
        )
    return clean.pop()
