"""lesson outcome — 분석 이후 시장 반응을 **자동 기록**해 lesson_runs reliability 를 성장시킨다.

성장 루프의 마지막 고리(자동화):
  record_lesson(...)  → 판단 시점 기록(hit_or_miss=pending)         [lesson_runs.py]
  evaluate_pending()  → **분석일 이후** N거래일 시장반응을 price_history 에서 계산해
                        record_outcome 으로 hit/miss/false_alarm 자동 판정 + reliability 갱신
  reliability(...)    → 다음 prehook 우선순위                        [lesson_runs.py]

예시(CEO 지시):
  분석일 2026-06-22, 005930, 신호=외국인/기관 매도+개인 매수, 제안=진입속도 조절
  → 5/20/60거래일 후 수익률·최대낙폭 자동계산 → hit/miss/false_alarm → reliability 갱신.

────────────────────────────────────────────────────────────────────────
불변 안전 규칙 (이 모듈은 **평가·기록만** 한다):
  • lookahead bias 차단: 결과 평가는 **분석일(created_at) 이후 거래일(trade_date > analysis_date)**
    의 price_history 일봉만 사용한다. 분석일 당일/이전 일봉은 절대 사용 금지.
    (당일을 baseline 으로도 쓰지 않는다 — 분석 시점 이후 시장이 어떻게 움직였나만 본다.)
  • 자동주문 0 / policy 변경 0 — 시장 반응 기록과 reliability 갱신만.
  • 계좌 격리: lesson_run 의 account_index 를 바꾸지 않는다(시장 노하우는 scope 단위 누적).
  • secret 0 / Anthropic API import 0 — 순수 수치 계산.
  • 미래 일봉이 부족하면 hit 으로 추정하지 않고 **pending 유지**(정직 — 가짜 성장 금지).

reliability 갱신은 lesson_runs.record_outcome(베이지안)에 위임한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import lesson_runs
from . import price_history
from .store import db as store_db

DEFAULT_WINDOWS = [5, 20, 60]

# 시장 반응으로 종목성 lesson 을 평가할 수 있는 scope_type 집합.
# (account/agent/task/user_view 등은 시장 일봉으로 채점할 수 없으므로 제외.)
PRICE_EVALUABLE_SCOPES = ("stock", "etf")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _analysis_date(created_at: str | None) -> str | None:
    """lesson_run.created_at(ISO/‘YYYY-MM-DD …’) → 'YYYY-MM-DD' (분석일).

    이후 일봉 trade_date('YYYY-MM-DD') 와 문자열 비교(둘 다 ISO 날짜 → 사전식=시간식).
    """
    if not created_at:
        return None
    s = str(created_at).strip()
    # ISO 'YYYY-MM-DDTHH..' 또는 'YYYY-MM-DD HH..' 또는 'YYYY-MM-DD'
    for sep in ("T", " "):
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    s = s[:10]
    # 형식 방어
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None
    return s


def future_bars(instrument_code: str, analysis_date: str,
                *, history: list[dict] | None = None) -> list[dict]:
    """**분석일 이후**(trade_date > analysis_date) 일봉만 오래된→최신 순으로 반환.

    lookahead 차단의 단일 관문: 여기를 통과한 일봉만 결과 평가에 쓰인다.
    history 주입 가능(테스트 결정론). 미주입 시 price_history.load_history 사용.
    """
    hist = history if history is not None else price_history.load_history(instrument_code)
    out = []
    for b in hist:
        d = b.get("date") or b.get("trade_date")
        c = b.get("close")
        if not d or c is None:
            continue
        if str(d) > analysis_date:  # 엄격히 '이후' — 당일/이전 제외(lookahead 차단)
            out.append({"date": str(d), "high": b.get("high"), "low": b.get("low"),
                        "close": float(c)})
    out.sort(key=lambda b: b["date"])
    return out


def window_metrics(future: list[dict], window: int,
                   *, baseline_close: float) -> dict | None:
    """분석일 이후 첫 거래일을 기준(baseline)으로 window 거래일 후 수익률/최대낙폭.

    baseline_close: 분석일 이후 **첫** 거래일 종가(=분석 직후 진입 기준가).
    return_pct: (window번째 종가 / baseline - 1) * 100
    drawdown_pct: baseline 대비 그 구간 최저가의 최대 낙폭(%, ≤0). low 없으면 close 사용.
    미래 일봉이 window 개 미만이면 None(pending 유지 — lookahead/정직).
    """
    if window <= 0 or baseline_close in (None, 0):
        return None
    if len(future) < window:
        return None  # 미래 데이터 부족 → 정직하게 평가 보류
    seg = future[:window]
    end_close = seg[-1]["close"]
    ret = (end_close / baseline_close - 1.0) * 100.0
    lows = [(b["low"] if b.get("low") is not None else b["close"]) for b in seg]
    min_low = min(lows)
    dd = (min_low / baseline_close - 1.0) * 100.0
    if dd > 0:
        dd = 0.0
    return {"return_pct": round(ret, 4), "drawdown_pct": round(dd, 4),
            "baseline_close": round(float(baseline_close), 4),
            "end_close": round(float(end_close), 4), "bars_used": window}


def evaluate_lesson(lesson_id: int, *, windows: list[int] | None = None,
                    history: list[dict] | None = None, conn=None) -> dict:
    """단일 lesson_run 평가 → 가능한 가장 긴 window 로 record_outcome(자동 판정).

    각 window 의 수익률/낙폭을 actual_outcome 에 모두 담아 시장반응을 보존하고,
    판정(hit/miss/false_alarm)은 **확정 가능한 가장 긴 window** 의 수익률/낙폭으로 한다.
    어떤 window 도 확정 불가(미래 일봉 부족) → pending 유지(기록 안 함, 정직).
    """
    windows = sorted(windows or DEFAULT_WINDOWS)
    own = conn is None
    conn = conn or store_db.connect()
    try:
        row = conn.execute("SELECT * FROM lesson_runs WHERE id=?", (int(lesson_id),)).fetchone()
        if row is None:
            raise ValueError(f"lesson_run {lesson_id} 없음")
        r = dict(row)
        if r["scope_type"] not in PRICE_EVALUABLE_SCOPES:
            return {"ok": False, "lesson_id": int(lesson_id), "status": "skipped",
                    "reason": "non_price_scope", "scope_type": r["scope_type"]}
        if r["hit_or_miss"] != "pending":
            return {"ok": False, "lesson_id": int(lesson_id), "status": "skipped",
                    "reason": "already_evaluated", "hit_or_miss": r["hit_or_miss"]}

        analysis_date = _analysis_date(r["created_at"])
        if not analysis_date:
            return {"ok": False, "lesson_id": int(lesson_id), "status": "skipped",
                    "reason": "no_analysis_date"}

        fut = future_bars(r["scope_key"], analysis_date, history=history)
        if not fut:
            return {"ok": False, "lesson_id": int(lesson_id), "status": "pending",
                    "reason": "no_future_bars",
                    "note": "분석일 이후 일봉 없음 — 평가 보류(정직)."}

        baseline = fut[0]["close"]  # 분석일 이후 첫 거래일 = 진입 기준가
        per_window: dict[str, dict] = {}
        decided_window = None
        decided_metrics = None
        for w in windows:
            m = window_metrics(fut, w, baseline_close=baseline)
            if m is None:
                continue
            per_window[f"{w}d"] = m
            decided_window, decided_metrics = w, m  # windows 정렬됨 → 가장 긴 확정값 유지

        if decided_metrics is None:
            return {"ok": False, "lesson_id": int(lesson_id), "status": "pending",
                    "reason": "insufficient_future_bars",
                    "future_bars": len(fut), "min_window": windows[0],
                    "note": "확정 가능한 window 없음 — pending 유지(미래 일봉 부족)."}

        actual = {
            "analysis_date": analysis_date,
            "baseline_close": decided_metrics["baseline_close"],
            "return_pct": decided_metrics["return_pct"],
            "drawdown_pct": decided_metrics["drawdown_pct"],
            "windows": {k: {"return_pct": v["return_pct"],
                            "drawdown_pct": v["drawdown_pct"]}
                        for k, v in per_window.items()},
            "evaluated_at": _now(),
            "decided_window": decided_window,
        }
        # 편의 키: return_5d/20d/60d, max_drawdown
        for w in windows:
            key = f"{w}d"
            if key in per_window:
                actual[f"return_{key}"] = per_window[key]["return_pct"]
        actual["max_drawdown"] = decided_metrics["drawdown_pct"]

        res = lesson_runs.record_outcome(int(lesson_id), decided_window, actual, conn=conn)
        res["status"] = "evaluated"
        res["per_window"] = per_window
        res["analysis_date"] = analysis_date
        return res
    finally:
        if own:
            conn.close()


def evaluate_pending(*, window_days: list[int] | None = None,
                     scope_key: str | None = None,
                     history: list[dict] | None = None, conn=None) -> dict:
    """hit_or_miss=pending 인 모든(혹은 scope_key 한정) price-scope lesson_run 자동 평가.

    각 lesson 의 분석일 이후 일봉으로 시장반응을 계산해 record_outcome.
    미래 일봉이 부족하면 그 lesson 은 pending 유지(다음 적재 후 재평가).
    **자동주문/policy 변경 0.**
    """
    windows = sorted(window_days or DEFAULT_WINDOWS)
    own = conn is None
    conn = conn or store_db.connect()
    try:
        q = ("SELECT id FROM lesson_runs WHERE hit_or_miss='pending' "
             "AND scope_type IN (%s)" % ",".join("?" * len(PRICE_EVALUABLE_SCOPES)))
        params: list = list(PRICE_EVALUABLE_SCOPES)
        if scope_key:
            q += " AND scope_key=?"
            params.append(scope_key)
        q += " ORDER BY id ASC"
        ids = [r["id"] for r in conn.execute(q, params).fetchall()]

        evaluated, still_pending, skipped, results = 0, 0, 0, []
        for lid in ids:
            out = evaluate_lesson(lid, windows=windows, history=history, conn=conn)
            results.append(out)
            status = out.get("status")
            if status == "evaluated":
                evaluated += 1
            elif status == "pending":
                still_pending += 1
            else:
                skipped += 1
        return {"ok": True, "windows": windows, "candidates": len(ids),
                "evaluated": evaluated, "still_pending": still_pending,
                "skipped": skipped, "auto_orders": 0, "policy_changes": 0,
                "results": results}
    finally:
        if own:
            conn.close()


# ============================================================
# CLI — 평가·기록 전용 (자동주문/policy 0)
# ============================================================
def _main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="lesson_outcome — 분석 이후 시장반응 자동 기록 + reliability 갱신 (평가만)")
    p.add_argument("--evaluate", action="store_true", help="pending lesson_run 자동 평가")
    p.add_argument("--scope-key", help="특정 종목코드만 평가(예: 005930)")
    p.add_argument("--windows", default="5,20,60", help="평가 거래일(콤마, 기본 5,20,60)")
    p.add_argument("--lesson-id", type=int, help="단일 lesson_run 평가")
    a = p.parse_args(argv)

    windows = [int(x) for x in a.windows.split(",") if x.strip()]
    if a.lesson_id is not None:
        out = evaluate_lesson(a.lesson_id, windows=windows)
    elif a.evaluate:
        out = evaluate_pending(window_days=windows, scope_key=a.scope_key)
    else:
        p.print_help()
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
