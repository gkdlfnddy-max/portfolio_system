"""관점별 후보(A/B/C안) + 같은 데이터 다른 해석 테스트.

검증(불변 안전 — CLAUDE.md §2, §4):
  - A/B/C 3안 생성: 각 안에 요약/이유/비중/장점/위험/트리거/추가확인 존재.
  - 비중 합계 100·섹터/인버스 한도 준수(base _variant 검증 재사용).
  - 관점별 차이: B(방어) 현금 ≥ A ≥ C(공격) 현금 / C 위험자산 ≥ A ≥ B.
  - 목적(investor_objective) 미설정 → 정직 표기(set=False, '목적 미설정').
  - 목적 설정(손실축소) → C 라도 '목적 안에서 절제' 표기.
  - **자동 차단**: A/B/C 는 draft 만 — compile_policy 불변(accepted 만 읽음).
  - 같은 데이터 다른 해석 출력 포맷 키 존재 + 충돌 시 mixed_swing.
  - 자동주문 0 · Anthropic API 미사용.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_perspective.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import perspective_variants as pv
from main_mission.portfolio_os import portfolio_impact as impact_mod
from main_mission.portfolio_os import policy as policy_mod
from main_mission.portfolio_os import price_history as ph

_PREV = None


def setup():
    store_db.init()


def setup_function(_fn=None):
    global _PREV
    _PREV = os.environ.get("SQLITE_PATH")
    os.environ["SQLITE_PATH"] = _TMP
    if os.path.exists(_TMP):
        os.remove(_TMP)
    store_db._bootstrapped = False
    store_db.init()


def teardown_function(_fn=None):
    if _PREV is not None:
        os.environ["SQLITE_PATH"] = _PREV


# ----------------------------------------------------------------
# 합성 데이터
# ----------------------------------------------------------------
def _profile(conn, idx, *, cmin=10.0, cmax=40.0, risk="neutral", interests="반도체, 바이오"):
    conn.execute(
        "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
        "interests_text, bond_target_pct, updated_at) VALUES(?,?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(account_index) DO NOTHING",
        (idx, risk, cmin, cmax, interests, 25.0))


def _user_view(conn, idx, **kw):
    conn.execute(
        "INSERT INTO user_views(account_index, layer, theme, ticker, etf, stance, conviction, "
        "horizon, note, status) VALUES(?,?,?,?,?,?,?,?,?, 'active')",
        (idx, kw.get("layer", "long"), kw.get("theme"), kw.get("ticker"), kw.get("etf"),
         kw.get("stance"), kw.get("conviction"), kw.get("horizon"), kw.get("note", "")))


def _objective(conn, idx, objective):
    """투자 목적 저장 — investor_objective.set_objective (user_views layer='objective'). A↔B 통합 경로."""
    from main_mission.portfolio_os import investor_objective as io
    io.set_objective(idx, {"investment_goal": objective, "risk_tolerance": "low"})


def _bars(closes, *, start="2025-01-01"):
    d0 = date.fromisoformat(start)
    return [{"date": (d0 + timedelta(days=i)).isoformat(), "open": round(c, 4),
             "high": round(c * 1.01, 4), "low": round(c * 0.99, 4),
             "close": round(c, 4), "volume": 1000.0} for i, c in enumerate(closes)]


def _crash_history():
    up = [100.0 + i for i in range(60)]
    peak = up[-1]
    return _bars(up + [peak * (1 - 0.03 * (k + 1)) for k in range(15)])


def _bucket(c, kind):
    return c["weights"][kind]


# ----------------------------------------------------------------
# 1. A/B/C 3안 구조 + 합계 100
# ----------------------------------------------------------------
def test_three_perspectives_structure_and_sum100():
    idx = 201
    conn = store_db.connect()
    try:
        _profile(conn, idx, cmin=10.0, cmax=40.0)
        _user_view(conn, idx, theme="반도체", stance="positive", conviction=0.7, horizon="long")
        conn.commit()
    finally:
        conn.close()

    out = pv.generate(idx)
    assert out["ok"], out
    perspectives = [c["perspective"] for c in out["candidates"]]
    assert perspectives == ["A", "B", "C"], perspectives
    for c in out["candidates"]:
        # 필수 서술 필드
        for k in ("summary", "why_fits_user", "weights", "pros", "risks",
                  "break_triggers", "more_to_confirm"):
            assert c.get(k), (c["perspective"], k)
        # 비중 합계 100
        assert c["weights"]["total"] == 100.0, c["weights"]
        # 자동 차단 플래그
        assert c["requires_user_approval"] is True
        assert c["auto_applied"] is False and c["auto_order_created"] is False
    assert out["auto_order_created"] is False and out["requires_user_approval"] is True


# ----------------------------------------------------------------
# 2. 관점별 해석 차이 — B 더 방어(현금↑), C 더 공격(위험↑)
# ----------------------------------------------------------------
def test_perspective_difference_defensive_vs_aggressive():
    idx = 202
    conn = store_db.connect()
    try:
        _profile(conn, idx, cmin=10.0, cmax=40.0)
        _user_view(conn, idx, theme="반도체", stance="positive", conviction=0.7, horizon="long")
        conn.commit()
    finally:
        conn.close()
    out = pv.generate(idx)
    cand = {c["perspective"]: c for c in out["candidates"]}
    a, b, c = cand["A"], cand["B"], cand["C"]
    # B 더 방어적: 방어 총량(순현금+국채) ≥ A ≥ C
    assert _bucket(b, "defensive") >= _bucket(a, "defensive") >= _bucket(c, "defensive"), (
        _bucket(b, "defensive"), _bucket(a, "defensive"), _bucket(c, "defensive"))
    # C 더 공격적: 위험자산 ≥ A ≥ B
    assert _bucket(c, "risk_assets") >= _bucket(a, "risk_assets") >= _bucket(b, "risk_assets"), (
        _bucket(c, "risk_assets"), _bucket(a, "risk_assets"), _bucket(b, "risk_assets"))
    # 셋이 전부 동일하면 '관점 차이'가 아님 — 최소 하나는 달라야 함
    defs = {_bucket(a, "defensive"), _bucket(b, "defensive"), _bucket(c, "defensive")}
    assert len(defs) >= 2, defs


# ----------------------------------------------------------------
# 3. 한도 준수 — 인버스/섹터
# ----------------------------------------------------------------
def test_limits_respected_inverse_and_sector():
    idx = 203
    conn = store_db.connect()
    try:
        _profile(conn, idx, interests="반도체, 바이오, 로봇")
        # 헤지 의도 테마 → 인버스 후보
        conn.execute("UPDATE investor_profile SET hedge_themes=? WHERE account_index=?",
                     ("반도체", idx))
        _user_view(conn, idx, theme="반도체", stance="positive", conviction=0.7, horizon="long")
        conn.commit()
    finally:
        conn.close()
    out = pv.generate(idx)
    pol = policy_mod.compile_policy(idx)
    inverse_max = pol["limits"]["inverse_max_pct"]
    sector_max = pol["limits"]["sector_max_pct"]
    for c in out["candidates"]:
        hedge_total = sum(r["weight_pct"] for r in c["rows"] if r["kind"] == "hedge")
        assert hedge_total <= inverse_max + 0.05, (c["perspective"], hedge_total, inverse_max)
        for r in c["rows"]:
            if r["kind"] == "tilt":
                assert r["weight_pct"] <= sector_max + 0.05, (c["perspective"], r)


# ----------------------------------------------------------------
# 4. 목적 미설정 → 정직 표기
# ----------------------------------------------------------------
def test_objective_unset_honest():
    idx = 204
    conn = store_db.connect()
    try:
        _profile(conn, idx)
        _user_view(conn, idx, theme="반도체", stance="positive", conviction=0.7, horizon="long")
        conn.commit()
    finally:
        conn.close()
    out = pv.generate(idx)
    assert out["objective"]["set"] is False, out["objective"]
    assert "목적 미설정" in out["objective"]["note"], out["objective"]
    # A안 설명에도 견해만 반영됨이 드러남
    a = next(c for c in out["candidates"] if c["perspective"] == "A")
    assert "목적" in a["why_fits_user"] or "미설정" in a["why_fits_user"], a["why_fits_user"]


# ----------------------------------------------------------------
# 5. 목적='손실 축소' → C 라도 절제 표기 + lean 반영
# ----------------------------------------------------------------
def test_objective_loss_min_tempers_aggressive():
    idx = 205
    conn = store_db.connect()
    try:
        _profile(conn, idx)
        _user_view(conn, idx, theme="반도체", stance="positive", conviction=0.7, horizon="long")
        conn.commit()
    finally:
        conn.close()
    # 목적 저장은 자체 connection 사용(set_objective) — 위 conn 의 쓰기 락 해제 후 호출(잠김 방지).
    _objective(None, idx, "loss_reduction")  # A 정식 enum(→ drawdown_min)
    out = pv.generate(idx)
    assert out["objective"]["set"] is True, out["objective"]
    assert out["objective"]["optimize"] == "drawdown_min", out["objective"]
    c = next(x for x in out["candidates"] if x["perspective"] == "C")
    assert "절제" in c["why_fits_user"], c["why_fits_user"]


# ----------------------------------------------------------------
# 6. 자동 적용 차단 — draft 저장해도 compile_policy 불변
# ----------------------------------------------------------------
def test_draft_does_not_change_compile_policy():
    idx = 206
    conn = store_db.connect()
    try:
        _profile(conn, idx)
        _user_view(conn, idx, theme="반도체", stance="positive", conviction=0.7, horizon="long")
        conn.commit()
    finally:
        conn.close()

    before = policy_mod.compile_policy(idx)
    out = pv.generate(idx)
    assert out["draft_rows_saved"] >= 1, out
    # target_allocations 에 status='draft' 로만 저장
    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT status FROM target_allocations WHERE account_index=? AND proposal_id=?",
            (idx, out["proposal_id"])).fetchall()
        assert rows and all(r["status"] == "draft" for r in rows), [dict(r) for r in rows]
        variants = conn.execute(
            "SELECT DISTINCT variant FROM target_allocations WHERE proposal_id=? ORDER BY variant",
            (out["proposal_id"],)).fetchall()
        assert {r["variant"] for r in variants} == {"A", "B", "C"}, variants
    finally:
        conn.close()

    after = policy_mod.compile_policy(idx)
    b = {k: v for k, v in before.items() if k != "compiled_at"}
    a = {k: v for k, v in after.items() if k != "compiled_at"}
    assert b == a, "draft 저장이 policy 를 바꿈(자동 적용 금지 위반)"


# ----------------------------------------------------------------
# 7. 같은 데이터 다른 해석 출력 포맷 + 충돌 시 mixed_swing
# ----------------------------------------------------------------
def test_different_interpretations_format_and_conflict():
    idx = 207
    ph.upsert_bars("SEMI", _crash_history(), "test")  # 단기 하락 신호 강함
    conn = store_db.connect()
    try:
        _profile(conn, idx)
        # 보유 + 장기긍정 ↔ 단기부정 = 충돌(mixed_swing)
        cur = conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, "
            "holdings_count, source, captured_at) VALUES(?,?,?,?,?,datetime('now'))",
            (idx, 3000000, 10000000, 1, "test"))
        sid = cur.lastrowid
        conn.execute(
            "INSERT INTO holdings(snapshot_id, account_index, ticker, name, qty, avg_price, "
            "market_value, captured_at) VALUES(?,?,?,?,?,?,?,datetime('now'))",
            (sid, idx, "SEMI", "SEMI", 10, 5000, 7000000))
        conn.execute(
            "INSERT INTO universe_instruments(account_index, ticker, name, asset_class, is_active) "
            "VALUES(?,?,?,?,1)", (idx, "SEMI", "SEMI", "semiconductor_etf"))
        _user_view(conn, idx, ticker="SEMI", stance="positive", conviction=0.8, horizon="long")
        _user_view(conn, idx, ticker="SEMI", stance="negative", conviction=0.6, horizon="short")
        conn.commit()
    finally:
        conn.close()

    out = impact_mod.different_interpretations(idx)
    assert out["ok"], out
    # 포맷 키 순서/존재
    for k in ("common_facts", "user_perspective", "interpretations", "portfolio_impact",
              "selectable_candidates", "candidate_pros_cons", "requires_user_approval"):
        assert k in out, k
    # 선택 가능 후보 = A/B/C
    assert {c["perspective"] for c in out["selectable_candidates"]} == {"A", "B", "C"}
    # 충돌 종목은 단정 금지(mixed_swing)
    semi = next(i for i in out["interpretations"] if i["instrument_code"] == "SEMI")
    assert semi["alignment"] == "conflict", semi
    assert semi["mixed_swing"] is True and "mixed_swing" in semi["reading"], semi
    assert out["auto_order_created"] is False and out["requires_user_approval"] is True


# ----------------------------------------------------------------
# 8. Anthropic API 미사용
# ----------------------------------------------------------------
def test_no_anthropic_import():
    import pathlib
    base = pathlib.Path(__file__).resolve().parents[1]
    for mod in ("perspective_variants.py", "allocation.py", "portfolio_impact.py"):
        text = base.joinpath(mod).read_text(encoding="utf-8")
        low = text.lower()
        assert "import anthropic" not in low
        assert "from anthropic" not in low
        assert "anthropic-ai" not in low
        assert "ANTHROPIC_API_KEY" not in text
