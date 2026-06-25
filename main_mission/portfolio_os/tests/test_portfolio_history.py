"""일별 포트폴리오 추이 테스트 — 멱등(계좌×일), 정렬, 계좌 격리, 정직(no-holdings), no-mock."""
from __future__ import annotations

import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_history.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import portfolio_history as ph


def setup():
    # env(SQLITE_PATH)는 setup()에서도 핀 — 다른 테스트 모듈 import 순서로 DB 가 가로채이지 않게.
    os.environ["SQLITE_PATH"] = _TMP
    store_db.init()


def _src_snapshot(idx, cash, total, holdings=None, captured="2026-06-19 09:00:00"):
    """운영 스냅샷(record 의 출처) 1개 삽입. holdings=[(ticker,name,qty,mv), ...]."""
    conn = store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, "
            "source, is_stale, captured_at) VALUES(?,?,?,?,?,0,?)",
            (idx, cash, total, len(holdings or []), "kis_paper", captured),
        )
        sid = cur.lastrowid
        for (t, n, q, mv) in (holdings or []):
            conn.execute(
                "INSERT INTO holdings(snapshot_id, account_index, ticker, name, qty, market_value, currency, captured_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (sid, idx, t, n, q, mv, "KRW", captured),
            )
        conn.commit()
        return sid
    finally:
        conn.close()


# ---- record 출처 없으면 정직하게 실패(가짜 기록 금지) ----
def test_record_requires_source_snapshot():
    r = ph.record_daily(701)
    assert r["ok"] is False and "동기화" in r["error"], r


# ---- 보유종목 없을 때: 현금/총자산만 + holdings_tracked=false (no mock) ----
def test_honest_no_holdings():
    _src_snapshot(702, cash=9_000_000, total=10_000_000, holdings=[])
    r = ph.record_daily(702, record_date="2026-06-20")
    assert r["ok"] and r["holdings_tracked"] is False, r
    assert r["holdings"] == [], r
    assert r["total_value_krw"] == 10_000_000 and r["cash_krw"] == 9_000_000, r
    assert "동기화 전" in (r["note"] or ""), r
    # bucket: 현금 90%, 채권 0, 위험 0 — 가짜 위험비중 만들지 않음.
    assert r["buckets"]["순현금"] == 90.0 and r["buckets"]["위험"] == 0.0, r


# ---- 멱등: 같은 계좌·같은 날 두 번 record → history 행 1개 ----
def test_record_idempotent_per_day():
    _src_snapshot(703, cash=5_000_000, total=10_000_000, holdings=[("005930", "삼성전자", 10, 5_000_000)])
    ph.record_daily(703, record_date="2026-06-20")
    ph.record_daily(703, record_date="2026-06-20")  # 재실행
    conn = store_db.connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM account_snapshots WHERE account_index=? AND source=? "
            "AND substr(replace(captured_at,'T',' '),1,10)=?",
            (703, ph.HISTORY_SOURCE, "2026-06-20"),
        ).fetchone()["c"]
        assert n == 1, f"같은 날 history 행은 1개여야 함, got {n}"
        # 교체된 행의 holdings 도 중복되지 않아야 함(고아 holdings 누적 방지).
        h = conn.execute(
            "SELECT COUNT(*) c FROM holdings WHERE account_index=? AND ticker='005930'", (703,)
        ).fetchone()["c"]
        # 출처 1 + history 1 = 2 (재실행해도 history 쪽은 교체되어 늘지 않음)
        assert h == 2, f"holdings 중복 적재됨: {h}"
    finally:
        conn.close()


# ---- series: 날짜 오름차순 정렬 ----
def test_series_sorted_dates():
    # 서로 다른 날짜로(일부러 뒤섞어) 적재 → series 는 오름차순으로 돌려줘야 함.
    _src_snapshot(704, 9_000_000, 10_000_000, [], captured="2026-06-17 09:00:00")
    ph.record_daily(704, record_date="2026-06-17")
    _src_snapshot(704, 8_000_000, 10_500_000, [], captured="2026-06-19 09:00:00")
    ph.record_daily(704, record_date="2026-06-19")
    _src_snapshot(704, 7_500_000, 11_000_000, [], captured="2026-06-18 09:00:00")
    ph.record_daily(704, record_date="2026-06-18")

    s = ph.series(704, days=30)
    assert s["ok"], s
    assert s["dates"] == sorted(s["dates"]), s["dates"]
    assert len(s["total_value"]) == len(s["dates"]) == len(s["cash"]), s
    # bucket_series 길이도 일치
    assert all(len(v) == len(s["dates"]) for v in s["bucket_series"].values()), s


