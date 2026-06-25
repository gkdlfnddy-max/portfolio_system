"""user_views — CRUD · layer 분리 · supersede 이력 · compare_view_vs_data · 계좌 격리.

키 없이 임시 SQLite로 전 경로 검증. (Anthropic API 미사용)
import 전에 임시 SQLITE_PATH 주입 → setup() 에서 store_db.init() (격리 필수).
"""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_user_views.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import user_views as uv


def setup():
    store_db.init()


# ──────────────── CRUD + layer 분리 ────────────────
def test_add_and_list():
    r = uv.add(1, layer="long", theme="반도체", stance="positive", conviction=0.7, note="장기 긍정")
    assert r["ok"] and r["view"]["id"] >= 1
    assert r["view"]["layer"] == "long"
    assert r["view"]["stance"] == "positive"
    assert r["view"]["conviction"] == 0.7
    assert r["view"]["status"] == "active"
    views = uv.list_views(1)
    assert any(v["theme"] == "반도체" for v in views)


def test_layers_separated():
    uv.add(2, layer="grand", note="공격적으로")
    uv.add(2, layer="mid", theme="바이오", etf="ARKG", stance="positive")
    uv.add(2, layer="short", theme="반도체", stance="negative", note="단기 고점")
    uv.add(2, layer="long", theme="반도체", stance="positive", note="장기 긍정")
    bl = uv.by_layer(2)
    assert len(bl["grand"]) == 1
    assert len(bl["mid"]) == 1
    assert len(bl["short"]) == 1
    assert len(bl["long"]) == 1
    # 같은 테마라도 단기/장기 견해가 다르게 공존
    assert bl["short"][0]["stance"] == "negative"
    assert bl["long"][0]["stance"] == "positive"


def test_invalid_enums_rejected():
    for kwargs in (
        {"layer": "nope"},
        {"layer": "long", "stance": "buy"},
        {"layer": "long", "conviction": 1.5},
    ):
        try:
            uv.add(3, **kwargs)
            assert False, f"should reject {kwargs}"
        except ValueError:
            pass


# ──────────────── supersede 이력 보존 ────────────────
def test_update_supersedes_old():
    r = uv.add(4, layer="long", theme="로봇", stance="neutral", conviction=0.3)
    old_id = r["view"]["id"]
    u = uv.update(4, old_id, stance="positive", conviction=0.6, note="조금 늘림")
    assert u["ok"]
    new_id = u["view"]["id"]
    assert new_id != old_id
    assert u["view"]["stance"] == "positive"
    assert u["view"]["conviction"] == 0.6
    # 변경되지 않은 필드는 유지
    assert u["view"]["theme"] == "로봇"

    # 옛 행은 superseded + superseded_by 로 이력 보존
    old = uv.get(4, old_id)
    assert old["status"] == "superseded"
    assert old["superseded_by"] == new_id

    # active 목록엔 새 것만
    actives = uv.list_views(4)
    assert [v["id"] for v in actives] == [new_id]

    # 이력 체인 추적
    chain = uv.history(4, old_id)
    assert [c["id"] for c in chain] == [old_id, new_id]


def test_cannot_update_non_active():
    r = uv.add(5, layer="mid", theme="양자")
    vid = r["view"]["id"]
    uv.update(5, vid, stance="observe")           # supersede
    bad = uv.update(5, vid, stance="positive")    # 이미 superseded
    assert not bad["ok"]


def test_archive():
    r = uv.add(6, layer="long", theme="2차전지")
    vid = r["view"]["id"]
    a = uv.archive(6, vid)
    assert a["ok"] and a["view"]["status"] == "archived"
    assert uv.list_views(6) == []
    assert any(v["status"] == "archived" for v in uv.list_views(6, include_superseded=True))


# ──────────────── 계좌 격리 (교차적용 금지) ────────────────
def test_account_isolation():
    r = uv.add(7, layer="long", theme="반도체", stance="positive")
    vid = r["view"]["id"]
    # 다른 계좌(8)에서 계좌7 견해를 못 본다/못 만진다
    assert uv.get(8, vid) is None
    assert all(v["account_index"] == 8 for v in uv.list_views(8))
    bad = uv.update(8, vid, stance="negative")
    assert not bad["ok"]
    bad2 = uv.archive(8, vid)
    assert not bad2["ok"]
    # 계좌7 견해는 그대로 active
    assert uv.get(7, vid)["status"] == "active"


# ──────────────── compare_view_vs_data ────────────────
def test_compare_agree():
    uv.add(10, layer="long", theme="반도체", stance="positive", conviction=0.8)
    out = uv.compare_view_vs_data(10, theme="반도체", data_signal={"risk_level": "low", "risk_score": 10})
    assert out["result"] == "agree", out


def test_compare_conflict():
    # 사용자는 긍정인데 데이터는 하락 위험↑ → 정면 충돌
    uv.add(11, layer="long", theme="반도체", stance="positive", conviction=0.8)
    out = uv.compare_view_vs_data(11, theme="반도체", data_signal={"risk_level": "high", "risk_score": 75})
    assert out["result"] == "conflict", out
    # 정직: 단정 금지 — 둘 다 제시
    assert out["view"] is not None
    assert out["data"]["direction"] == -1
    assert "단정" in out["explanation"]


def test_compare_observe():
    uv.add(12, layer="short", theme="양자", stance="observe")
    out = uv.compare_view_vs_data(12, theme="양자", data_signal={"risk_level": "high", "risk_score": 80})
    assert out["result"] == "observe", out


def test_compare_no_view():
    out = uv.compare_view_vs_data(13, ticker="005930", data_signal={"direction": "down"})
    assert out["result"] == "no_view", out
    assert out["view"] is None


def test_compare_differ_when_neutral():
    uv.add(14, layer="long", theme="로봇", stance="neutral")
    out = uv.compare_view_vs_data(14, theme="로봇", data_signal={"direction": "down"})
    assert out["result"] == "differ", out


def test_compare_ticker_and_etf_match():
    uv.add(15, layer="long", ticker="005930", stance="positive")
    uv.add(15, layer="mid", etf="ARKG", stance="negative")
    by_ticker = uv.compare_view_vs_data(15, ticker="005930", data_signal={"direction": "up"})
    assert by_ticker["result"] == "agree", by_ticker
    by_etf = uv.compare_view_vs_data(15, ticker="ARKG", data_signal={"direction": "up"})
    assert by_etf["result"] == "conflict", by_etf


def test_compare_account_isolation():
    uv.add(16, layer="long", theme="반도체", stance="positive")
    # 계좌17 에는 견해 없음 → 계좌16 견해가 새지 않는다
    out = uv.compare_view_vs_data(17, theme="반도체", data_signal={"direction": "up"})
    assert out["result"] == "no_view", out


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} USER-VIEWS TESTS PASSED")
