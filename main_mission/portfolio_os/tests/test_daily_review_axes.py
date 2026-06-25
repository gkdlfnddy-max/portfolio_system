"""Daily Review 6축 상태 블록(six_axis) 테스트 — 정직 제외·자동주문 0·broker-neutral·단정 회피.

검증 핵심(불변):
  - 6축 상태 블록이 모든 분기(watch 포함)에서 노출된다(generate top-level + payload).
  - 데이터 없는 축은 제외(미연동 정직 표기) — 가짜 점수/단정 0.
  - confidence 낮거나 데이터 부족이면 단정하지 않는다(관망/추가확인 톤).
  - auto_order_created=False · auto_applied=False · broker_neutral=True (자동주문/정책 0).
  - macro/supply_demand 가 연동되면 해당 축을 정직하게 가용으로 보강한다.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_dailyreview_axes.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import daily_review as dr


def setup():
    store_db.init()


def _profile(idx, *, interests="반도체"):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
            "interests_text, updated_at) VALUES(?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(account_index) DO NOTHING",
            (idx, "neutral", 10.0, 30.0, interests),
        )
        conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, "
            "source, captured_at) VALUES(?,?,?,?,?,datetime('now'))",
            (idx, 9_000_000, 10_000_000, 0, "test"),
        )
        conn.commit()
    finally:
        conn.close()


def _six(r):
    """generate top-level 또는 payload 어느 쪽이든 six_axis 를 꺼낸다."""
    return r.get("six_axis") or (r.get("payload", {}) or {}).get("six_axis")


# ---- 1) 6축 블록이 watch 분기(선택 없음)에서도 항상 노출 ----
def test_six_axis_block_present_in_watch():
    _profile(201)
    r = dr.generate_review(201)
    assert r["ok"] and r["action_decision"] == "watch", r
    sa = _six(r)
    assert sa is not None, "watch 분기에서도 six_axis 노출되어야 함"
    assert sa["total_axes"] == 6, sa
    assert len(sa["axes"]) == 6, sa
    # 6축 라벨이 모두 존재(정직 표기: 미연동 축도 라벨로 노출).
    labels = {a["label"] for a in sa["axes"]}
    assert {"기술", "분산", "거시", "이벤트", "심리", "정책/규제"} <= labels, labels


# ---- 2) 데이터 없는 축은 제외(미연동 정직) — 가짜 점수 0 ----
def test_six_axis_missing_axes_excluded_honestly():
    _profile(202)
    r = dr.generate_review(202)
    sa = _six(r)
    # 종목 일봉/축 데이터가 없으므로 가용 축 없음 → 전부 미연동(정직).
    assert sa["available_count"] == 0, sa
    assert len(sa["missing_axes"]) == 6, sa
    for a in sa["axes"]:
        assert a["data_available"] is False, a
        assert "미연동" in a["note"], a
        # 미연동 축은 confidence 가 가짜로 채워지지 않는다(None).
        assert a["confidence"] is None, a


# ---- 3) confidence/위험 데이터 부족 시 단정하지 않는다(관망/추가확인 톤) ----
def test_six_axis_no_assertion_when_insufficient():
    _profile(203)
    r = dr.generate_review(203)
    sa = _six(r)
    assert sa["overall_confidence"] is None and sa["holistic_risk"] is None, sa
    impact = " ".join(sa["portfolio_impact"])
    assert "관망" in impact or "추가확인" in impact or "단정" in impact, impact
    # '하락 확정' 같은 단정 문구가 없어야 한다.
    assert "확정" not in impact or "단정" in impact, impact


# ---- 4) 자동주문/정책 0 · broker-neutral 불변 ----
def test_six_axis_no_auto_order_broker_neutral():
    _profile(204)
    r = dr.generate_review(204)
    sa = _six(r)
    assert sa["auto_order_created"] is False, sa
    assert sa["auto_applied"] is False, sa
    assert sa["broker_neutral"] is True, sa
    assert sa["requires_user_approval"] is True, sa
    # 리뷰 전체도 자동주문 0 유지.
    assert r.get("auto_order_created", False) is False, r


# ---- 5) latest() roundtrip 에서도 six_axis 가 payload 로 보존 ----
def test_six_axis_persisted_in_latest():
    _profile(205)
    dr.generate_review(205)
    last = dr.latest(205)
    assert last is not None, "리뷰 저장 실패"
    sa = (last.get("payload") or {}).get("six_axis")
    assert sa is not None and sa["total_axes"] == 6, sa


# ---- 6) macro 연동 시 거시축이 정직하게 가용으로 보강 ----
def test_six_axis_macro_axis_available_when_connected():
    _profile(206)
    macro_connected = {"connected": True, "lean": "defensive", "changes": [{"note": "금리 상승"}]}
    decline_empty = {"names": []}
    supply_empty = {"data_available": False}
    sa = dr._six_axis_block(decline=decline_empty, macro=macro_connected, supply_demand=supply_empty)
    macro_axis = next(a for a in sa["axes"] if a["axis"] == "macro")
    assert macro_axis["data_available"] is True, macro_axis
    assert "거시" in sa["available_axes"], sa
    # 신호가 정직하게 표기됨(거시 기울기/변화).
    assert any("거시" in s or "금리" in s for s in macro_axis["signals"]), macro_axis


# ---- 7) supply_demand 연동 시 분산축이 가용으로 보강 ----
def test_six_axis_distribution_available_when_supply_connected():
    sa = dr._six_axis_block(
        decline={"names": []},
        macro={"connected": False, "changes": []},
        supply_demand={"data_available": True, "confidence": 0.6,
                       "aggregate": {"smart_money_net_cum": -120.0}},
    )
    dist_axis = next(a for a in sa["axes"] if a["axis"] == "distribution")
    assert dist_axis["data_available"] is True, dist_axis
    assert dist_axis["confidence"] == 0.6, dist_axis
    assert any("스마트머니" in s for s in dist_axis["signals"]), dist_axis


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} SIX-AXIS TESTS PASSED")