# ---- 계좌 격리: A 의 시계열 != B ----
def test_account_isolation():
    _src_snapshot(801, cash=9_000_000, total=10_000_000, holdings=[("005930", "삼성전자", 10, 1_000_000)])
    ph.record_daily(801, record_date="2026-06-20")
    _src_snapshot(802, cash=2_000_000, total=20_000_000, holdings=[("000660", "SK하이닉스", 5, 18_000_000)])
    ph.record_daily(802, record_date="2026-06-20")

    a = ph.series(801, days=30)
    b = ph.series(802, days=30)
    assert a["total_value"] != b["total_value"], (a["total_value"], b["total_value"])
    assert "005930" in a["holdings_by_symbol"] and "005930" not in b["holdings_by_symbol"], (a, b)
    assert "000660" in b["holdings_by_symbol"] and "000660" not in a["holdings_by_symbol"], (a, b)
    # B 의 종목이 A 의 시계열에 누출되지 않음.
    assert a["account_index"] == 801 and b["account_index"] == 802


# ---- 종목별 추이 + bucket 채권 분류 ----
def test_holdings_trend_and_bond_bucket():
    _src_snapshot(901, cash=1_000_000, total=10_000_000,
                  holdings=[("005930", "삼성전자", 10, 6_000_000),
                            ("국채10년", "KOSEF 국고채10년", 30, 3_000_000)])
    r = ph.record_daily(901, record_date="2026-06-20")
    assert r["ok"] and r["holdings_tracked"] is True, r
    syms = {h["symbol"]: h for h in r["holdings"]}
    assert syms["005930"]["weight_pct"] == 60.0, syms
    # 채권 휴리스틱: '국채' 포함 → 채권 bucket 으로
    assert r["buckets"]["채권"] == 30.0, r["buckets"]
    assert r["buckets"]["위험"] == 60.0 and r["buckets"]["순현금"] == 10.0, r["buckets"]

    s = ph.series(901, days=30)
    assert s["holdings_tracked"] is True
    assert s["holdings_by_symbol"]["005930"][0]["weight"] == 60.0, s


# ---- no-mock: series 빈 계좌는 빈 시계열 + 정직 플래그 ----
def test_empty_account_no_fake_numbers():
    s = ph.series(999, days=30)
    assert s["ok"] and s["dates"] == [] and s["total_value"] == [], s
    assert s["holdings_tracked"] is False and s["holdings_by_symbol"] == {}, s
    assert "동기화 전" in (s["note"] or ""), s
    # 노출/ drift 도 빈 시계열 + 정직 플래그(가짜 노출 없음)
    assert s["exposure_tracked"] is False and s["exposure"] is None, s
    assert s["drift_series"] == [] and all(v == [] for v in s["exposure_series"].values()), s


# ---------------------------------------------------------------------------
# 노출(exposure) — selected allocation 의 확정 안 rows 에서 직접 계산
# ---------------------------------------------------------------------------
def _select_alloc(idx, alloc):
    """allocation_selections 에 active 확정 안 1개 삽입. alloc=[{kind,ref,weight_pct}, ...]."""
    import json as _json
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO allocation_selections(account_index, variant, allocation, status, selected_by, selected_at) "
            "VALUES(?,?,?,?,?,?)",
            (idx, "base", _json.dumps(alloc), "active", "ceo", "2026-06-20 10:00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _review(idx, review_date, drift):
    """daily_portfolio_reviews 1행(drift_score) 삽입(계좌×일 UNIQUE)."""
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO daily_portfolio_reviews(account_index, review_date, drift_score, action_decision) "
            "VALUES(?,?,?,?)",
            (idx, review_date, drift, "hold"),
        )
        conn.commit()
    finally:
        conn.close()


