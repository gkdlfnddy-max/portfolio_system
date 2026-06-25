"""하락 징후 분석 기록 영속화 (성장 루프) — decline_analyses 테이블.

역할:
  1. record_analysis — scan/composite 결과를 **예측 시점**(analysis_date)으로 저장.
     그 시점까지의 데이터만으로 산출된 점수다(lookahead 차단은 호출측 + evaluate 가 보장).
  2. set_user_action — 사용자 반응(ignored/accepted/modified/saved_to_policy/rejected_as_wrong) 갱신.
  3. evaluate_outcome — analysis_date **이후** future_return_window 거래일 일봉만으로
     실제 낙폭(actual_drawdown) 계산 → hit/miss → track_record 로 reliability 갱신 →
     reliability_before/after 기록. **분석일 이후 데이터만 사용(미래 누설 금지).**
  4. evaluate_pending — 결과 평가 가능한(window 경과) 분석을 일괄 평가.

원칙(불변):
  - Anthropic API 미사용. 자동주문 0(분석/결과 기록만).
  - lookahead bias 차단: evaluate 는 trade_date > analysis_date 인 일봉만 사용.
  - scope 격리: account_index 는 "조회 계좌" 기록일 뿐, reliability 성장은 axis/instrument/
    sector 시장 공통 노하우로 누적(계좌 교차적용 금지).
  - 실현 결과 적으면 reliability 중립(0.5) 유지 — 정직(가짜 성장 보고 금지).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .. import price_history as ph
from ..store import db as store_db
from . import track_record as tr

# 하락 판정 임계 — analysis_date 이후 window 내 최대 낙폭이 이 % 이상이면 "실제 하락"(hit).
DEFAULT_DECLINE_THRESHOLD_PCT = 7.0
DEFAULT_FUTURE_WINDOW = 10  # 거래일

_USER_ACTIONS = {"ignored", "accepted", "modified", "saved_to_policy", "rejected_as_wrong"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# 1. 분석 기록 저장 (예측 시점)
# ============================================================
def record_analysis(code: str, scan_result: dict, *, analysis_date: str,
                    account_index: int | None = None, sector: str | None = None) -> dict:
    """scan_instrument 결과(composite 포함)를 decline_analyses 에 저장(예측 시점 기록).

    scan_result: decline_scan.scan_instrument(...) 의 반환(ok=True, composite 포함 가정).
    analysis_date: 'YYYY-MM-DD' — 이 시점까지 데이터로 산출됨(lookahead 차단 기준).
    저장 시 hit_or_miss='pending'(미평가). 결과는 evaluate_outcome 가 사후 채운다.
    """
    comp = scan_result.get("composite") or {}
    meta = comp.get("metacognition") or {}
    available = [n for n, r in (comp.get("axes") or {}).items() if r.get("data_available")]
    missing = meta.get("data_missing_axes") or []
    axis_scores = {
        n: {"risk_0_100": r.get("risk_0_100"), "confidence": r.get("confidence"),
            "reliability": r.get("reliability"), "weight": r.get("weight")}
        for n, r in (comp.get("axes") or {}).items() if r.get("data_available")
    }
    overall_risk = comp.get("holistic_risk", scan_result.get("risk_score"))
    overall_conf = comp.get("overall_confidence")
    # 발화 여부(예측) — holistic risk 가 elevated 이상이면 보수적 전환 후보
    sector_v = sector if sector is not None else scan_result.get("sector")

    conn = store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO decline_analyses(account_index, code, sector, analysis_date, "
            "available_axes, missing_axes, axis_scores, overall_risk, overall_confidence, "
            "suggested_action, policy_draft_created, hit_or_miss, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_index, code, sector_v, analysis_date,
             json.dumps(available, ensure_ascii=False),
             json.dumps(missing, ensure_ascii=False),
             json.dumps(axis_scores, ensure_ascii=False),
             overall_risk, overall_conf,
             scan_result.get("suggested_action"), 0, "pending", _now()),
        )
        conn.commit()
        return {"ok": True, "analysis_id": cur.lastrowid, "code": code,
                "analysis_date": analysis_date, "overall_risk": overall_risk,
                "available_axes": available, "missing_axes": missing}
    finally:
        conn.close()


# ============================================================
# 2. 사용자 반응 갱신
# ============================================================
def set_user_action(analysis_id: int, user_action: str, *,
                    policy_draft_created: bool | None = None) -> dict:
    if user_action not in _USER_ACTIONS:
        return {"ok": False, "error": f"unknown user_action: {user_action}",
                "allowed": sorted(_USER_ACTIONS)}
    conn = store_db.connect()
    try:
        row = conn.execute("SELECT analysis_id FROM decline_analyses WHERE analysis_id=?",
                           (analysis_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "analysis_not_found", "analysis_id": analysis_id}
        if policy_draft_created is None:
            conn.execute("UPDATE decline_analyses SET user_action=? WHERE analysis_id=?",
                         (user_action, analysis_id))
        else:
            conn.execute(
                "UPDATE decline_analyses SET user_action=?, policy_draft_created=? WHERE analysis_id=?",
                (user_action, 1 if policy_draft_created else 0, analysis_id))
        conn.commit()
        return {"ok": True, "analysis_id": analysis_id, "user_action": user_action}
    finally:
        conn.close()


# ============================================================
# 3. 결과 평가 → 성장 (lookahead 차단)
# ============================================================
def _future_drawdown(history: list[dict], analysis_date: str, window: int) -> dict | None:
    """analysis_date **이후** window 거래일 일봉만으로 최대 낙폭(%) 계산.

    lookahead 차단 핵심: trade_date(date) > analysis_date 인 바만 사용.
    기준가 = analysis_date 종가(있으면) 또는 미래 첫 바 종가. 낙폭 = (min(close)/기준 - 1)*100.
    경과분이 없으면(아직 미래 데이터 없음) None 반환 → 평가 보류(정직).
    """
    after = [b for b in history if str(b.get("date")) > analysis_date]
    if not after:
        return None
    window_bars = after[:window]
    if not window_bars:
        return None
    # 기준가: 분석일 종가(분석 시점에 알 수 있는 값) — 없으면 미래 첫 바로 폴백.
    base_rows = [b for b in history if str(b.get("date")) == analysis_date]
    base = base_rows[-1]["close"] if base_rows else window_bars[0]["close"]
    if not base:
        return None
    closes = [b["close"] for b in window_bars if b.get("close") is not None]
    if not closes:
        return None
    trough = min(closes)
    dd = (trough / base - 1.0) * 100.0
    return {"actual_drawdown": round(dd, 2), "bars_used": len(window_bars),
            "base_close": base, "trough_close": trough,
            "window_from": window_bars[0]["date"], "window_to": window_bars[-1]["date"]}


def evaluate_outcome(analysis_id: int, *, history: list[dict] | None = None,
                     future_window: int = DEFAULT_FUTURE_WINDOW,
                     decline_threshold_pct: float = DEFAULT_DECLINE_THRESHOLD_PCT) -> dict:
    """단일 분석 결과 평가(lookahead 차단) → hit/miss → reliability 갱신 + before/after 기록.

    history 미지정 시 DB(price_history)에서 로드. **분석일 이후 일봉만** 낙폭 계산에 사용.
    예측(발화)이 아니면(overall_risk 가 임계 미만) no_prediction — track record 미기록.
    """
    conn = store_db.connect()
    try:
        a = conn.execute("SELECT * FROM decline_analyses WHERE analysis_id=?",
                         (analysis_id,)).fetchone()
    finally:
        conn.close()
    if not a:
        return {"ok": False, "error": "analysis_not_found", "analysis_id": analysis_id}

    code = a["code"]
    analysis_date = a["analysis_date"]
    hist = history if history is not None else ph.load_history(code)
    fut = _future_drawdown(hist, analysis_date, future_window)
    if fut is None:
        return {"ok": False, "analysis_id": analysis_id, "reason": "no_future_data_yet",
                "note": "분석일 이후 일봉 미적재 — 결과 평가 보류(정직). lookahead 방지."}

    # 예측 발화 여부: overall_risk 가 elevated(>=15) 이상이면 "하락 예측" 으로 간주.
    overall_risk = a["overall_risk"] or 0.0
    predicted = overall_risk >= 15.0
    actual_decline = fut["actual_drawdown"] <= -decline_threshold_pct

    # 결과평가 직전/직후 대표 reliability — 종목(instrument) 기준.
    rel_before = tr.reliability_scoped("instrument", code)["reliability"]

    outcome = "no_prediction"
    axis_results = []
    if predicted:
        outcome = "hit" if actual_decline else "miss"
        # 종목 + 섹터 + 발화 축별로 reliability 갱신(시장 공통 노하우, 계좌 교차적용 아님).
        tr.record_outcome_scoped("instrument", code, predicted_decline=True,
                                 actual_decline=actual_decline,
                                 ref_note=f"{analysis_date} 분석 이후 {fut['bars_used']}거래일 낙폭 {fut['actual_drawdown']}%")
        if a["sector"]:
            tr.record_outcome_scoped("sector", a["sector"], predicted_decline=True,
                                     actual_decline=actual_decline)
        for axis in json.loads(a["available_axes"] or "[]"):
            r = tr.record_outcome(axis, predicted_decline=True,
                                  actual_decline=actual_decline)
            axis_results.append({"axis": axis, "outcome": r.get("outcome")})

    rel_after = tr.reliability_scoped("instrument", code)["reliability"]

    conn = store_db.connect()
    try:
        conn.execute(
            "UPDATE decline_analyses SET actual_drawdown=?, hit_or_miss=?, future_return_window=?, "
            "reliability_before=?, reliability_after=?, evaluated_at=? WHERE analysis_id=?",
            (fut["actual_drawdown"], outcome, fut["bars_used"], rel_before, rel_after,
             _now(), analysis_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "analysis_id": analysis_id, "code": code,
            "analysis_date": analysis_date, "predicted_decline": predicted,
            "actual_drawdown": fut["actual_drawdown"], "actual_decline": actual_decline,
            "hit_or_miss": outcome, "future_window": fut["bars_used"],
            "lookahead_guard": {"only_after": analysis_date,
                                "window_from": fut["window_from"], "window_to": fut["window_to"]},
            "reliability_before": rel_before, "reliability_after": rel_after,
            "axis_outcomes": axis_results}


def evaluate_pending(*, future_window: int = DEFAULT_FUTURE_WINDOW,
                     decline_threshold_pct: float = DEFAULT_DECLINE_THRESHOLD_PCT,
                     limit: int = 200) -> dict:
    """평가 가능한(미평가 + 미래 데이터 충분) 분석을 일괄 평가."""
    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT analysis_id FROM decline_analyses WHERE hit_or_miss='pending' "
            "ORDER BY analysis_id LIMIT ?", (limit,)).fetchall()
        ids = [r["analysis_id"] for r in rows]
    finally:
        conn.close()
    evaluated, deferred = [], []
    for aid in ids:
        res = evaluate_outcome(aid, future_window=future_window,
                               decline_threshold_pct=decline_threshold_pct)
        if res.get("ok"):
            evaluated.append(res)
        else:
            deferred.append({"analysis_id": aid, "reason": res.get("reason", res.get("error"))})
    return {"ok": True, "evaluated_count": len(evaluated), "deferred_count": len(deferred),
            "evaluated": evaluated, "deferred": deferred}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evaluate-pending", action="store_true")
    ap.add_argument("--evaluate", type=int, metavar="ANALYSIS_ID")
    ap.add_argument("--window", type=int, default=DEFAULT_FUTURE_WINDOW)
    args = ap.parse_args()
    if args.evaluate is not None:
        out = evaluate_outcome(args.evaluate, future_window=args.window)
    elif args.evaluate_pending:
        out = evaluate_pending(future_window=args.window)
    else:
        out = {"ok": False, "error": "--evaluate-pending | --evaluate ID [--window N]"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
