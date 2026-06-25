"""dotenv 없이도 .env 를 읽는 표준 라이브러리 폴백.

저사양/이식 환경에서 `python-dotenv` 미설치 시, web API 라우트가 시스템 python 으로
백엔드를 실행해도 .env 의 KIS 자격증명이 조용히 비는 사고를 막는다.
(KIS 클라이언트 원칙: 외부 라이브러리 없이 stdlib 만.)

semantics: load_dotenv 와 동일하게 override=False — 이미 os.environ 에 있는 값은 보존.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_env_file(env_path: Path) -> None:
    """`KEY=VALUE` 형식 .env 를 파싱해 os.environ 에 주입(기존 값은 덮어쓰지 않음)."""
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        os.environ.setdefault(key, val)
