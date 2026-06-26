"""개별주 추천 엔진(stock_reco) — 정직성·랭킹·부족 표기.

격리 sqlite. 가짜 티커/점수 없음: 미검증 티커 제외, 후보 부족 시 정직 note.
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_stock_reco.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import stock_reco


def setup():
    store_db.init()


def test_returns_candidate_evaluations_and_honest_shortfall():
    setup()
    out = stock_reco.recommend_stocks(1, n=10)
    assert out["ok"] and out["requested"] == 10
    assert out["auto_order_created"] is False and out["requires_user_approval"] is True
    # 시드 개별주(005930/000660)만 → 10개 미만 + 정직 부족 note
    assert out["returned"] < 10
    assert "부족" in out["universe_note"]
    for c in out["candidates"]:
        assert c["candidate_type"] == "stock"
        assert c["approval_required"] is True and c["auto_order_created"] is False
        assert "recommendation_strength" in c


def test_unverified_extra_ticker_is_excluded():
    setup()
    # 존재하지 않는 가짜 티커는 corp_map 검증 실패 → 제외(가짜 티커 금지)
    out = stock_reco.recommend_stocks(1, n=10, extra_tickers=["ZZZZ9", "000000_FAKE"])
    ids = {c["candidate_id"] for c in out["candidates"]}
    assert "ZZZZ9" not in ids and "000000_FAKE" not in ids


def test_ranked_by_confidence_desc():
    setup()
    out = stock_reco.recommend_stocks(1, n=10)
    confs = [float(c.get("confidence") or 0.0) for c in out["candidates"]]
    assert confs == sorted(confs, reverse=True)


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} STOCK-RECO TESTS PASSED")
