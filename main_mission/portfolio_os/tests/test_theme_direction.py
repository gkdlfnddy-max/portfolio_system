"""관심 테마 방향성 분류 — 자동 롱 금지, 견해에서 방향 추출 (CEO 지시)."""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_themedir.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import field_advisors as fa


def setup():
    store_db.init()


def _seed_views(idx, views):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, interests_text, views_text, updated_at) "
            "VALUES(?,?,?,datetime('now')) ON CONFLICT(account_index) DO UPDATE SET views_text=excluded.views_text",
            (idx, "", views),
        )
        conn.commit()
    finally:
        conn.close()


def _by_theme(out):
    return {c["theme"]: c for c in out["extracted_variables"]["classified"]}


def test_semiconductor_short_when_overheated():
    _seed_views(21, "반도체는 너무 고점인거 같아서 숏 관점으로 기회를 보는게 좋을 것 같아")
    out = fa.theme_advisor(21, "반도체")
    c = _by_theme(out)["반도체"]
    assert c["direction"] == "short_or_hedge_candidate", c
    assert c["is_hedge_candidate"] and not c["is_long_candidate"], c
    assert c["role"] == "hedge", c
    assert c["evidence_quote"], c  # 근거 문장 저장


def test_mixed_swing_for_semiconductor():
    # CEO 예시1: 장기 성장 + 단기 과열 공존 → mixed_swing (단순 롱/숏 아님, 스윙 노출관리).
    _seed_views(22, "반도체는 장기적으로 유망하지만 지금은 고점이라 과열이라 인버스로 헤지도 본다")
    out = fa.theme_advisor(22, "반도체")
    c = _by_theme(out)["반도체"]
    assert c["direction"] == "mixed_swing", c          # 롱+숏 신호 공존
    assert c["role"] == "swing", c                       # allocation 역할 = swing(롱+인버스 페어)
    assert c["evidence_quote"], c
    # 회귀: allocation 에서 mixed_swing 은 롱 tilt + 인버스 둘 다(스윙 페어) — 단순 롱 금지.
    from main_mission.portfolio_os import allocation as a
    rows = a._variant("base", 40, ["반도체"], ["반도체"], sector_max=30, inverse_max=10, bond_pct=0)
    kinds = {r["kind"] for r in rows}
    assert "tilt" in kinds and "hedge" in kinds, rows   # 롱과 헤지 분리·공존


def test_unknown_when_no_view_hint():
    _seed_views(22, "")  # 견해 없음 → 방향 미정 (롱 아님)
    out = fa.theme_advisor(22, "양자컴퓨터")
    c = _by_theme(out)["양자컴퓨터"]
    assert c["direction"] == "unknown_direction", c
    assert c["role"] is None, c              # allocation 미반영
    assert c["needs_clarification"] is True, c


def test_no_auto_long_for_watch():
    _seed_views(23, "반도체는 관심만 있고 아직 모르겠어")
    out = fa.theme_advisor(23, "반도체")
    c = _by_theme(out)["반도체"]
    assert c["direction"] in ("watch_only", "unknown_direction"), c
    assert c["role"] is None, c              # 자동 롱 금지


def test_long_and_hedge_roles_split():
    _seed_views(24, "반도체는 인버스로 헤지하고 싶고, 로봇은 장기성장이라 분할 매수하고 싶어")
    out = fa.theme_advisor(24, "반도체, 로봇")
    cls = _by_theme(out)
    assert cls["로봇"]["direction"] == "long_candidate" and cls["로봇"]["role"] == "long", cls["로봇"]
    assert cls["로봇"]["allocation_role"] == "growth_tilt", cls["로봇"]
    assert cls["반도체"]["role"] == "hedge", cls["반도체"]   # 숏/헤지 (롱 아님)


def test_only_long_candidate_counts_for_tilt():
    # 롱 후보만 tilt cap 대상 — 방향 미정/관망은 제외
    _seed_views(25, "로봇은 사고싶어")  # 로봇만 롱, 나머지 미정
    out = fa.theme_advisor(25, "로봇, 바이오, 양자컴퓨터")
    caps = out["extracted_variables"]["per_theme_tilt_cap_pct"]
    assert "로봇" in caps, caps
    assert "바이오" not in caps and "양자컴퓨터" not in caps, caps  # 미정은 tilt 미반영


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} THEME-DIRECTION TESTS PASSED")