# ---- 노출: net=long-short, gross=long+short, theme/hedge 정확 ----
def test_exposure_net_gross_from_selected_allocation():
    _select_alloc(1101, [
        {"kind": "cash", "ref": None, "weight_pct": 10},
        {"kind": "bond", "ref": "국채", "weight_pct": 10},   # 방어 — 노출 제외
        {"kind": "anchor", "ref": "광범위", "weight_pct": 40},
        {"kind": "tilt", "ref": "반도체", "weight_pct": 25},
        {"kind": "tilt", "ref": "2차전지", "weight_pct": 15},
        {"kind": "hedge", "ref": "인버스", "weight_pct": 10},
    ])
    e = ph._exposure_from_alloc(ph._active_allocation(store_db.connect(), 1101))
    assert e["long_pct"] == 80.0, e        # anchor40 + tilt25 + tilt15
    assert e["short_pct"] == 10.0, e       # hedge10
    assert e["net_pct"] == 70.0, e         # 80 - 10
    assert e["gross_pct"] == 90.0, e       # 80 + 10
    assert e["hedge_exposure_pct"] == 10.0, e
    assert e["theme_exposure"] == {"반도체": 25.0, "2차전지": 15.0}, e
    # 방어(cash+bond)는 노출에서 제외됨(long/short 에 안 들어감)


# ---- chart 숫자 = series 숫자: record_daily 와 series 노출/ drift 일치, 차트가 쓰는 시리즈와 동일 ----
def test_chart_numbers_equal_db_series():
    _src_snapshot(1102, cash=2_000_000, total=10_000_000,
                  holdings=[("005930", "삼성전자", 10, 6_000_000)], captured="2026-06-19 09:00:00")
    _select_alloc(1102, [
        {"kind": "cash", "ref": None, "weight_pct": 20},
        {"kind": "anchor", "ref": "광범위", "weight_pct": 60},
        {"kind": "tilt", "ref": "반도체", "weight_pct": 25},
        {"kind": "hedge", "ref": "인버스", "weight_pct": 5},
    ])
    _review(1102, "2026-06-19", drift=12.5)
    r = ph.record_daily(1102, record_date="2026-06-19")
    assert r["ok"], r
    # record 가 돌려주는 노출/ drift
    assert r["exposure"]["net_pct"] == 80.0 and r["exposure"]["gross_pct"] == 90.0, r["exposure"]
    assert r["exposure"]["hedge_exposure_pct"] == 5.0, r["exposure"]
    assert r["drift_score"] == 12.5, r

    s = ph.series(1102, days=30)
    # 차트가 그리는 시리즈의 마지막 점 == record 가 계산한 값(= DB 일치)
    assert s["exposure_series"]["net"][-1] == r["exposure"]["net_pct"], s
    assert s["exposure_series"]["gross"][-1] == r["exposure"]["gross_pct"], s
    assert s["exposure_series"]["hedge"][-1] == r["exposure"]["hedge_exposure_pct"], s
    assert s["exposure_series"]["theme"][-1] == 25.0, s  # tilt 합
    assert s["drift_series"][-1] == 12.5, s
    # 시리즈 길이 = dates 길이(차트 축 정합)
    assert all(len(v) == len(s["dates"]) for v in s["exposure_series"].values()), s
    assert len(s["drift_series"]) == len(s["dates"]), s
    assert s["exposure_tracked"] is True, s


# ---- drift carry-forward: 점검 없는 날은 그 이전 최근 drift, 아예 없으면 None(정직) ----
def test_drift_series_carry_forward_and_none():
    _src_snapshot(1103, 9_000_000, 10_000_000, [], captured="2026-06-17 09:00:00")
    ph.record_daily(1103, record_date="2026-06-17")
    _src_snapshot(1103, 9_000_000, 10_000_000, [], captured="2026-06-18 09:00:00")
    ph.record_daily(1103, record_date="2026-06-18")
    _src_snapshot(1103, 9_000_000, 10_000_000, [], captured="2026-06-19 09:00:00")
    ph.record_daily(1103, record_date="2026-06-19")
    _review(1103, "2026-06-18", drift=7.0)  # 18일에만 점검
    s = ph.series(1103, days=30)
    # 17일: 점검 이전 → None(가짜 0 금지) · 18,19일: 7.0(carry-forward)
    assert s["dates"] == ["2026-06-17", "2026-06-18", "2026-06-19"], s["dates"]
    assert s["drift_series"] == [None, 7.0, 7.0], s["drift_series"]


