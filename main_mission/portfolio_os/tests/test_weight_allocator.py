"""비중 조절 엔진 테스트 — 확정안 bucket 한도 안에서 배분.

핵심 검증(CEO 불변 원칙):
- 각 bucket 의 선택 종목 합 = 그 bucket 의 확정안 weight (**초과 0**).
- 총합 100 불변 (확정안 = 단일 진실, bucket 합 변경 금지).
- 단일종목 한도 / 섹터(테마) 한도 / 헤지(인버스) 총합 한도 준수 — 초과 시 차단 경고.
- 개별주 A/B/C 옵션은 **위험자산 안에서** carve(추가 비중 아님, 100 불변).
- draft 미반영: DB write 0, 자동주문 0, policy 변경 0.
- 확정안 없으면 정직하게 ok=False(확정안 truth 사용).

임시 SQLite 핀(SQLITE_PATH) + 직접 적재. Anthropic 미사용.
"""
from __future__ import annotations

import json
import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_weight_allocator.sqlite3")

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import weight_allocator as wa


def setup():
    os.environ["SQLITE_PATH"] = _TMP
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = _TMP + suffix
        if os.path.exists(p):
            os.remove(p)
    store_db.init()


# --------------------------------------------------------------------------- helpers
def _confirm(acct, rows, variant="base"):
    """확정안(allocation_selections active) 1행을 직접 적재."""
    conn = store_db.connect()
    try:
        conn.execute("UPDATE allocation_selections SET status='superseded' "
                     "WHERE account_index=? AND status='active'", (acct,))
        conn.execute(
            "INSERT INTO allocation_selections(account_index, proposal_id, variant, allocation, "
            "precheck_status, selected_by, status) VALUES(?,?,?,?,?,?,'active')",
            (acct, "p1", variant, json.dumps(rows, ensure_ascii=False), "pass", "user"))
        conn.commit()
    finally:
        conn.close()


def _add_universe(conn, acct, ticker, name, asset_class="equity_etf", is_inverse=0):
    conn.execute(
        "INSERT INTO universe_instruments(account_index, ticker, market, name, asset_class, "
        "is_inverse, is_active, source) VALUES(?,?,?,?,?,?,1,'manual')",
        (acct, ticker, "US", name, asset_class, is_inverse))
    conn.commit()


def _add_view(conn, acct, theme, ticker=None, etf=None, conviction=0.5, stance="positive"):
    conn.execute(
        "INSERT INTO user_views(account_index, layer, theme, ticker, etf, stance, conviction, status) "
        "VALUES(?,?,?,?,?,?,?,'active')",
        (acct, "mid", theme, ticker, etf, stance, conviction))
    conn.commit()


# 표준 확정안: 순현금 24, 국채 16(방어 40), 글로벌코어(anchor) 30, 반도체 tilt 20, 로봇 tilt 10
#   합 = 24+16+30+20+10 = 100
_BASE_ALLOC = [
    {"kind": "cash", "ref": None, "weight_pct": 24.0},
    {"kind": "bond", "ref": "국채·long", "weight_pct": 16.0},
    {"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": 30.0},
    {"kind": "tilt", "ref": "반도체", "weight_pct": 20.0},
    {"kind": "tilt", "ref": "로봇", "weight_pct": 10.0},
]


# --------------------------------------------------------------------------- 확정안 truth
def test_confirmed_buckets_parses_truth():
    setup()
    _confirm(1, _BASE_ALLOC)
    cb = wa.confirmed_buckets(1)
    assert cb["ok"] and cb["total_pct"] == 100.0 and cb["total_is_100"]
    keys = {b["key"] for b in cb["buckets"]}
    assert {"global_core", "semiconductor", "robotics"} <= keys, cb
    assert cb["defensive"]["cash_pct"] == 24.0
    assert cb["defensive"]["govbond_pct"] == 16.0


def test_no_confirmed_is_honest_block():
    setup()
    cb = wa.confirmed_buckets(1)
    assert cb["ok"] is False and "확정안" in cb["error"]
    a = wa.allocate(1, {"semiconductor": ["SOXX"]})
    assert a["ok"] is False  # 확정안 없이 배분 금지(truth)


