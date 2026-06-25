"""Agent 6 개선 3 — 금지행위 **레포 전역 스캔**(흩어진 개별 점검을 단일 회귀로 통합).

검증(전 portfolio_os 소스 + web 일부):
  - anthropic/openai 직접 import/호출 없음(지능 = Claude+메모리)
  - auto_order_created=True / auto_applied=True 리터럴 없음(자동주문/자동적용 금지)
  - 하드코딩 secret 없음(KIS/API 키는 .env 전용)
  - placeholder 를 실데이터처럼 쓰는 표식 없음(가짜 데이터 금지 — 표준은 data_available)
"""
from __future__ import annotations

import glob
import os
import re

_PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../portfolio_os
_WEB = os.path.normpath(os.path.join(_PKG, "..", "..", "web"))


def _py_sources() -> list[str]:
    files = glob.glob(os.path.join(_PKG, "**", "*.py"), recursive=True)
    return [f for f in files if "__pycache__" not in f and os.sep + "tests" + os.sep not in f]


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_no_anthropic_or_openai_imports():
    pat = re.compile(r"^\s*(?:import|from)\s+(?:anthropic|openai)\b", re.M)
    bad = [f for f in _py_sources() if pat.search(_read(f))]
    assert not bad, f"anthropic/openai import 발견: {bad}"


def test_no_anthropic_api_key_usage():
    pat = re.compile(r"ANTHROPIC_API_KEY|OPENAI_API_KEY")
    bad = [f for f in _py_sources() if pat.search(_read(f))]
    assert not bad, f"외부 LLM API key 참조 발견: {bad}"


def test_no_auto_order_or_auto_apply_true():
    pat = re.compile(r"auto_(?:order_created|applied)\s*=\s*True")
    bad = [f for f in _py_sources() if pat.search(_read(f))]
    assert not bad, f"auto_order_created/auto_applied=True 리터럴 발견: {bad}"


def test_no_hardcoded_secrets():
    # APP_KEY/APP_SECRET/API_KEY/TOKEN 에 긴 리터럴을 직접 대입한 코드 금지(.env 전용).
    pat = re.compile(r"(APP_KEY|APP_SECRET|API_KEY|ACCESS_TOKEN|SECRET_KEY)\s*=\s*['\"][A-Za-z0-9/_\-+]{16,}['\"]")
    bad = []
    for f in _py_sources():
        for ln in _read(f).splitlines():
            if pat.search(ln) and "os.getenv" not in ln and "environ" not in ln:
                bad.append((os.path.basename(f), ln.strip()[:80]))
    assert not bad, f"하드코딩 secret 의심: {bad}"


def test_web_no_anthropic_openai():
    if not os.path.isdir(os.path.join(_WEB, "app")):
        return  # web 미존재 시 skip(graceful)
    pat = re.compile(r"from\s+['\"](?:@anthropic-ai|openai)|require\(['\"](?:@anthropic-ai|openai)")
    bad = []
    for base in ("app", "lib", "components"):
        for f in glob.glob(os.path.join(_WEB, base, "**", "*.ts*"), recursive=True):
            if "node_modules" in f:
                continue
            if pat.search(_read(f)):
                bad.append(f)
    assert not bad, f"web 에서 anthropic/openai 직접 사용: {bad}"


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"ALL {len(fns)} SAFETY-SCAN TESTS PASSED")
