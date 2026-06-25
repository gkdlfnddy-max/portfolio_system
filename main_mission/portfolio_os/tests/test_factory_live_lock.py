"""Broker factory **live 하드락** 단위 회귀 테스트 (안전 §6, §15).

목적: 브로커 어댑터/팩토리 리팩토링(Phase 2~3) 중 live 잠금이 *조용히* 우회되지 않게
      factory 레벨에서 직접 고정한다.

  - get_broker(mode="live")  : KIS_LIVE_CONFIRM 없으면 RuntimeError, 있으면 KisLiveAdapter
  - kiwoom live              : 동일 하드락 (안전 §6,15)
  - mock / paper             : KIS_LIVE_CONFIRM 무관하게 생성 (live 가 아니므로)

보완 관계: `test_core_runtask.py` ⑤ 는 *order_submit run_task* 경로에서 live lock 예외 전파를
검증한다(통합 레벨). 본 파일은 *factory.get_broker 자체*를 잠근다(단위 레벨). 둘은 다른 층.
교차계좌 메모리 격리는 test_memory_anonymize #3, 미들웨어 account gate 는 test_core_runtask ②가 커버.
DB 불필요(자격증명 없이 동작) — Anthropic API 미사용.
"""
from __future__ import annotations

import os
import tempfile

import pytest

# conftest autouse fixture 가 참조하는 모듈 임시경로(DB 안 쓰지만 규약 유지).
_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_factory_live.sqlite3")

from main_mission.portfolio_os.broker import factory
from main_mission.portfolio_os.broker.mock_adapter import MockAdapter
from main_mission.portfolio_os.broker.kis_adapter import KisPaperAdapter, KisLiveAdapter
from main_mission.portfolio_os.broker.kiwoom_adapter import KiwoomRestAdapter

_CONFIRM = "KIS_LIVE_CONFIRM"
_OK = "I_UNDERSTAND"


@pytest.fixture(autouse=True)
def _clean_confirm_env():
    """각 테스트가 KIS_LIVE_CONFIRM 를 명시적으로 제어하고, 끝나면 원복."""
    saved = os.environ.get(_CONFIRM)
    os.environ.pop(_CONFIRM, None)
    yield
    if saved is None:
        os.environ.pop(_CONFIRM, None)
    else:
        os.environ[_CONFIRM] = saved


# ── ① KIS live 차단: confirm 없으면 RuntimeError + 메시지에 KIS_LIVE_CONFIRM ──
def test_kis_live_blocked_without_confirm():
    os.environ.pop(_CONFIRM, None)
    with pytest.raises(RuntimeError) as ei:
        factory.get_broker(mode="live")
    assert "KIS_LIVE_CONFIRM" in str(ei.value), ei.value


# ── ② KIS live 허용: confirm 정확값이면 KisLiveAdapter 생성 (게이트가 '환경변수=값' 임을 고정) ──
def test_kis_live_allowed_with_confirm():
    os.environ[_CONFIRM] = _OK
    adapter = factory.get_broker(mode="live")
    assert isinstance(adapter, KisLiveAdapter), type(adapter)


# ── ③ 잘못된 confirm 값도 차단 (정확 일치만 허용) ──
def test_kis_live_blocked_with_wrong_confirm():
    os.environ[_CONFIRM] = "yes"
    with pytest.raises(RuntimeError):
        factory.get_broker(mode="live")


# ── ④ kiwoom live 도 동일 하드락 ──
def test_kiwoom_live_blocked_without_confirm():
    os.environ.pop(_CONFIRM, None)
    with pytest.raises(RuntimeError) as ei:
        factory.get_broker(mode="live", broker="kiwoom")
    assert "KIS_LIVE_CONFIRM" in str(ei.value), ei.value


def test_kiwoom_live_allowed_with_confirm():
    os.environ[_CONFIRM] = _OK
    adapter = factory.get_broker(mode="live", broker="kiwoom")
    assert isinstance(adapter, KiwoomRestAdapter), type(adapter)


# ── ⑤ mock/paper 는 live 가 아니므로 confirm 없이도 생성 (하드락이 live 에만 적용됨을 고정) ──
def test_mock_and_paper_never_require_confirm():
    os.environ.pop(_CONFIRM, None)
    assert isinstance(factory.get_broker(mode="mock"), MockAdapter)
    assert isinstance(factory.get_broker(mode="paper"), KisPaperAdapter)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
