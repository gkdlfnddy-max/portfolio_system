"""개별주 carve(equity_option) — "개별주 N종을 자본 5%/10%로" 가 실제 배분에 적용되는지.

핵심(이전엔 미적용 버그):
  - allocate(equity_option='10') → picks['individual'] 개별주에 위험자산에서 10% 떼어 균등(종목당 ≤2%).
  - anchor/tilt 비례 축소 → 합계 100 불변.
  - 개별주 검증은 instrument_master(DB) — 하드코딩 시드 금지. 미검증 티커 제외.
  - equity_option='none' 이면 carve 0(하위호환).
격리 sqlite + instrument_master 시드. Anthropic API 미사용.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import instrument_master as im
from main_mission.portfolio_os import weight_allocator as wa

# 위험자산 큰 확정안(개별주 carve 여지 충분). anchor 40 + tilt 8+8 + hedge 2 + cash 42 = 100.
_CONFIRMED = [
    {"kind": "cash", "ref": None, "weight_pct": 42.0},
    {"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": 40.0},
    {"kind": "tilt", "ref": "로봇", "weight_pct": 8.0},
    {"kind": "tilt", "ref": "반도체", "weight_pct": 8.0},
    {"kind": "hedge", "ref": "반도체 인버스", "weight_pct": 2.0},
]
_INDIV10 = ["005930", "000660", "000990", "036930", "039030",
            "042700", "058470", "240810", "373220", "207940"]


def setup_function():
    store_db.init()
    im.seed()
    conn = store_db.connect()
    try:
        conn.execute("INSERT OR IGNORE INTO accounts(account_index, alias, mode) VALUES(1,'t','mock')")
        conn.execute("INSERT INTO allocation_selections(account_index, variant, allocation, status, "
                     "selected_by, selected_at) VALUES(1,'base',?, 'active','user',?)",
                     (json.dumps(_CONFIRMED), datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()


def _indiv(r):
    return [h for h in r["holdings"] if h["bucket"] == "individual"]


def test_carve_10_names_10pct_one_pct_each():
    r = wa.allocate(1, {"individual": _INDIV10}, equity_option="10")
    assert r["ok"] and r["total_pct"] == 100.0 and r["total_is_100"]
    ind = _indiv(r)
    assert len(ind) == 10
    assert all(h["weight_pct"] == 1.0 for h in ind)            # 10% / 10종 = 1%씩
    assert round(sum(h["weight_pct"] for h in ind), 2) == 10.0  # 개별주 합 = 10%


def test_carve_reduces_anchor_tilt_keeps_total_100():
    base = wa.allocate(1, {"individual": _INDIV10}, equity_option="none")
    carved = wa.allocate(1, {"individual": _INDIV10}, equity_option="10")
    at_base = sum(h["weight_pct"] for h in base["holdings"] if h["kind"] in ("anchor", "tilt"))
    at_carved = sum(h["weight_pct"] for h in carved["holdings"] if h["kind"] in ("anchor", "tilt"))
    assert abs((at_base - at_carved) - 10.0) <= 0.2   # anchor+tilt ≈10%p 축소(반올림 잔차는 현금 흡수)
    assert carved["total_pct"] == 100.0
    assert round(sum(h["weight_pct"] for h in carved["holdings"] if h["bucket"] == "individual"), 2) == 10.0
    # 헤지는 불변, 현금은 반올림 잔차(≤0.2)만 흡수(carve 는 anchor/tilt 에서만).
    assert _kind_sum(base, "hedge") == _kind_sum(carved, "hedge")
    assert abs(_kind_sum(base, "cash") - _kind_sum(carved, "cash")) <= 0.2


def _kind_sum(r, kind):
    return round(sum(h["weight_pct"] for h in r["holdings"] if h["kind"] == kind), 2)


def test_carve_5pct():
    r = wa.allocate(1, {"individual": _INDIV10}, equity_option="5")
    ind = _indiv(r)
    assert len(ind) == 10
    assert all(h["weight_pct"] == 0.5 for h in ind)            # 5% / 10종 = 0.5%
    assert round(sum(h["weight_pct"] for h in ind), 2) == 5.0


def test_single_name_2pct_cap():
    # 2종 + 10% → 10/2=5% 이지만 단일 2% 상한 → 2%씩(합 4%, 전부 안 씀 — 정직).
    r = wa.allocate(1, {"individual": ["005930", "000660"]}, equity_option="10")
    ind = _indiv(r)
    assert all(h["weight_pct"] == 2.0 for h in ind)
    assert round(sum(h["weight_pct"] for h in ind), 2) == 4.0


def test_unverified_ticker_excluded_no_hardcode():
    # 가짜/미검증 티커는 instrument_master 에 없으므로 carve 에서 제외(하드코딩 시드 의존 아님).
    r = wa.allocate(1, {"individual": ["005930", "ZZZZ9", "FAKE000"]}, equity_option="10")
    ind = _indiv(r)
    assert {h["ticker"] for h in ind} == {"005930"}            # 검증된 1종만
    assert any("미검증" in w.get("msg", "") for w in r["over_limit_warnings"])


def test_none_no_carve_backward_compat():
    r = wa.allocate(1, {"individual": _INDIV10}, equity_option="none")
    assert _indiv(r) == []                                     # carve 없음 → 개별주 holdings 0
    assert r["total_pct"] == 100.0