# --------------------------------------------------------------------------- bucket 한도 준수
def test_bucket_sum_equals_bucket_weight_no_overflow():
    setup()
    _confirm(1, _BASE_ALLOC)
    # 반도체 bucket 20% → SOXX/SMH 두 종목 → 합이 정확히 20% (초과 0)
    a = wa.allocate(1, {"semiconductor": ["SOXX", "SMH"], "robotics": ["BOTZ"]})
    assert a["ok"]
    semi = [h for h in a["holdings"] if h["bucket"] == "semiconductor"]
    assert round(sum(h["weight_pct"] for h in semi), 1) == 20.0, semi
    assert all(h["weight_pct"] <= 20.0 for h in semi)
    # bucket_summary 의 headroom 0 (정확히 채움)
    semi_sum = next(b for b in a["bucket_summary"] if b["key"] == "semiconductor")
    assert semi_sum["allocated_pct"] == 20.0 and semi_sum["headroom_pct"] == 0.0


def test_total_stays_100_invariant():
    setup()
    _confirm(1, _BASE_ALLOC)
    a = wa.allocate(1, {"semiconductor": ["SOXX", "SMH"], "robotics": ["BOTZ"],
                        "global_core": ["SPY", "VOO"]})
    assert a["total_pct"] == 100.0 and a["total_is_100"], a["holdings"]
    # 방어자산(현금+국채)도 보존돼 holdings 에 있음
    assert any(h["kind"] == "cash" and h["weight_pct"] == 24.0 for h in a["holdings"])
    assert any(h["kind"] == "bond" and h["weight_pct"] == 16.0 for h in a["holdings"])


def test_unselected_bucket_weight_preserved():
    setup()
    _confirm(1, _BASE_ALLOC)
    # 반도체만 선택 → 로봇/앵커는 확정안 weight 보존(미배정)
    a = wa.allocate(1, {"semiconductor": ["SOXX"]})
    robo = next(h for h in a["holdings"] if h["bucket"] == "robotics")
    assert robo["weight_pct"] == 10.0 and robo["ticker"] is None
    assert a["total_pct"] == 100.0


# --------------------------------------------------------------------------- 단일종목 한도
def test_single_name_limit_blocks_overweight():
    setup()
    # 반도체 bucket 을 25% 로(>20 단일한도) 만들고 1종목만 선택 → 단일 25% > 20% block
    alloc = [
        {"kind": "cash", "ref": None, "weight_pct": 35.0},
        {"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": 40.0},
        {"kind": "tilt", "ref": "반도체", "weight_pct": 25.0},
    ]
    _confirm(1, alloc)
    a = wa.allocate(1, {"semiconductor": ["SOXX"]})
    blocks = [w for w in a["over_limit_warnings"] if w["level"] == "block"]
    assert any("단일종목 한도" in w["msg"] for w in blocks), a["over_limit_warnings"]
    assert a["blocked"] is True


def test_single_name_ok_when_split():
    setup()
    alloc = [
        {"kind": "cash", "ref": None, "weight_pct": 35.0},
        {"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": 40.0},
        {"kind": "tilt", "ref": "반도체", "weight_pct": 25.0},
    ]
    _confirm(1, alloc)
    # 25% 를 2종목으로 분산 → 각 12.5% < 20% (block 없음)
    a = wa.allocate(1, {"semiconductor": ["SOXX", "SMH"]})
    assert not any("단일종목 한도" in w["msg"] for w in a["over_limit_warnings"]), a["over_limit_warnings"]


# --------------------------------------------------------------------------- 헤지 한도
def test_hedge_total_limit():
    setup()
    # 헤지(인버스) bucket 을 15% 로(>10 한도) → 헤지 총합 초과 block
    alloc = [
        {"kind": "cash", "ref": None, "weight_pct": 30.0},
        {"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": 55.0},
        {"kind": "hedge", "ref": "반도체 인버스", "weight_pct": 15.0},
    ]
    _confirm(1, alloc)
    a = wa.allocate(1, {"semiconductor_inverse": ["SOXS"]})
    assert any("인버스 총합" in w["msg"] for w in a["over_limit_warnings"]), a["over_limit_warnings"]


