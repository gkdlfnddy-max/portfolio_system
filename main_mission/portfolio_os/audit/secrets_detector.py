"""감사로그 payload 에 비밀값이 섞이는 것을 *원천 차단* (마스킹 아님).

key 이름 또는 명백한 비밀 형태 감지 시 거부. 안전 §8 (자격증명 .env 전용).
"""
from __future__ import annotations

import re
from typing import Any

_SECRET_KEY = re.compile(
    r"(app_?key|app_?secret|appkey|appsecret|secret|token|password|passwd|"
    r"account_?no|계좌번호|api_?key|private)",
    re.IGNORECASE,
)


def _walk(obj: Any, path: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k), v
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


def scan(payload: Any) -> str | None:
    """비밀값 의심 발견 시 사유 문자열, 없으면 None."""
    if payload is None:
        return None
    for key, _ in _walk(payload):
        if _SECRET_KEY.search(key):
            return f"secret-like key in payload: {key!r}"
    return None
