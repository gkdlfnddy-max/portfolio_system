"""설정 파일(config/portfolio/*.json) 단일 원본 로더 — 코드 하드코딩 금지(CEO 지시).

종목/ETF·테마 분류·국채 유니버스 등 "원본 자료"는 코드에 하드코딩하지 않고 여기 설정
파일에서만 관리한다. 추가·수정은 해당 JSON 파일만 고치면 모든 소비처에 반영된다.

scope 별 config (계좌별/시스템별/종목별 등):
  - instruments : 종목/ETF/테마/섹터/bucket            (종목별)
  - themes      : 테마 키워드/인접/보완/헤지              (시스템별)
  - govbond     : 국채 ETF 유니버스                       (시스템별)
계좌별 설정(목적/성향/관심·제외/정책)은 정적 파일이 아니라 DB(investor_profile/
portfolio_policies/universe_instruments 등)에서 계좌별로 동적 관리한다.

파일 없음/JSON 오류는 명확 실패(가짜 데이터 추측 금지).
"""
from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).resolve().parents[2] / "config" / "portfolio"
_cache: dict[str, dict] = {}


def config_dir() -> Path:
    return _DIR


def load(name: str) -> dict:
    """config/portfolio/{name}.json 로드(프로세스 캐시). name 예: instruments|themes|govbond."""
    if name not in _cache:
        path = _DIR / f"{name}.json"
        with open(path, encoding="utf-8") as f:
            _cache[name] = json.load(f)
    return _cache[name]


def reload(name: str | None = None) -> None:
    """캐시 무효화(설정 파일을 런타임에 갱신했을 때). name 미지정 시 전체."""
    if name is None:
        _cache.clear()
    else:
        _cache.pop(name, None)
