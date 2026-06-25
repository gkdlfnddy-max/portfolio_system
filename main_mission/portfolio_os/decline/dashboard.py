"""하락 징후 Dashboard 데이터 (조회 전용 — 웹 화면은 후속).

decline_analyses(분석 기록) 영속화 위에서 **추이/이력 데이터**만 산출한다.
웹은 조회 전용(CLAUDE.md §7,§18) — 여기서는 데이터 함수만 제공하고, 화면/렌더는 web/ 후속.

제공 데이터:
  - risk_trend        : 최근 위험점수/confidence 추이(시간순)
  - confidence_trend  : confidence 추이(risk_trend 에 포함)
  - missing_axes_freq : 부족(미연동) 데이터 축 빈도(어디를 채워야 신뢰 오를지)
  - conservative_shifts : 보수적 전환 제안 이력(+사용자 반응)
  - prediction_scoreboard : 제안 적중/미스 집계(평가 완료분만 — 정직)
  - reliability_snapshot : axis/instrument/sector reliability 현재값(중립=데이터 부족)

원칙: Anthropic API 미사용. 자동주문 0(읽기 전용). 실현결과 적으면 reliability 중립 유지.
계좌 필터(account_index)는 "그 계좌가 본 분석"만 — reliability 성장은 계좌 무관(시장 공통).
"""
from __future__ import annotations

import argparse
import json
import sys

from ..store import db as store_db
from . import track_record as tr


def _acct_clause(account_index: int | None, params: list) -> str:
    if account_index is None:
        return ""
    params.append(account_index)
    return " AND account_index=?"


def risk_trend(*, account_index: int | None = None, code: str | None = None,
               limit: int = 60) -> list[dict]:
    """최근 분석들의 위험점수 + confidence 추이(분석일 오름차순)."""
    params: list = []
    sql = ("SELECT analysis_id, code, sector, analysis_date, overall_risk, overall_confidence, "
           "hit_or_miss, actual_drawdown FROM decline_analyses WHERE 1=1")
    sql += _acct_clause(account_index, params)
    if code:
        sql += " AND code=?"; params.append(code)
    sql += " ORDER BY analysis_date DESC, analysis_id DESC LIMIT ?"; params.append(limit)
    conn = store_db.connect()
    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
    rows.reverse()  # 오래된 → 최신(차트용)
    return rows


def missing_axes_freq(*, account_index: int | None = None, limit: int = 500) -> dict:
    """미연동(부족) 데이터 축 빈도 — 어디를 채워야 분석 신뢰가 오르는지."""
    params: list = []
    sql = "SELECT missing_axes, available_axes FROM decline_analyses WHERE 1=1"
    sql += _acct_clause(account_index, params)
    sql += " ORDER BY analysis_id DESC LIMIT ?"; params.append(limit)
    conn = store_db.connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    miss: dict[str, int] = {}
    avail: dict[str, int] = {}
    for r in rows:
        for ax in json.loads(r["missing_axes"] or "[]"):
            miss[ax] = miss.get(ax, 0) + 1
        for ax in json.loads(r["available_axes"] or "[]"):
            avail[ax] = avail.get(ax, 0) + 1
    return {"analyses": len(rows),
            "missing_axes": dict(sorted(miss.items(), key=lambda kv: kv[1], reverse=True)),
            "available_axes": dict(sorted(avail.items(), key=lambda kv: kv[1], reverse=True))}


def conservative_shifts(*, account_index: int | None = None, limit: int = 50) -> list[dict]:
    """보수적 전환 제안 이력(+사용자 반응). suggested_action 또는 draft 생성된 건."""
    params: list = []
    sql = ("SELECT analysis_id, code, sector, analysis_date, overall_risk, overall_confidence, "
           "suggested_action, policy_draft_created, user_action, hit_or_miss, actual_drawdown "
           "FROM decline_analyses WHERE (suggested_action IS NOT NULL OR policy_draft_created=1)")
    sql += _acct_clause(account_index, params)
    sql += " ORDER BY analysis_id DESC LIMIT ?"; params.append(limit)
    conn = store_db.connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def prediction_scoreboard(*, account_index: int | None = None) -> dict:
    """제안 적중/미스 집계 — **평가 완료분만**(정직: pending/no_prediction 제외).

    실현결과(hit/miss)가 적으면 적중률 신뢰 낮음 → samples 동봉(과장 금지).
    """
    params: list = []
    sql = "SELECT hit_or_miss, COUNT(*) c FROM decline_analyses WHERE 1=1"
    sql += _acct_clause(account_index, params)
    sql += " GROUP BY hit_or_miss"
    conn = store_db.connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    counts = {r["hit_or_miss"]: r["c"] for r in rows}
    hits = counts.get("hit", 0)
    misses = counts.get("miss", 0)
    scored = hits + misses
    return {
        "hits": hits, "misses": misses, "scored": scored,
        "pending": counts.get("pending", 0),
        "no_prediction": counts.get("no_prediction", 0),
        "hit_rate": round(hits / scored, 3) if scored else None,
        "note": ("실현 결과 표본 적음 — 적중률 신뢰 낮음(정직)." if scored < 5
                 else "평가 완료분 기준 적중률."),
    }


def reliability_snapshot(*, axes: list[str] | None = None, codes: list[str] | None = None,
                         sectors: list[str] | None = None) -> dict:
    """축/종목/섹터 reliability 현재 스냅샷. 데이터 부족이면 0.5(중립) — 정직."""
    out: dict = {"axis": {}, "instrument": {}, "sector": {}}
    for ax in (axes or []):
        out["axis"][ax] = tr.reliability(ax)
    for c in (codes or []):
        out["instrument"][c] = tr.reliability_scoped("instrument", c)
    for s in (sectors or []):
        out["sector"][s] = tr.reliability_scoped("sector", s)
    return out


def dashboard(account_index: int | None = None) -> dict:
    """대시보드 전체 데이터 묶음(조회 전용). auto_order_created=False 명시."""
    trend = risk_trend(account_index=account_index)
    codes = sorted({r["code"] for r in trend})
    sectors = sorted({r["sector"] for r in trend if r.get("sector")})
    return {
        "ok": True,
        "account_index": account_index,
        "risk_trend": trend,
        "missing_axes_freq": missing_axes_freq(account_index=account_index),
        "conservative_shifts": conservative_shifts(account_index=account_index),
        "prediction_scoreboard": prediction_scoreboard(account_index=account_index),
        "reliability_snapshot": reliability_snapshot(codes=codes, sectors=sectors),
        "auto_order_created": False,
        "read_only": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int)
    args = ap.parse_args()
    sys.stdout.write(json.dumps(dashboard(args.account), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
