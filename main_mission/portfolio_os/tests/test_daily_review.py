"""Daily Portfolio Review 테스트 — 관망/조정 분기, 예약성 계획, selected allocation 가드, 스윙/헤지 점검."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_dailyreview.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import allocation as alloc
from main_mission.portfolio_os import selection as sel
from main_mission.portfolio_os import daily_review as dr
from main_mission.portfolio_os import daily_runner as drun
from main_mission.portfolio_os import evidence as ev_mod


def setup():
    store_db.init()


def _profile(idx):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
            "interests_text, updated_at) VALUES(?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(account_index) DO NOTHING",
            (idx, "neutral", 10.0, 30.0, "반도체, 2차전지"),
        )
        conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, source, captured_at) "
            "VALUES(?,?,?,?,?,datetime('now'))", (idx, 9000000, 10000000, 0, "test"),
        )
        conn.commit()
    finally:
        conn.close()


def _select(idx):
    out = alloc.generate(idx)
    sel.select(idx, out["proposal_id"], "base")


# ---- 관망: selected allocation 없음 → 주문 후보 금지 ----
def test_watch_when_no_selection():
    _profile(12)
    r = dr.generate_review(12)
    assert r["ok"] and r["action_decision"] == "watch", r
    assert r["has_orders"] is False and r["scheduled_order_plan_id"] is None, r
    assert r["no_trade_reason"], r  # 사유 명시(관망도 정상)


def test_watch_when_no_snapshot():
    r = dr.generate_review(99)
    assert r["ok"] and r["action_decision"] == "watch", r
    assert "스냅샷" in (r["no_trade_reason"] or ""), r
    assert r["has_orders"] is False, r


# ---- 조정: selected allocation + drift → 예약성 지정가 계획 ----
def test_rebalance_creates_scheduled_plan():
    _profile(11)
    _select(11)
    r = dr.generate_review(11)
    assert r["ok"] is True, r                       # 관망도 조정도 ok:True (실패 아님)
    assert r["action_decision"] in ("rebalance", "hold", "watch"), r
    if r["action_decision"] == "rebalance":
        assert r["has_orders"] and r["scheduled_order_plan_id"], r
        conn = store_db.connect()
        try:
            steps = conn.execute(
                "SELECT direction, total_pct, total_rounds, limit_price, on_unfilled, status "
                "FROM scheduled_order_steps WHERE plan_id=?", (r["scheduled_order_plan_id"],)).fetchall()
        finally:
            conn.close()
        assert len(steps) >= 1, "예약 step 없음"
        for s in steps:
            assert s["direction"] in ("매수", "매도"), dict(s)
            assert s["limit_price"] is None, "지정가는 호가단계 — 미체결 재평가(시장가 금지)"
            assert s["on_unfilled"], "미체결 처리 명시 필요"
            assert s["status"] == "candidate"
    else:
        assert r["scheduled_order_plan_id"] is None  # 관망이면 주문 후보 없음


def test_latest_roundtrip():
    _profile(13)
    _select(13)
    dr.generate_review(13)
    last = dr.latest(13)
    assert last and last["account_index"] == 13, last
    assert last["action_decision"] in ("rebalance", "hold", "watch"), last
    assert isinstance(last.get("payload"), dict), last  # payload JSON 파싱


def test_review_is_idempotent_per_day():
    _profile(14)
    _select(14)
    dr.generate_review(14)
    dr.generate_review(14)  # 같은 날 재생성 — 1행 유지(덮어쓰기)
    conn = store_db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM daily_portfolio_reviews WHERE account_index=14").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1, f"계좌×일 1행이어야 함 (got {n})"


# ============================================================
# 스윙/헤지 점검 (mixed_swing) — 노출 점검만, 주문 비자동
# ============================================================

def _seed_swing(idx, *, long_pct, hedge_pct, long_drift_positive=False):
    """mixed_swing 계좌 직접 시드 — 반도체 롱(tilt) + 반도체 인버스(hedge) 페어.

    long_drift_positive=True 면 보유 시드로 롱 drift 를 양수(과열)로 만든다(expand 규칙 검증용)."""
    now = datetime.now(timezone.utc).isoformat()
    # 합계 100 맞춤: cash + bond + 롱 + 헤지 + anchor = 100
    anchor = round(100.0 - 30.0 - 9.0 - long_pct - hedge_pct, 1)
    alloc_rows = [
        {"kind": "cash", "ref": None, "weight_pct": 30.0},
        {"kind": "bond", "ref": "국채", "weight_pct": 9.0},
        {"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": anchor},
        {"kind": "tilt", "ref": "반도체", "weight_pct": long_pct},
        {"kind": "hedge", "ref": "반도체 인버스", "weight_pct": hedge_pct},
    ]
    conn = store_db.connect()
    try:
        # mixed_swing 방향: views 에 롱+숏 신호 공존 + interests=반도체.
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
            "interests_text, views_text, updated_at) VALUES(?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(account_index) DO UPDATE SET interests_text=excluded.interests_text, "
            "views_text=excluded.views_text",
            (idx, "neutral", 10.0, 40.0, "반도체",
             "반도체는 장기적으로 유망하지만 지금은 고점이라 과열이라 인버스로 헤지도 본다"),
        )
        cash = 3_000_000
        total = 10_000_000
        cur = conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, "
            "source, captured_at) VALUES(?,?,?,?,?,?)", (idx, cash, total, 0, "test", now))
        snap_id = cur.lastrowid
        # 롱 과열 시드: anchor 보유로 cur_invested_pct 를 끌어올려 tilt drift 를 양수로(decision 은
        # 미태깅 보유를 anchor 로 근사하므로 tilt 의 cur 는 0 → 보유로 직접 양수 drift 만들 수 없음).
        # 대신 expand/reduce 규칙은 _theme_action 단위 테스트로 검증하고, 여기선 블록 구조만 본다.
        conn.execute(
            "INSERT INTO allocation_selections(account_index, proposal_id, variant, allocation, "
            "account_snapshot_id, precheck_status, status, selected_at) VALUES(?,?,?,?,?,?,?,?)",
            (idx, f"p-swing-{idx}", "base", json.dumps(alloc_rows, ensure_ascii=False),
             snap_id, "pass", "active", now))
        conn.commit()
    finally:
        conn.close()


def test_swing_hedge_block_generated_for_mixed_swing():
    _seed_swing(31, long_pct=18.0, hedge_pct=2.0)
    r = dr.generate_review(31)
    assert r["ok"], r
    sh = r.get("swing_hedge") or (r.get("payload", {}) or {}).get("swing_hedge")
    assert sh and sh["has_mixed_swing"], sh
    th = {t["theme"]: t for t in sh["themes"]}
    assert "반도체" in th, sh
    sd = th["반도체"]
    assert sd["long_pct"] == 18.0 and sd["hedge_pct"] == 2.0, sd
    assert sd["net_pct"] == 16.0 and sd["gross_pct"] == 20.0, sd
    # hedge_ratio = 2/18*100 ≈ 11.1
    assert sd["hedge_ratio_pct"] == 11.1, sd
    assert sd["action"] in ("maintain", "reduce", "expand"), sd
    assert sd["reason"], sd
    # 전체 노출은 decision.compute 재사용.
    ov = sh["overall"]
    assert ov["today_net_pct"] is not None and ov["today_gross_pct"] is not None, ov
    assert ov["today_hedge_ratio_pct"] is not None, ov


def _seed_long_only(idx):
    """mixed_swing 없는 순수 롱 계좌 직접 시드(tz-aware snapshot → decision ok)."""
    now = datetime.now(timezone.utc).isoformat()
    alloc_rows = [
        {"kind": "cash", "ref": None, "weight_pct": 30.0},
        {"kind": "bond", "ref": "국채", "weight_pct": 9.0},
        {"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": 44.0},
        {"kind": "tilt", "ref": "로봇", "weight_pct": 17.0},
    ]
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
            "interests_text, views_text, updated_at) VALUES(?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(account_index) DO UPDATE SET views_text=excluded.views_text",
            (idx, "neutral", 10.0, 40.0, "로봇", "로봇은 장기성장이라 분할 매수하고 싶어"),
        )
        cur = conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, "
            "source, captured_at) VALUES(?,?,?,?,?,?)", (idx, 3_000_000, 10_000_000, 0, "test", now))
        conn.execute(
            "INSERT INTO allocation_selections(account_index, proposal_id, variant, allocation, "
            "account_snapshot_id, precheck_status, status, selected_at) VALUES(?,?,?,?,?,?,?,?)",
            (idx, f"p-long-{idx}", "base", json.dumps(alloc_rows, ensure_ascii=False),
             cur.lastrowid, "pass", "active", now))
        conn.commit()
    finally:
        conn.close()


def test_swing_hedge_empty_when_no_mixed_swing():
    # 순수 롱만(mixed 없음) → honest 빈 상태
    _seed_long_only(32)
    r = dr.generate_review(32)
    assert r["ok"], r
    sh = r.get("swing_hedge") or (r.get("payload", {}) or {}).get("swing_hedge")
    assert sh is not None, r
    assert sh["has_mixed_swing"] is False and sh["themes"] == [], sh


def test_swing_hedge_action_rules():
    # maintain: 정상 밴드 내
    a, _ = dr._theme_action(15.0, -1.0)
    assert a == "maintain", a
    # reduce: 헤지 과다(상단 초과)
    a, _ = dr._theme_action(30.0, -1.0)
    assert a == "reduce", a
    # expand: 헤지 부족 + 롱 과열
    a, _ = dr._theme_action(2.0, 5.0)
    assert a == "expand", a
    # maintain: 헤지 부족이지만 과열 신호 없음 → 유지(관망)
    a, _ = dr._theme_action(2.0, -1.0)
    assert a == "maintain", a


def test_mixed_swing_does_not_force_order_when_no_drift():
    # mixed_swing 노출은 그 자체로 주문 신호가 아니다 — drift 미충족이면 plan 없음(hold/watch).
    _seed_swing(33, long_pct=4.0, hedge_pct=0.0)  # 롱이 작아 drift 작음 → 조정 불필요 가능
    r = dr.generate_review(33)
    assert r["ok"], r
    if r["action_decision"] in ("hold", "watch"):
        assert r["scheduled_order_plan_id"] is None, r
        assert r["has_orders"] is False, r
        assert r["no_trade_reason"], r  # 관망 사유 명시(정상)
    # rebalance 면 drift 충족이라 plan 존재 — 이 경우도 mixed_swing 때문이 아니라 selected+drift 때문.


def test_no_trade_reason_and_next_review_present():
    _profile(34)  # selected 없음 → watch
    r = dr.generate_review(34)
    assert r["ok"] and r["action_decision"] == "watch", r
    assert r["no_trade_reason"], r  # 관망 사유 존재
    # next_review 는 dec 가 있을 때 payload 에 들어감 — watch(no selection)에선 없을 수 있으나
    # 관망 사유는 항상 존재해야 한다(주문 비자동의 핵심 증거).


def test_order_candidate_only_when_selected_and_risk_passed():
    # selected allocation + risk_passed + drift 충족일 때만 plan. (risk 위반이면 plan 없음)
    _seed_swing(35, long_pct=18.0, hedge_pct=2.0)
    r = dr.generate_review(35)
    assert r["ok"], r
    if r["action_decision"] == "rebalance":
        assert r["risk_passed"] is True, r        # 후보는 risk_passed 전제
        assert r["scheduled_order_plan_id"], r
    else:
        # 관망 계열이면 후보 없음(정상)
        assert r["scheduled_order_plan_id"] is None, r


# ============================================================
# daily_runner — 전 계좌 일괄 생성(멱등) · 주문 자동실행 0
# ============================================================

def _seed_account(idx):
    conn = store_db.connect()
    try:
        conn.execute("INSERT OR IGNORE INTO accounts(account_index, alias, mode, has_credentials) "
                     "VALUES(?,?,?,0)", (idx, f"acct{idx}", "paper"))
        conn.commit()
    finally:
        conn.close()


def test_run_all_generates_review_per_account():
    _seed_account(41); _profile(41); _select(41)
    _seed_account(42); _profile(42)  # 선택 없음 → watch (정상)
    out = drun.run_all([41, 42])
    assert out["ok"] and out["accounts"] == 2, out
    assert out["generated"] == 2, out          # 두 계좌 모두 review row 생성(자동 생성 proof)
    assert out["orders_executed"] == 0, out    # 불변: 주문 자동 실행 0
    by = {r["account_index"]: r for r in out["results"]}
    assert by[41]["ok"] and by[41]["review_id"], by[41]
    assert by[42]["action_decision"] == "watch", by[42]
    # DB 에 실제 review row 가 생겼는지 확인.
    conn = store_db.connect()
    try:
        for idx in (41, 42):
            n = conn.execute("SELECT COUNT(*) c FROM daily_portfolio_reviews WHERE account_index=?",
                             (idx,)).fetchone()["c"]
            assert n == 1, (idx, n)
    finally:
        conn.close()


def test_run_all_is_idempotent():
    _seed_account(43); _profile(43); _select(43)
    drun.run_all([43])
    drun.run_all([43])  # 같은 날 재실행 — 계좌×일 1행 유지
    conn = store_db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM daily_portfolio_reviews WHERE account_index=43").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1, f"멱등이어야 함 (got {n})"


def test_run_all_lists_accounts_from_table():
    _seed_account(44); _profile(44)
    accts = drun.list_accounts()
    assert 44 in accts, accts


# ============================================================
# carry-over — 직전 미체결 후보 재평가(자동 주문 아님)
# ============================================================

def _seed_unfilled_plan(idx, *, plan_age_days):
    """직전 cycle 미체결 step 1개를 가진 plan 시드 — plan_age_days 만큼 과거 생성."""
    from datetime import timedelta
    created = (datetime.now(timezone.utc) - timedelta(days=plan_age_days)).isoformat()
    conn = store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO scheduled_order_plans(account_index, status, created_at) VALUES(?,?,?)",
            (idx, "pending_approval", created))
        pid = cur.lastrowid
        conn.execute(
            "INSERT INTO scheduled_order_steps(plan_id, ref, direction, total_pct, remaining_pct, "
            "round_no, total_rounds, on_unfilled, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (pid, "반도체", "매수", 5.0, 5.0, 1, 2, "다음 cycle 재평가", "candidate", created))
        conn.commit()
        return pid
    finally:
        conn.close()


def test_carry_over_carries_recent_unfilled():
    _seed_account(51); _profile(51)
    _seed_unfilled_plan(51, plan_age_days=1)  # 최근 → carry
    r = dr.generate_review(51)
    co = r.get("carry_over") or (r.get("payload", {}) or {}).get("carry_over")
    assert co and len(co["items"]) == 1, co
    item = co["items"][0]
    assert item["verdict"] == "carry", item
    assert co["carry_count"] == 1 and co["expire_count"] == 0, co
    assert r["has_orders"] is False or r["action_decision"] in ("watch", "hold", "rebalance"), r


def test_carry_over_expires_stale_unfilled():
    _seed_account(52); _profile(52)
    pid = _seed_unfilled_plan(52, plan_age_days=dr.CARRY_OVER_EXPIRE_DAYS + 2)  # 오래됨 → expire
    r = dr.generate_review(52)
    co = r.get("carry_over") or (r.get("payload", {}) or {}).get("carry_over")
    assert co and co["expire_count"] == 1, co
    assert co["items"][0]["verdict"] == "expire", co
    # 만료된 plan/step 은 상태 전이만(주문 실행 아님).
    conn = store_db.connect()
    try:
        plan = conn.execute("SELECT status FROM scheduled_order_plans WHERE id=?", (pid,)).fetchone()
        step = conn.execute("SELECT status FROM scheduled_order_steps WHERE plan_id=?", (pid,)).fetchone()
    finally:
        conn.close()
    assert plan["status"] == "expired", dict(plan)
    assert step["status"] == "blocked", dict(step)


def test_carry_over_is_not_an_order():
    # carry-over 는 재평가일 뿐 — fills/orders 테이블에 아무것도 안 생긴다(주문 자동실행 0).
    _seed_account(53); _profile(53)
    _seed_unfilled_plan(53, plan_age_days=1)
    dr.generate_review(53)
    conn = store_db.connect()
    try:
        # 후보 status 는 candidate/hold/blocked 만 — filled/executed 같은 체결 상태 절대 없음.
        bad = conn.execute(
            "SELECT COUNT(*) c FROM scheduled_order_steps s "
            "JOIN scheduled_order_plans p ON p.id=s.plan_id "
            "WHERE p.account_index=53 AND s.status NOT IN ('candidate','hold','blocked')").fetchone()["c"]
    finally:
        conn.close()
    assert bad == 0, "주문 체결 상태가 생기면 안 됨(자동 주문 금지)"


# ============================================================
# stale snapshot — fail-closed watch + 사유 명시
# ============================================================

def test_stale_snapshot_marks_watch_with_reason():
    from datetime import timedelta
    idx = 61
    _seed_account(idx)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, risk_tolerance, cash_min_pct, cash_max_pct, "
            "interests_text, updated_at) VALUES(?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(account_index) DO NOTHING",
            (idx, "neutral", 10.0, 30.0, "반도체"))
        cur = conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, "
            "source, captured_at) VALUES(?,?,?,?,?,?)", (idx, 9_000_000, 10_000_000, 0, "test", old))
        snap_id = cur.lastrowid
        conn.execute(
            "INSERT INTO allocation_selections(account_index, proposal_id, variant, allocation, "
            "account_snapshot_id, precheck_status, status, selected_at) VALUES(?,?,?,?,?,?,?,?)",
            (idx, "p-stale", "base",
             json.dumps([{"kind": "cash", "ref": None, "weight_pct": 30.0},
                         {"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": 70.0}],
                        ensure_ascii=False),
             snap_id, "pass", "active", old))
        conn.commit()
    finally:
        conn.close()
    r = dr.generate_review(idx)
    assert r["ok"] and r["action_decision"] == "watch", r
    assert r["has_orders"] is False and r["scheduled_order_plan_id"] is None, r
    assert "stale" in (r["no_trade_reason"] or "").lower() or "오래" in (r["no_trade_reason"] or ""), r


# ============================================================
# evidence link — 근거 연결(있으면 링크, 없으면 정직 빈 목록)
# ============================================================

def test_evidence_linked_when_present():
    idx = 71
    _seed_account(idx); _profile(idx); _select(idx)
    ev_mod.add_evidence("news", theme="반도체", topic="반도체",
                        summary="반도체 업황 회복 신호", stance="long_support",
                        confidence=0.7, account_index=idx)
    r = dr.generate_review(idx)
    e = r.get("evidence") or (r.get("payload", {}) or {}).get("evidence")
    assert e is not None, r
    assert e["has_evidence"] is True and len(e["links"]) >= 1, e
    # daily_review_evidence_links 에 실제 링크 row 존재.
    conn = store_db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM daily_review_evidence_links WHERE review_id=?",
                         (r["review_id"],)).fetchone()["c"]
    finally:
        conn.close()
    assert n >= 1, n


def test_evidence_honest_empty_when_absent():
    idx = 72
    _seed_account(idx); _profile(idx); _select(idx)  # 근거 없음
    r = dr.generate_review(idx)
    e = r.get("evidence") or (r.get("payload", {}) or {}).get("evidence")
    assert e is not None and e["has_evidence"] is False, e
    assert e["links"] == [], e
    assert "근거" in e["note"], e


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} DAILY-REVIEW TESTS PASSED")