# ---- 노출 정직: 확정 안 없으면 exposure None + exposure_series 0 + 플래그 false ----
def test_exposure_honest_without_selection():
    _src_snapshot(1104, 9_000_000, 10_000_000, [], captured="2026-06-20 09:00:00")
    ph.record_daily(1104, record_date="2026-06-20")
    s = ph.series(1104, days=30)
    assert s["exposure_tracked"] is False and s["exposure"] is None, s
    assert s["exposure_series"]["net"] == [0.0], s  # 길이 정합용 0, 플래그로 정직 구분
    r = ph.record_daily(1104, record_date="2026-06-20")
    assert r["exposure"] is None, r


# ---------------------------------------------------------------------------
# advice_history — 적용/무시 분류 + 계좌 격리
# ---------------------------------------------------------------------------
def _advice_event(idx, action, field="interests", consult_id=None, detail=None):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO field_advice_events(account_index, field_consultation_id, field_name, user_action, detail) "
            "VALUES(?,?,?,?,?)",
            (idx, consult_id, field, action, detail),
        )
        conn.commit()
    finally:
        conn.close()


def _consultation(idx, field, agent, evidence_ids=None, lesson_ids=None, suggested="개선안"):
    conn = store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO field_consultations(account_index, field_name, agent_name, advice_type, "
            "suggested_text, evidence_ids, lesson_ids) VALUES(?,?,?,?,?,?,?)",
            (idx, field, agent, "improve", suggested, evidence_ids, lesson_ids),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ---- advice_history: applied/edited/saved=반영, ignored=무시 분류 ----
def test_advice_history_kept_vs_ignored():
    cid = _consultation(1201, "views", "opinion-field-advisor",
                        evidence_ids="3,7", lesson_ids="[12]")
    _advice_event(1201, "applied", field="views", consult_id=cid)
    _advice_event(1201, "ignored", field="interests")
    _advice_event(1201, "edited", field="region", consult_id=cid)
    _advice_event(1201, "saved", field="whole")

    h = ph.advice_history(1201)
    assert h["ok"] and h["point_count"] == 4, h
    assert h["counts"]["applied"] == 1 and h["counts"]["ignored"] == 1, h["counts"]
    assert h["counts"]["edited"] == 1 and h["counts"]["saved"] == 1, h["counts"]
    assert h["counts"]["kept_total"] == 3 and h["counts"]["ignored_total"] == 1, h["counts"]
    # 최신순 + kept 플래그 + evidence/lesson 카운트(증거/회귀 사용 이력)
    by_action = {e["user_action"]: e for e in h["events"]}
    assert by_action["applied"]["kept"] is True and by_action["ignored"]["kept"] is False, h
    assert by_action["applied"]["evidence_count"] == 2, by_action["applied"]
    assert by_action["applied"]["lesson_count"] == 1, by_action["applied"]
    assert by_action["applied"]["agent_name"] == "opinion-field-advisor", by_action["applied"]


# ---- advice_history: 계좌 격리 ----
def test_advice_history_account_isolation():
    _advice_event(1301, "applied", field="views")
    _advice_event(1302, "ignored", field="interests")
    a = ph.advice_history(1301)
    b = ph.advice_history(1302)
    assert a["counts"]["applied"] == 1 and a["counts"]["ignored"] == 0, a["counts"]
    assert b["counts"]["ignored"] == 1 and b["counts"]["applied"] == 0, b["counts"]
    # A 의 이벤트가 B 에 누출되지 않음
    assert all(e["account_index"] if "account_index" in e else True for e in a["events"])  # shape sanity
    assert a["point_count"] == 1 and b["point_count"] == 1, (a, b)


# ---- advice_history: 빈 계좌는 정직(빈 이력 + 안내) ----
def test_advice_history_empty_honest():
    h = ph.advice_history(1399)
    assert h["ok"] and h["events"] == [] and h["point_count"] == 0, h
    assert h["counts"]["kept_total"] == 0 and h["counts"]["ignored_total"] == 0, h
    assert "이력이 없습니다" in (h["note"] or ""), h
    # 정책 기록 없으면 policy_version_current=None (정직 — 가짜 버전 금지)
    assert h["policy_version_current"] is None, h


# ---------------------------------------------------------------------------
# 노출 시리즈: long/short 라인 추가 (gross/net 과 정합)
# ---------------------------------------------------------------------------
def test_exposure_series_includes_long_short():
    _src_snapshot(1150, 2_000_000, 10_000_000, [], captured="2026-06-19 09:00:00")
    _select_alloc(1150, [
        {"kind": "anchor", "ref": "광범위", "weight_pct": 50},
        {"kind": "tilt", "ref": "반도체", "weight_pct": 30},
        {"kind": "hedge", "ref": "인버스", "weight_pct": 10},
    ])
    ph.record_daily(1150, record_date="2026-06-19")
    s = ph.series(1150, days=30)
    assert "long" in s["exposure_series"] and "short" in s["exposure_series"], s["exposure_series"].keys()
    assert s["exposure_series"]["long"][-1] == 80.0, s   # anchor50 + tilt30
    assert s["exposure_series"]["short"][-1] == 10.0, s  # hedge10
    # net = long - short, gross = long + short (시리즈 정합)
    assert s["exposure_series"]["net"][-1] == 70.0, s
    assert s["exposure_series"]["gross"][-1] == 90.0, s
    # 모든 시리즈 길이 정합(차트 축)
    assert all(len(v) == len(s["dates"]) for v in s["exposure_series"].values()), s


# ---------------------------------------------------------------------------
# advice_history: policy version 반영 표시 (이벤트 시점의 활성 정책 버전)
# ---------------------------------------------------------------------------
def _policy(idx, version, created_at):
    import json as _json
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO portfolio_policies(account_index, version, policy, source, created_at) "
            "VALUES(?,?,?,?,?)",
            (idx, version, _json.dumps({"v": version}), "user", created_at),
        )
        conn.commit()
    finally:
        conn.close()