# --------------------------------------------------------------------------- 검증 실패 종목
def test_invalid_ticker_excluded_with_warning():
    setup()
    _confirm(1, _BASE_ALLOC)
    a = wa.allocate(1, {"semiconductor": ["SOXX", "NOTREAL"]})
    warns = [w for w in a["over_limit_warnings"] if w.get("ticker") == "NOTREAL"]
    assert warns and "후보에 없음" in warns[0]["msg"]
    # 유효한 SOXX 1종에 bucket 전량(20%) 배분
    semi = [h for h in a["holdings"] if h["bucket"] == "semiconductor" and h["ticker"]]
    assert len(semi) == 1 and semi[0]["ticker"] == "SOXX" and semi[0]["weight_pct"] == 20.0


# --------------------------------------------------------------------------- 관점 가중
def test_view_weighting_skews_by_conviction():
    setup()
    _confirm(1, _BASE_ALLOC)
    conn = store_db.connect()
    try:
        _add_view(conn, 1, "반도체", etf="SOXX", conviction=0.9)
        _add_view(conn, 1, "반도체", etf="SMH", conviction=0.1)
    finally:
        conn.close()
    a = wa.allocate(1, {"semiconductor": ["SOXX", "SMH"]}, weighting="view")
    soxx = next(h for h in a["holdings"] if h["ticker"] == "SOXX")
    smh = next(h for h in a["holdings"] if h["ticker"] == "SMH")
    assert soxx["weight_pct"] > smh["weight_pct"], (soxx, smh)
    assert round(soxx["weight_pct"] + smh["weight_pct"], 1) == 20.0  # 합은 그대로


# --------------------------------------------------------------------------- 개별주 A/B/C
def test_individual_options_carve_from_risk():
    setup()
    _confirm(1, _BASE_ALLOC)  # 방어 40 → 위험 60
    out = wa.individual_bucket_options(1)
    assert out["ok"] and out["risk_asset_pct"] == 60.0
    assert out["options"]["A"]["individual_cap_pct"] == 0.0
    assert out["options"]["B"]["individual_cap_pct"] == 5.0
    assert out["options"]["C"]["individual_cap_pct"] == 10.0
    # 단일 상한 1~2% (CEO)
    assert out["options"]["C"]["per_name_max_pct"] <= 2.0
    assert out["options"]["C"]["carve_from"].startswith("위험자산")


def test_individual_option_capped_when_risk_small():
    setup()
    # 위험자산 7% 만 있는 방어형 → C(10%) 는 7% 로 cap
    alloc = [
        {"kind": "cash", "ref": None, "weight_pct": 93.0},
        {"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": 7.0},
    ]
    _confirm(1, alloc)
    out = wa.individual_bucket_options(1)
    assert out["risk_asset_pct"] == 7.0
    assert out["options"]["C"]["individual_cap_pct"] == 7.0
    assert out["options"]["C"]["capped_to_risk"] is True


# --------------------------------------------------------------------------- draft 미반영
def test_no_db_write_no_auto_order():
    setup()
    _confirm(1, _BASE_ALLOC)
    a = wa.allocate(1, {"semiconductor": ["SOXX", "SMH"]})
    assert a["auto_order_created"] is False
    assert a["requires_user_approval"] is True
    assert a["db_write"] is False
    conn = store_db.connect()
    try:
        orders = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
        # 확정안은 1개(setup 적재분)뿐 — allocate 가 새 selection 을 만들지 않음
        sels = conn.execute("SELECT COUNT(*) c FROM allocation_selections "
                            "WHERE account_index=1 AND status='active'").fetchone()["c"]
    finally:
        conn.close()
    assert orders == 0 and sels == 1


def test_no_anthropic_import():
    # 본 모듈은 Anthropic API 를 절대 import/사용하지 않는다(불변).
    # 문서에 'Anthropic API 0' 선언 문구는 허용 — import/SDK/키 사용만 금지.
    import main_mission.portfolio_os.weight_allocator as mod
    src = open(mod.__file__, encoding="utf-8").read()
    assert "import anthropic" not in src
    assert "from anthropic" not in src
    assert "ANTHROPIC_API_KEY" not in src
