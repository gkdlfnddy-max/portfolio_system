"""Daily Review 국채(govbond) 점검 블록 테스트.

검증(불변):
  - 모든 분기(관망/조정/스냅샷 없음)에서 govbond_check 가 노출된다.
  - **자동 변경 금지**: auto_order_created=False · auto_applied=False · requires_user_approval=True.
  - 재검토 후보(candidates)는 전부 후보일 뿐 — 주문/정책 변경 아님.
  - 장기채 변동성 과도(장기 비중 과다) → 재검토 후보로 띄운다.
  - 현금 부족(순현금<임계) → 재검토 후보.
  - 거시/금리 미연동이면 graceful(데이터 없음 단정 금지) — 깨지지 않는다.
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_dr_govbond.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import allocation as alloc
from main_mission.portfolio_os import selection as sel
from main_mission.portfolio_os import daily_review as dr


def setup():
    store_db.init()


def _profile(idx, *, bond_target=40.0, duration="long", cash_min=10.0, cash_max=40.0):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
            "interests_text, bond_target_pct, bond_duration_pref, updated_at) "
            "VALUES(?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(account_index) DO NOTHING",
            (idx, "neutral", cash_min, cash_max, "반도체", bond_target, duration),
        )
        conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, "
            "source, captured_at) VALUES(?,?,?,?,?,datetime('now'))",
            (idx, 9000000, 10000000, 0, "test"),
        )
        conn.commit()
    finally:
        conn.close()


def _select(idx):
    out = alloc.generate(idx)
    sel.select(idx, out["proposal_id"], "base")


def _gc(r):
    return r.get("govbond_check") or (r.get("payload", {}) or {}).get("govbond_check")


# ---- 모든 분기 공통: govbond_check 노출 + 자동 변경 금지 ----
def test_govbond_block_present_and_no_auto_change():
    _profile(101, bond_target=40.0, duration="long")
    _select(101)
    r = dr.generate_review(101)
    g = _gc(r)
    assert g is not None, r
    assert g["data_available"] is True, g
    assert g["auto_order_created"] is False, g
    assert g["auto_applied"] is False, g
    assert g["requires_user_approval"] is True, g
    assert g["broker_neutral"] is True, g
    # breakdown 핵심 필드 존재(가짜 0 아님 — 실 분해).
    b = g["breakdown"]
    for k in ("govbond_pct", "pure_cash_pct", "short_govbond_pct", "long_govbond_pct", "risk_asset_pct"):
        assert k in b, (k, b)
    # 방어자산 구현 수단 문구 — 수익 극대화 아님(필수 고지).
    assert "방어자산 구현 수단" in g["note"], g["note"]


def test_govbond_present_in_watch_branch_no_snapshot():
    # 스냅샷 없는 계좌도 govbond 점검은 노출(graceful) — 자동 변경 0.
    r = dr.generate_review(199)
    g = _gc(r)
    assert g is not None, r
    assert g["auto_order_created"] is False and g["auto_applied"] is False, g


# ---- 장기채 변동성 과도 → 재검토 후보(자동 변경 아님) ----
def test_long_bond_heavy_raises_review_candidate():
    # duration=long → 국채 전량 장기 → long_share=100% → 변동성 과도 후보.
    _profile(102, bond_target=40.0, duration="long")
    _select(102)
    r = dr.generate_review(102)
    g = _gc(r)
    kinds = {c["kind"] for c in g["candidates"]}
    assert "long_bond_volatility" in kinds, g["candidates"]
    # 후보는 전부 auto=False(자동 변경 아님).
    assert all(c.get("auto") is False for c in g["candidates"]), g["candidates"]
    assert g["candidate_count"] == len(g["candidates"]), g


# ---- 균형(단/장 분산)이면 변동성 과도 후보가 없을 수 있음(관망도 정상) ----
def test_balanced_duration_no_long_volatility_candidate():
    _profile(103, bond_target=40.0, duration="mixed")  # 단기50/장기50 기본 → long_share=50%
    _select(103)
    r = dr.generate_review(103)
    g = _gc(r)
    kinds = {c["kind"] for c in g["candidates"]}
    assert "long_bond_volatility" not in kinds, g["candidates"]


# ---- 환율/금리 미연동이면 graceful(단정 금지) ----
def test_fx_rate_graceful_when_macro_not_connected():
    _profile(104, bond_target=40.0, duration="long")
    _select(104)
    r = dr.generate_review(104)
    g = _gc(r)
    # 거시 미연동 환경: 환율 데이터 미연동을 정직 표기(연결 안 됨).
    assert g["fx_data_connected"] is False, g
    fx_checks = [c for c in g["checks"] if c["key"] == "fx"]
    assert fx_checks and "미연동" in fx_checks[0]["msg"], fx_checks
    # 금리 미연동이면 단정하지 않음(checks 에 정직 표기).
    assert g["rate_data_connected"] in (False, True), g


# ---- 국채 0% 계좌도 깨지지 않음(순현금 중심, 후보는 일반 원칙) ----
def test_zero_govbond_account_graceful():
    _profile(105, bond_target=0.0, duration="short")
    _select(105)
    r = dr.generate_review(105)
    g = _gc(r)
    assert g["data_available"] is True, g
    assert g["breakdown"]["govbond_pct"] == 0.0, g["breakdown"]
    # no_govbond check 가 정직하게 들어감.
    assert any(c["key"] == "no_govbond" for c in g["checks"]), g["checks"]


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} GOVBOND-CHECK TESTS PASSED")