def _advice_event_at(idx, action, field, created_at, consult_id=None):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO field_advice_events(account_index, field_consultation_id, field_name, "
            "user_action, created_at) VALUES(?,?,?,?,?)",
            (idx, consult_id, field, action, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def test_advice_history_policy_version_at_event_time():
    _policy(1250, 1, "2026-06-10 09:00:00")
    _policy(1250, 2, "2026-06-18 09:00:00")
    # 이벤트가 v2 컴파일 후 → policy_version=2 / v1~v2 사이 → 1
    _advice_event_at(1250, "applied", "views", "2026-06-12 10:00:00")  # v1 활성
    _advice_event_at(1250, "edited", "region", "2026-06-19 10:00:00")  # v2 활성
    h = ph.advice_history(1250)
    by_field = {e["field_name"]: e for e in h["events"]}
    assert by_field["views"]["policy_version"] == 1, by_field["views"]
    assert by_field["region"]["policy_version"] == 2, by_field["region"]
    assert h["policy_version_current"] == 2, h


# ---------------------------------------------------------------------------
# growth_history — evidence / lesson candidate / promoted lesson(익명) / regression
# ---------------------------------------------------------------------------
def _lesson_candidate(idx, scope, ref, title, observed=2, conf=0.4):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO lesson_candidates(account_index, scope, ref, title, body, observed_count, "
            "confidence, status) VALUES(?,?,?,?,?,?,?,?)",
            (idx, scope, ref, title, "관찰 본문", observed, conf, "candidate"),
        )
        conn.commit()
    finally:
        conn.close()


def _agent_mem(scope_type, title, body, *, account_index=None, promoted=0, agent="theme-field-advisor",
               theme=None, conf=0.6):
    conn = store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO agent_memories(scope_type, agent_name, account_index, theme, title, body, "
            "confidence, promoted, archived, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,0,datetime('now'),datetime('now'))",
            (scope_type, agent, account_index, theme, title, body, conf, promoted),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _regression(task_type, title, expect):
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO task_regression_tests(task_type, title, expect, status) "
            "VALUES(?,?,?,?)",
            (task_type, title, expect, "active"),
        )
        conn.commit()
    finally:
        conn.close()


def test_growth_history_layers():
    from main_mission.portfolio_os import evidence as ev_mod
    # 1) evidence(이 계좌) — stance/freshness/confidence
    ev_mod.add_evidence("news", theme="반도체", topic="HBM", summary="HBM 수요 강세",
                        stance="long_support", confidence=0.8, account_index=1401)
    # 2) lesson 후보(이 계좌)
    _lesson_candidate(1401, "sector", "반도체", "반도체 사이클 관찰")
    # 3) promoted lesson(공통, 익명화됨 = account_index NULL)
    _agent_mem("agent", "테마 방향 원칙", "반도체 hedge 시 인버스 우선", promoted=1, account_index=None)
    # 4) regression 승격
    _regression("theme_advice", "반도체→방향분류", "반도체→short_or_hedge")

    g = ph.growth_history(1401)
    assert g["ok"], g
    assert g["counts"]["evidence"] >= 1 and g["counts"]["lesson_candidates"] >= 1, g["counts"]
    assert g["counts"]["promoted_lessons"] >= 1 and g["counts"]["regression"] >= 1, g["counts"]
    e0 = g["evidence"][0]
    assert e0["stance"] == "long_support" and e0["stance_label"] == "롱 근거", e0
    assert e0["eff_confidence"] is not None and e0["base_confidence"] == 0.8, e0
    assert g["anonymized"] is True, g


def test_growth_history_evidence_account_isolation():
    from main_mission.portfolio_os import evidence as ev_mod
    ev_mod.add_evidence("news", theme="2차전지", topic="A", summary="A전용",
                        stance="watch_only", confidence=0.5, account_index=1501)
    ev_mod.add_evidence("news", theme="2차전지", topic="B", summary="B전용",
                        stance="watch_only", confidence=0.5, account_index=1502)
    a = ph.growth_history(1501)
    b = ph.growth_history(1502)
    a_topics = {e["topic"] for e in a["evidence"]}
    b_topics = {e["topic"] for e in b["evidence"]}
    # A 의 근거가 B 로 누출되지 않음(계좌 격리)
    assert "A" in a_topics and "A" not in b_topics, (a_topics, b_topics)
    assert "B" in b_topics and "B" not in a_topics, (a_topics, b_topics)


def test_growth_history_promoted_only_anonymized():
    from main_mission.portfolio_os.growth.memory import has_identifiers
    # account-scoped 원본(식별정보 포함) — 노출되면 안 됨.
    _agent_mem("account", "민감 메모", "user_a의 1번 계좌 삼성전자 500주 보유 12345678 계좌",
               account_index=1601, promoted=0)
    # 승격본(익명화, account_index NULL) — 노출 대상.
    _agent_mem("agent", "공통 원칙", "반도체 hedge 시 인버스 우선(일반 원칙)",
               account_index=None, promoted=1)
    g = ph.growth_history(1601)
    # account-scoped 원본은 promoted_lessons 에 절대 포함 안 됨.
    titles = [p["title"] for p in g["promoted_lessons"]]
    assert "민감 메모" not in titles, titles
    # 노출된 promoted lesson 텍스트엔 식별정보가 없어야 함(익명화 보장).
    for p in g["promoted_lessons"]:
        assert not has_identifiers(p["title"]), p
        assert not has_identifiers(p["body"]), p


def test_growth_history_empty_honest():
    # 깨끗한 임시 DB(다른 테스트 누적 격리) — 빈 계좌의 정직 응답 검증.
    # db_path() 는 호출시점 os.environ 을 읽으므로 SQLITE_PATH 만 바꾸면 즉시 반영(reload 불필요).
    import os as _os, tempfile as _tf
    clean = _os.path.join(_tf.gettempdir(), "portfolio_test_history_empty.sqlite3")
    if _os.path.exists(clean):
        _os.remove(clean)
    prev = _os.environ["SQLITE_PATH"]
    _os.environ["SQLITE_PATH"] = clean
    try:
        store_db.init()
        g = ph.growth_history(1699)
        assert g["ok"] and g["point_count"] == 0, g
        assert g["evidence"] == [] and g["lesson_candidates"] == [], g
        assert g["promoted_lessons"] == [] and g["regression_promotions"] == [], g
        assert "성장 이력이 없습니다" in (g["note"] or ""), g
    finally:
        _os.environ["SQLITE_PATH"] = prev
