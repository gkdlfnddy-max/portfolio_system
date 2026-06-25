"""lesson run — 판단 → 시장반응/사용자반응 → reliability 갱신 → 다음 pre-hook 재사용.

흐름(성장 루프):
  1. record_lesson(...): 분석 시점의 판단(신호 요약·suggested_action·근거 memory/evidence ids)을
     기록한다. 이 시점엔 결과 미정(hit_or_miss=pending). **주문/policy 변경 없음** — 기록만.
  2. record_outcome(lesson_id, window, actual): analysis 이후 N거래일 시장반응(수익률/낙폭)을
     넣으면 hit/miss/false_alarm 으로 판정하고 **베이지안**으로 reliability 를 갱신한다.
  3. reliability(scope_type, scope_key): 그 자산/시장의 누적 신뢰도(prehook 우선순위에 사용).

reliability(베이지안):
  Beta(alpha, beta) 사후. 시작 prior alpha0=beta0=1(=0.5).
  hit → alpha += 1 / miss → beta += 1 / false_alarm → beta += 1(틀린 경보도 신뢰 하락).
  reliability = alpha / (alpha + beta).
  scope 단위(자산/시장 공통)로 누적 — **계좌 교차적용 아님**(시장 노하우).

지능 = Claude + 메모리 (Anthropic API 미사용 — import 없음). 자동 적용/주문 0.

테이블(이미 생성됨, 스키마 편집 금지): lesson_runs.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .store import db as store_db

HIT_STATES = ("hit", "miss", "false_alarm", "pending")
USER_ACTIONS = ("accepted", "ignored", "modified", "rejected", None)

PRIOR_ALPHA = 1.0
PRIOR_BETA = 1.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _json_ids(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps([int(x) for x in v])


# ============================================================
# record_lesson — 판단 시점 기록(결과 미정)
# ============================================================
def record_lesson(
    scope_type: str,
    scope_key: str,
    *,
    account_index: int | None = None,
    user_id: int | None = None,
    source_memory_ids=None,
    source_evidence_ids=None,
    decision_context=None,
    signal_summary=None,
    suggested_action=None,
    user_action=None,
    lesson_text=None,
    stale_at=None,
    conn=None,
) -> dict:
    """분석 시점 판단을 lesson_run 으로 기록. hit_or_miss=pending. 주문/적용 없음."""
    skey = _clean(scope_key)
    st = _clean(scope_type)
    if not st or not skey:
        raise ValueError("scope_type / scope_key 는 필수입니다")
    ua = _clean(user_action)
    if ua is not None and ua not in USER_ACTIONS:
        raise ValueError(f"user_action 은 {USER_ACTIONS} 중 하나여야 합니다")

    rel_before = reliability(st, skey, conn=conn)["reliability"]

    own = conn is None
    conn = conn or store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO lesson_runs("
            "scope_type, scope_key, account_index, user_id, "
            "source_memory_ids, source_evidence_ids, decision_context, signal_summary, "
            "suggested_action, user_action, market_reaction_window, actual_outcome, "
            "hit_or_miss, reliability_before, reliability_after, lesson_text, stale_at, "
            "created_at, last_used_at) "
            "VALUES(?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?)",
            (
                st, skey,
                int(account_index) if account_index not in (None, "") else None,
                int(user_id) if user_id not in (None, "") else None,
                _json_ids(source_memory_ids), _json_ids(source_evidence_ids),
                _clean(decision_context), _clean(signal_summary),
                _clean(suggested_action), ua, None, None,
                "pending", rel_before, None, _clean(lesson_text), _clean(stale_at),
                _now(), _now(),
            ),
        )
        conn.commit()
        return {"ok": True, "id": cur.lastrowid, "hit_or_miss": "pending",
                "reliability_before": rel_before}
    finally:
        if own:
            conn.close()


# ============================================================
# 판정 — 시장반응 → hit / miss / false_alarm
# ============================================================
def classify_outcome(suggested_action: str | None, actual: dict) -> str:
    """suggested_action 과 실제 시장반응(actual)을 비교해 결과 판정.

    actual 키(있는 것만 사용): return_pct(기간 수익률 %), drawdown_pct(최대 낙폭 %, 음수).
    규칙(보수적·설명가능):
      - 방어/축소 계열(shift_conservative/reduce/sell/hedge/short):
          실제 낙폭 발생(drawdown <= -DRAWDOWN_HIT 또는 return<0) → hit, 아니면 false_alarm.
      - 진입/증가 계열(buy/add/enter/long):
          return > 0 → hit, 아니면 miss.
      - hold/관망 또는 미상: 큰 변동 없으면 hit, 한쪽으로 크게 움직이면 miss.
    """
    act = (suggested_action or "").lower()
    ret = actual.get("return_pct")
    dd = actual.get("drawdown_pct")
    DEFENSIVE = ("shift_conservative", "reduce", "sell", "hedge", "short", "trim", "cut")
    OFFENSIVE = ("buy", "add", "enter", "long", "increase", "accumulate")

    if any(k in act for k in DEFENSIVE):
        fell = (dd is not None and dd <= -DRAWDOWN_HIT) or (ret is not None and ret < 0)
        return "hit" if fell else "false_alarm"
    if any(k in act for k in OFFENSIVE):
        if ret is None:
            return "pending"
        return "hit" if ret > 0 else "miss"
    # hold / 미상
    if ret is None:
        return "pending"
    return "miss" if abs(ret) >= BIG_MOVE else "hit"


DRAWDOWN_HIT = 5.0   # 방어 조언이 맞았다고 볼 최소 낙폭(%)
BIG_MOVE = 8.0       # hold 가 틀렸다고 볼 큰 변동(%)


def _bayes(alpha: float, beta: float, result: str) -> tuple[float, float]:
    if result == "hit":
        alpha += 1.0
    elif result in ("miss", "false_alarm"):
        beta += 1.0
    return alpha, beta


def reliability(scope_type: str, scope_key: str, *, conn=None) -> dict:
    """scope 누적 reliability — 평가된 lesson_run 들의 베이지안 사후.

    계좌 무관(시장 공통 노하우). hit/miss/false_alarm 카운트 + Beta 사후 평균.
    """
    st = _clean(scope_type)
    skey = _clean(scope_key)
    own = conn is None
    conn = conn or store_db.connect()
    try:
        rows = conn.execute(
            "SELECT hit_or_miss FROM lesson_runs WHERE scope_type=? AND scope_key=? "
            "AND hit_or_miss != 'pending'",
            (st, skey),
        ).fetchall()
    finally:
        if own:
            conn.close()

    alpha, beta = PRIOR_ALPHA, PRIOR_BETA
    counts = {"hit": 0, "miss": 0, "false_alarm": 0}
    for r in rows:
        res = r["hit_or_miss"]
        counts[res] = counts.get(res, 0) + 1
        alpha, beta = _bayes(alpha, beta, res)
    return {
        "scope_type": st,
        "scope_key": skey,
        "reliability": round(alpha / (alpha + beta), 4),
        "evaluated": len(rows),
        "counts": counts,
        "alpha": alpha,
        "beta": beta,
    }


# ============================================================
# record_outcome — 시장반응 입력 → 판정 → reliability 갱신
# ============================================================
def record_outcome(
    lesson_id: int,
    window: int,
    actual: dict,
    *,
    hit_or_miss: str | None = None,
    conn=None,
) -> dict:
    """analysis 이후 N거래일 시장반응을 넣어 결과 확정 + reliability 갱신.

    hit_or_miss 명시하면 그대로, 아니면 classify_outcome 으로 판정.
    reliability_before/after 는 그 scope 의 베이지안 사후 변화를 기록.
    """
    own = conn is None
    conn = conn or store_db.connect()
    try:
        row = conn.execute("SELECT * FROM lesson_runs WHERE id=?", (int(lesson_id),)).fetchone()
        if row is None:
            raise ValueError(f"lesson_run {lesson_id} 없음")
        st, skey = row["scope_type"], row["scope_key"]

        rel_before = reliability(st, skey, conn=conn)["reliability"]
        result = hit_or_miss or classify_outcome(row["suggested_action"], actual)
        if result not in HIT_STATES:
            raise ValueError(f"hit_or_miss 는 {HIT_STATES} 중 하나여야 합니다")

        conn.execute(
            "UPDATE lesson_runs SET market_reaction_window=?, actual_outcome=?, "
            "hit_or_miss=?, reliability_before=?, last_used_at=? WHERE id=?",
            (int(window), json.dumps(actual, ensure_ascii=False), result,
             rel_before, _now(), int(lesson_id)),
        )
        conn.commit()
        # 이 행이 평가됨에 따라 사후가 바뀐다 — 갱신 후 reliability 재계산.
        rel_after = reliability(st, skey, conn=conn)["reliability"]
        conn.execute(
            "UPDATE lesson_runs SET reliability_after=? WHERE id=?",
            (rel_after, int(lesson_id)),
        )
        conn.commit()
        return {
            "ok": True,
            "lesson_id": int(lesson_id),
            "scope_type": st,
            "scope_key": skey,
            "hit_or_miss": result,
            "window": int(window),
            "reliability_before": rel_before,
            "reliability_after": rel_after,
        }
    finally:
        if own:
            conn.close()


def recent_runs(scope_type: str, scope_key: str, *, limit: int = 20, conn=None) -> list[dict]:
    st, skey = _clean(scope_type), _clean(scope_key)
    own = conn is None
    conn = conn or store_db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM lesson_runs WHERE scope_type=? AND scope_key=? "
            "ORDER BY datetime(created_at) DESC LIMIT ?",
            (st, skey, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


# ============================================================
# CLI
# ============================================================
def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="lesson_runs — 판단→반응→reliability")
    p.add_argument("--record", action="store_true")
    p.add_argument("--outcome", action="store_true")
    p.add_argument("--reliability", action="store_true")
    p.add_argument("--scope-type")
    p.add_argument("--scope-key")
    p.add_argument("--account", type=int)
    p.add_argument("--signal")
    p.add_argument("--action")
    p.add_argument("--lesson-id", type=int)
    p.add_argument("--window", type=int, default=20)
    p.add_argument("--return-pct", type=float)
    p.add_argument("--drawdown-pct", type=float)
    a = p.parse_args(argv)

    if a.record:
        out = record_lesson(a.scope_type, a.scope_key, account_index=a.account,
                            signal_summary=a.signal, suggested_action=a.action)
    elif a.outcome:
        actual = {}
        if a.return_pct is not None:
            actual["return_pct"] = a.return_pct
        if a.drawdown_pct is not None:
            actual["drawdown_pct"] = a.drawdown_pct
        out = record_outcome(a.lesson_id, a.window, actual)
    elif a.reliability:
        out = reliability(a.scope_type, a.scope_key)
    else:
        p.print_help()
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
