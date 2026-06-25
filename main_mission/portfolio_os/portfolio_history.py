"""일별 포트폴리오 추이 지속 관리 — 총자산 / 자산군(bucket) 비중 / 종목별 추이.

본질: 매일 계좌의 상태를 1행(계좌×일)으로 정직하게 적재하고, 그 누적을 일별 시계열로 조회한다.
실시간 봇이 아니라 **지속 기록**이다. 추세는 기록이 쌓여야 의미가 생긴다.

원칙 (CLAUDE.md):
  - 계좌 격리: 모든 기록/조회는 account_index 로 분리. A 의 추이가 B 와 섞이지 않는다.
  - 지능/저장은 로컬 DB 만. Anthropic API 사용 안 함 (산술/집계만).
  - **정직(NO mock)**: 보유종목이 아직 동기화 전이면 가짜 종목을 만들지 않는다.
    현금/총자산만 기록하고 holdings_tracked=false 로 명시한다.
    종목별 추이는 **실거래 보유종목이 동기화된 후부터** 쌓인다.

저장 위치(중요):
  활성 SQLite 스키마(store/schema.sql)에는 `portfolio_snapshots` 테이블이 없다(이는 PG 초안에만 존재).
  실제 일별 금액 truth 테이블은 `account_snapshots`(account_index, captured_at, total_value_krw,
  cash_krw, holdings_count) + 행단위 `holdings` 이며, sync_job 이 쓰는 그 테이블이다.
  스키마 변경 금지 규약에 따라 신규 테이블/컬럼을 만들지 않고 이 테이블을 일별 스냅샷 저장소로 사용한다.
  멱등: 같은 계좌·같은 날(UTC date)에 record_daily 재실행 시 그날 history 행을 교체(중복 적재 방지).

  python -m main_mission.portfolio_os.portfolio_history --account 1 --record
  python -m main_mission.portfolio_os.portfolio_history --account 1 --series
  python -m main_mission.portfolio_os.portfolio_history --account 1 --advice

자동 트리거(중요 — sync_job 은 B 에이전트 영역이므로 수정하지 않는다):
  record_daily 는 운영 스냅샷(sync_job 적재)을 출처로 하므로, **동기화 직후 별도 cron/CLI** 로 호출한다.
  권장: sync 후 같은 스케줄에 이어서 1줄 실행(멱등 — 같은 날 재실행 안전).
    # 매일 장 마감 후(예: cron 0 16 * * 1-5)
    .venv/bin/python -m main_mission.portfolio_os.portfolio_history --account 1 --record
  sync_job 코드에 직접 호출을 심지 않는다(영역 분리). cron 또는 운영 스크립트로 외부에서 트리거한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .store import db as store_db

# record_daily 가 적재하는 history 행의 source 라벨 — 멱등 키(계좌×일×source)의 일부.
HISTORY_SOURCE = "daily_history"

# 노출(exposure) 계산 시 kind 분류 — decision.py 와 동일 정의(호출은 하지 않고 정의만 미러).
#   long  = anchor + tilt(롱)   · short = hedge(인버스/숏)
#   net   = long - short        · gross = long + short
#   theme_exposure = tilt 별 합  · hedge_exposure = hedge 합
#   방어(cash + bond)는 노출 계산에서 제외.
_LONG_KINDS = ("anchor", "tilt")
_DEFENSIVE_KINDS = ("cash", "bond")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _date_of(captured_at: str | None) -> str | None:
    """captured_at(ISO 또는 'YYYY-MM-DD HH:MM:SS') → 'YYYY-MM-DD' (UTC date)."""
    if not captured_at:
        return None
    s = str(captured_at).strip().replace("T", " ")
    return s[:10] if len(s) >= 10 else None


# ---------------------------------------------------------------------------
# 자산군(bucket) 분류 — 순현금 / 채권 / 위험
# ---------------------------------------------------------------------------
# 보유종목 ticker/name 에서 채권 성격을 보수적으로 추정한다(휴리스틱, 데이터 없으면 위험으로 분류).
# 가짜 정밀도를 피한다: 분류 불확실은 '위험'으로 두되 bucket_basis 로 근거를 남긴다.
_BOND_HINTS = ("국채", "채권", "BOND", "TREASUR", "TLT", "IEF", "SHY", "AGG", "BND", "회사채", "통안")


def _classify_bond(ticker: str | None, name: str | None) -> bool:
    blob = f"{ticker or ''} {name or ''}".upper()
    return any(h.upper() in blob for h in _BOND_HINTS)


def _buckets(total: float, cash: float, holdings: list[dict]) -> dict:
    """순현금 / 채권 / 위험 비중(%) — 합 100 지향. total=0 이면 0 처리(분모 보호)."""
    if total <= 0:
        return {"순현금": 0.0, "채권": 0.0, "위험": 0.0}
    bond_val = sum(float(h.get("value") or 0) for h in holdings if h.get("is_bond"))
    risk_val = sum(float(h.get("value") or 0) for h in holdings if not h.get("is_bond"))
    cash_pct = round(cash / total * 100, 1)
    bond_pct = round(bond_val / total * 100, 1)
    risk_pct = round(risk_val / total * 100, 1)
    return {"순현금": cash_pct, "채권": bond_pct, "위험": risk_pct}


# ---------------------------------------------------------------------------
# 노출(exposure) — selected allocation(확정 안)의 rows 에서 직접 계산
# ---------------------------------------------------------------------------
# 주의: decision.py 를 호출하지 않는다(B 에이전트가 동시 수정 중). 동일 정의를 여기서 직접 계산만 한다.
def _active_allocation(conn, account_index) -> list[dict] | None:
    """확정된(active) selected allocation 의 rows([{kind, ref, weight_pct}, ...]) — 없으면 None.

    출처: allocation_selections(status='active').allocation(JSON). 계좌 격리(account_index 조건).
    """
    row = conn.execute(
        "SELECT allocation FROM allocation_selections "
        "WHERE account_index=? AND status='active' ORDER BY id DESC LIMIT 1",
        (account_index,),
    ).fetchone()
    if not row or not row["allocation"]:
        return None
    try:
        alloc = json.loads(row["allocation"])
        return alloc if isinstance(alloc, list) else None
    except Exception:  # noqa: BLE001
        return None


def _exposure_from_alloc(alloc: list[dict] | None) -> dict | None:
    """selected allocation rows → 노출 지표(net/gross/theme/hedge). alloc 없으면 None.

    long  = Σ weight(kind in anchor,tilt)
    short = Σ weight(kind=hedge)
    net   = long - short · gross = long + short
    theme_exposure = {tilt ref: 합} · hedge_exposure = Σ hedge
    방어(cash+bond)는 노출에서 제외.
    """
    if not alloc:
        return None
    long_total = round(sum(float(el.get("weight_pct") or 0)
                           for el in alloc if el.get("kind") in _LONG_KINDS), 1)
    hedge_total = round(sum(float(el.get("weight_pct") or 0)
                            for el in alloc if el.get("kind") == "hedge"), 1)
    theme: dict[str, float] = {}
    for el in alloc:
        if el.get("kind") == "tilt":
            ref = el.get("ref") or "테마"
            theme[ref] = round(theme.get(ref, 0.0) + float(el.get("weight_pct") or 0), 1)
    return {
        "long_pct": long_total,
        "short_pct": hedge_total,
        "net_pct": round(long_total - hedge_total, 1),
        "gross_pct": round(long_total + hedge_total, 1),
        "hedge_exposure_pct": hedge_total,
        "theme_exposure": theme,
        "basis": "selected allocation(확정 안)의 확정 비중 — 보유종목 미반영(목표 기준).",
    }


def _latest_drift(conn, account_index, on_or_before_date: str | None = None) -> float | None:
    """daily_portfolio_reviews.drift_score 최근값(계좌 격리). 날짜 지정 시 그 날짜 이하의 최근.

    None 이면 점검 기록이 없거나 drift 미기록(정직 — 가짜 0 만들지 않음).
    """
    if on_or_before_date:
        row = conn.execute(
            "SELECT drift_score FROM daily_portfolio_reviews "
            "WHERE account_index=? AND review_date<=? AND drift_score IS NOT NULL "
            "ORDER BY review_date DESC, id DESC LIMIT 1",
            (account_index, on_or_before_date),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT drift_score FROM daily_portfolio_reviews "
            "WHERE account_index=? AND drift_score IS NOT NULL "
            "ORDER BY review_date DESC, id DESC LIMIT 1",
            (account_index,),
        ).fetchone()
    return round(float(row["drift_score"]), 1) if row and row["drift_score"] is not None else None


# ---------------------------------------------------------------------------
# record_daily — 오늘의 스냅샷 1행 적재(계좌×일 멱등)
# ---------------------------------------------------------------------------
def _latest_source_snapshot(conn, account_index):
    """가장 최근(운영 sync 등) 스냅샷 — record 의 금액/보유 출처. history 행 자신은 제외."""
    return conn.execute(
        "SELECT id, cash_krw, total_value_krw, holdings_count, captured_at FROM account_snapshots "
        "WHERE account_index=? AND COALESCE(source,'')<>? ORDER BY id DESC LIMIT 1",
        (account_index, HISTORY_SOURCE),
    ).fetchone()


def _holdings_of(conn, snapshot_id) -> list[dict]:
    rows = conn.execute(
        "SELECT ticker, name, qty, market_value FROM holdings WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "symbol": r["ticker"],
            "name": r["name"],
            "qty": float(r["qty"] or 0),
            "value": float(r["market_value"] or 0),
            "is_bond": _classify_bond(r["ticker"], r["name"]),
        })
    return out


def record_daily(account_index: int, record_date: str | None = None) -> dict:
    """오늘의 추이 스냅샷을 적재한다(계좌×일 1행, 멱등 — 같은 날 재실행 시 교체).

    출처: 가장 최근 운영 스냅샷(sync_job 등). 없으면 적재 불가(정직 — 동기화 먼저).
    보유종목이 0개면 holdings_tracked=false 로 현금/총자산만 기록(가짜 종목 생성 안 함).
    """
    record_date = record_date or _today()
    conn = store_db.connect()
    try:
        src = _latest_source_snapshot(conn, account_index)
        if not src:
            return {
                "ok": False, "account_index": account_index, "date": record_date,
                "error": "운영 스냅샷 없음 — 동기화 후 기록 가능(현금/총자산도 출처가 필요).",
            }

        total = float(src["total_value_krw"] or 0)
        cash = float(src["cash_krw"] or 0)
        src_holdings = _holdings_of(conn, src["id"])
        holdings_tracked = len(src_holdings) > 0

        # 종목별 비중(weight_pct) 계산 — total=0 이면 0.
        per_holding = []
        for h in src_holdings:
            w = round(h["value"] / total * 100, 1) if total > 0 else 0.0
            per_holding.append({
                "symbol": h["symbol"], "name": h["name"],
                "qty": h["qty"], "value": h["value"], "weight_pct": w,
            })

        buckets = _buckets(total, cash, src_holdings)
        # 노출(net/gross/테마/hedge)은 selected allocation(확정 안)의 rows 에서 직접 계산.
        #   보유종목이 없으면 목표 기준 — 정직 라벨(holdings_tracked=false)로 표시된다.
        exposure = _exposure_from_alloc(_active_allocation(conn, account_index))
        drift = _latest_drift(conn, account_index, on_or_before_date=record_date)

        # 멱등: 같은 계좌·같은 날의 history 행 + 그 행의 holdings 를 먼저 제거(교체).
        old = conn.execute(
            "SELECT id FROM account_snapshots WHERE account_index=? AND source=? "
            "AND substr(replace(captured_at,'T',' '),1,10)=?",
            (account_index, HISTORY_SOURCE, record_date),
        ).fetchall()
        for o in old:
            conn.execute("DELETE FROM holdings WHERE snapshot_id=?", (o["id"],))
            conn.execute("DELETE FROM account_snapshots WHERE id=?", (o["id"],))

        # captured_at 을 'record_date 00:00:00' 로 고정 → 같은 날 멱등 키가 안정적.
        captured = f"{record_date} 00:00:00"
        cur = conn.execute(
            "INSERT INTO account_snapshots(account_index, cash_krw, total_value_krw, holdings_count, "
            "source, is_stale, captured_at) VALUES(?,?,?,?,?,0,?)",
            (account_index, cash, total, len(src_holdings), HISTORY_SOURCE, captured),
        )
        new_id = cur.lastrowid
        for h in src_holdings:
            conn.execute(
                "INSERT INTO holdings(snapshot_id, account_index, ticker, name, qty, market_value, currency, captured_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (new_id, account_index, h["symbol"], h["name"], h["qty"], h["value"], "KRW", captured),
            )
        conn.commit()

        return {
            "ok": True, "account_index": account_index, "date": record_date,
            "snapshot_id": new_id,
            "total_value_krw": round(total, 0),
            "cash_krw": round(cash, 0),
            "holdings_tracked": holdings_tracked,
            "holdings": per_holding,
            "buckets": buckets,
            "exposure": exposure,          # selected allocation 기준 net/gross/테마/hedge (없으면 None)
            "drift_score": drift,          # daily_portfolio_reviews 최근 drift (없으면 None — 정직)
            "note": (None if holdings_tracked
                     else "보유종목 동기화 전 — 현금/총자산만 기록. 종목별 추이는 라이브 동기화 후부터 쌓입니다."),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# series — 일별 시계열 조회(계좌 격리)
# ---------------------------------------------------------------------------
def series(account_index: int, days: int = 30) -> dict:
    """최근 `days` 일의 일별 추이(날짜 오름차순).

    반환:
      dates: ['YYYY-MM-DD', ...]  (오름차순)
      total_value: [float, ...]
      cash: [float, ...]
      holdings_by_symbol: { symbol: [{date, value, weight}, ...] }
      bucket_series: { '순현금':[...], '채권':[...], '위험':[...] }  (날짜별 %)
      exposure_series: { net:[...], gross:[...], hedge:[...], theme:[...], long:[...], short:[...] }  (날짜별 %, selected allocation 기준)
      exposure: { net_pct, gross_pct, hedge_exposure_pct, theme_exposure{}, long_pct, short_pct }  (현재 확정 안)
      drift_series: [float|None, ...]  (날짜별 daily_portfolio_reviews.drift_score, 최근값 carry-forward)
      holdings_tracked: bool   (시계열 전체에 종목 데이터가 한 번이라도 있으면 true)
      exposure_tracked: bool   (확정된 selected allocation 이 있으면 true)
      note: 정직 안내
    """
    days = max(1, int(days))
    conn = store_db.connect()
    try:
        # history 행 우선, 없으면 운영 스냅샷도 포함(추이가 끊기지 않게) — 같은 날은 1행으로.
        rows = conn.execute(
            "SELECT id, cash_krw, total_value_krw, captured_at, source FROM account_snapshots "
            "WHERE account_index=? ORDER BY captured_at ASC, id ASC",
            (account_index,),
        ).fetchall()

        # 날짜별로 묶되, 같은 날에 history 행이 있으면 그것을, 없으면 가장 늦은 운영 스냅샷을 채택.
        by_date: dict[str, dict] = {}
        for r in rows:
            d = _date_of(r["captured_at"])
            if not d:
                continue
            prev = by_date.get(d)
            is_hist = (r["source"] == HISTORY_SOURCE)
            # history 행이면 무조건 우선 채택; 아니면(운영 행) 기존이 운영 행일 때만 더 늦은 것으로 갱신.
            if prev is None or is_hist or (not prev["is_hist"]):
                by_date[d] = {"row": r, "is_hist": is_hist}

        ordered_dates = sorted(by_date.keys())[-days:]

        # 노출(net/gross/테마/hedge)은 selected allocation(확정 안)의 rows 에서 직접 계산(decision.py 미호출).
        #   확정 안은 계좌의 현재 plan 이므로 시계열 전 구간에 동일 적용(목표 기준). 보유종목 미반영.
        exposure = _exposure_from_alloc(_active_allocation(conn, account_index))

        dates: list[str] = []
        total_value: list[float] = []
        cash: list[float] = []
        bucket_series = {"순현금": [], "채권": [], "위험": []}
        exposure_series: dict[str, list[float]] = {
            "net": [], "gross": [], "hedge": [], "theme": [], "long": [], "short": []}
        drift_series: list[float | None] = []
        holdings_by_symbol: dict[str, list[dict]] = {}
        any_holdings = False

        for d in ordered_dates:
            r = by_date[d]["row"]
            total = float(r["total_value_krw"] or 0)
            c = float(r["cash_krw"] or 0)
            dates.append(d)
            total_value.append(round(total, 0))
            cash.append(round(c, 0))

            hs = _holdings_of(conn, r["id"])
            if hs:
                any_holdings = True
            b = _buckets(total, c, hs)
            bucket_series["순현금"].append(b["순현금"])
            bucket_series["채권"].append(b["채권"])
            bucket_series["위험"].append(b["위험"])

            for h in hs:
                sym = h["symbol"]
                w = round(h["value"] / total * 100, 1) if total > 0 else 0.0
                holdings_by_symbol.setdefault(sym, []).append(
                    {"date": d, "value": round(h["value"], 0), "weight": w})

            # 노출 시리즈: 확정 안이 있으면 그 값을 날짜별로(목표 기준) — 없으면 0(정직 라벨로 구분).
            if exposure:
                exposure_series["net"].append(exposure["net_pct"])
                exposure_series["gross"].append(exposure["gross_pct"])
                exposure_series["hedge"].append(exposure["hedge_exposure_pct"])
                exposure_series["theme"].append(round(sum(exposure["theme_exposure"].values()), 1))
                exposure_series["long"].append(exposure["long_pct"])
                exposure_series["short"].append(exposure["short_pct"])
            else:
                for k in exposure_series:
                    exposure_series[k].append(0.0)

            # drift 시리즈: 그 날짜 이하의 가장 최근 drift_score(carry-forward). 없으면 None(정직).
            drift_series.append(_latest_drift(conn, account_index, on_or_before_date=d))

        note = (None if any_holdings else
                "보유종목 동기화 전 — 현금/총자산만 기록. 종목별 추이는 라이브 동기화 후부터 쌓입니다.")

        return {
            "ok": True,
            "account_index": account_index,
            "days": days,
            "dates": dates,
            "total_value": total_value,
            "cash": cash,
            "holdings_by_symbol": holdings_by_symbol,
            "bucket_series": bucket_series,
            "exposure_series": exposure_series,
            "exposure": exposure,                 # 현재 확정 안 기준 노출(없으면 None)
            "exposure_tracked": exposure is not None,
            "drift_series": drift_series,
            "holdings_tracked": any_holdings,
            "point_count": len(dates),
            "note": note,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# advice_history — 조언 적용/편집/무시/저장 타임라인(계좌 격리)
# ---------------------------------------------------------------------------
# user_action 분류(field_advice_events): applied|edited|ignored|saved.
#   - applied/edited/saved = 반영(반영군), ignored = 무시.
_ACTION_LABELS = {
    "applied": "적용", "edited": "수정 적용", "ignored": "무시", "saved": "저장",
}
# 반영(applied/edited/saved) vs 무시(ignored) 분류 — 카드 통계용.
_ACTION_KEPT = ("applied", "edited", "saved")


def _policy_version_at(conn, account_index, at_ts: str | None) -> int | None:
    """주어진 시점(at_ts) 기준으로 활성이던 portfolio_policies.version (계좌 격리).

    at_ts(이벤트 created_at) 이하로 가장 최근에 컴파일된 정책 버전 — "이 결정이 어느 정책 버전에서
    내려졌는가"를 보여준다. at_ts 없으면 최신 버전. 정책 기록이 없으면 None(정직 — 가짜 버전 금지).
    """
    if at_ts:
        row = conn.execute(
            "SELECT version FROM portfolio_policies "
            "WHERE account_index=? AND created_at<=? ORDER BY version DESC LIMIT 1",
            (account_index, at_ts),
        ).fetchone()
        if row is not None:
            return int(row["version"])
        # 이벤트가 첫 정책보다 이전이면, 그 계좌의 가장 이른 버전을 표시(가짜 없음, 보수적).
    row = conn.execute(
        "SELECT version FROM portfolio_policies WHERE account_index=? ORDER BY version DESC LIMIT 1",
        (account_index,),
    ).fetchone()
    return int(row["version"]) if row is not None else None


def advice_history(account_index: int, limit: int = 50) -> dict:
    """필드 조언에 대한 사람의 결정 타임라인(적용/수정/무시/저장) — 계좌 격리.

    출처: field_advice_events(append-only) + field_consultations(조언 본문/필드/출처 evidence·lesson).
    반환:
      events: [{id, field_name, user_action, action_label, kept(bool), detail, agent_name,
                advice_type, evidence_count, lesson_count, suggested_text, policy_version,
                created_at}, ...]  (최신순)
      counts: { applied, edited, ignored, saved, kept_total, ignored_total }
      policy_version_current: int|None  (계좌 현재 정책 버전 — 없으면 None)
      note: 정직 안내(빈 이력)
    """
    limit = max(1, min(int(limit), 500))
    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT e.id, e.field_name, e.user_action, e.detail, e.created_at, "
            "       e.field_consultation_id, "
            "       c.agent_name, c.advice_type, c.suggested_text, c.evidence_ids, c.lesson_ids "
            "FROM field_advice_events e "
            "LEFT JOIN field_consultations c ON c.id = e.field_consultation_id "
            "WHERE e.account_index=? ORDER BY e.id DESC LIMIT ?",
            (account_index, limit),
        ).fetchall()

        def _count_csv(v) -> int:
            if not v:
                return 0
            s = str(v).strip()
            if not s:
                return 0
            # CSV 또는 JSON 배열 모두 허용(정직 — 파싱 실패 시 0).
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return len([x for x in parsed if str(x).strip()])
            except Exception:  # noqa: BLE001
                pass
            return len([p for p in s.split(",") if p.strip()])

        events = []
        counts = {"applied": 0, "edited": 0, "ignored": 0, "saved": 0}
        for r in rows:
            action = (r["user_action"] or "").strip()
            if action in counts:
                counts[action] += 1
            kept = action in _ACTION_KEPT
            events.append({
                "id": r["id"],
                "field_consultation_id": r["field_consultation_id"],
                "field_name": r["field_name"],
                "user_action": action,
                "action_label": _ACTION_LABELS.get(action, action or "기타"),
                "kept": kept,
                "detail": r["detail"],
                "agent_name": r["agent_name"],
                "advice_type": r["advice_type"],
                "evidence_count": _count_csv(r["evidence_ids"]),
                "lesson_count": _count_csv(r["lesson_ids"]),
                "suggested_text": r["suggested_text"],
                # 이 결정이 내려진 시점에 활성이던 정책 버전(추적성 — 어느 정책 위에서 적용/무시했는가).
                "policy_version": _policy_version_at(conn, account_index, r["created_at"]),
                "created_at": r["created_at"],
            })

        kept_total = counts["applied"] + counts["edited"] + counts["saved"]
        counts_out = {**counts, "kept_total": kept_total, "ignored_total": counts["ignored"]}
        note = (None if events else
                "아직 조언 적용/무시 이력이 없습니다 — 대전제·중전제 조언을 적용하거나 보류하면 여기에 쌓입니다.")
        return {
            "ok": True,
            "account_index": account_index,
            "events": events,
            "counts": counts_out,
            "policy_version_current": _policy_version_at(conn, account_index, None),
            "point_count": len(events),
            "note": note,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# growth_history — 성장(evidence/lesson/regression) 이력
# ---------------------------------------------------------------------------
# 본질: "에이전트가 메모리로 어떻게 성장했는가"의 추적. 4계층:
#   1) 최근 evidence : 자료조사 근거 (stance/freshness/confidence) — 계좌 격리(다른 계좌 근거 제외).
#   2) lesson 후보   : 승격 전 관찰(lesson_candidates) — 계좌 격리(account_index 일치 또는 전역).
#   3) promoted lesson: 계좌 간 공통 성장(agent_memories.promoted=1) — **익명화된 것만 노출**(개인/계좌 0).
#   4) regression    : 실패→회귀테스트 승격(task_regression_tests) — 시스템 학습(개인정보 없음).
# 정직(NO mock): 데이터 없으면 빈 리스트 + 안내. 가짜 evidence/lesson 생성 안 함.
_STANCE_LABELS = {
    "long_support": "롱 근거", "short_support": "숏 근거", "hedge_support": "헤지 근거",
    "risk_warning": "위험 경고", "watch_only": "관찰만", "insufficient_evidence": "근거 부족",
    "conflicting_evidence": "상충 근거",
}


def growth_history(account_index: int, limit: int = 20) -> dict:
    """성장(evidence/lesson/regression) 이력 — 계좌 격리 + promoted lesson 익명화.

    반환:
      evidence: [{id, source_type, theme, topic, summary, stance, stance_label,
                  base_confidence, eff_confidence, age_days, freshness_at}, ...]
                (최근/신뢰도순, 계좌 격리 — 다른 계좌 근거 제외)
      lesson_candidates: [{id, scope, ref, title, observed_count, confidence, status, created_at}, ...]
                (승격 전 관찰, 계좌 격리 — 이 계좌 또는 전역(account_index NULL))
      promoted_lessons: [{id, scope_type, agent_name, theme, sector, title, body, confidence}, ...]
                (**익명화된 promoted=1 만** — 개인/계좌 식별정보 0, 계좌 무관 공통 성장)
      regression_promotions: [{id, task_type, title, expect, status, created_at}, ...]
                (실패→회귀테스트 승격 — 시스템 학습, 개인정보 없음)
      counts: { evidence, lesson_candidates, promoted_lessons, regression }
      anonymized: true  (promoted lesson 은 익명화 보장 — 안전 라벨)
      note: 정직 안내
    """
    limit = max(1, min(int(limit), 100))
    conn = store_db.connect()
    try:
        # 1) evidence — evidence.recall_evidence 재사용(계좌 격리 + freshness decay + stance 보존).
        try:
            from . import evidence as ev_mod
            ev_rows = ev_mod.recall_evidence(account_index=account_index, limit=limit, conn=conn)
        except Exception:  # noqa: BLE001
            ev_rows = []
        evidence = [{
            "id": e["id"], "source_type": e.get("source_type"), "theme": e.get("theme"),
            "topic": e.get("topic"), "summary": (e.get("summary") or "")[:240],
            "stance": e.get("stance"), "stance_label": _STANCE_LABELS.get(e.get("stance") or "", e.get("stance")),
            "base_confidence": e.get("base_confidence"), "eff_confidence": e.get("eff_confidence"),
            "age_days": e.get("age_days"), "freshness_at": e.get("freshness_at"),
        } for e in ev_rows]

        # 2) lesson 후보 — 계좌 격리(이 계좌 또는 전역 NULL). 승격 전 관찰.
        lc_rows = conn.execute(
            "SELECT id, scope, ref, title, observed_count, confidence, status, created_at "
            "FROM lesson_candidates "
            "WHERE (account_index=? OR account_index IS NULL) "
            "ORDER BY id DESC LIMIT ?",
            (account_index, limit),
        ).fetchall()
        lesson_candidates = [{
            "id": r["id"], "scope": r["scope"], "ref": r["ref"], "title": r["title"],
            "observed_count": r["observed_count"], "confidence": r["confidence"],
            "status": r["status"], "created_at": r["created_at"],
        } for r in lc_rows]

        # 3) promoted lesson — agent_memories.promoted=1 (계좌 간 공통 성장).
        #    승격 시 익명화(account_index/scope_id NULL + 텍스트 토큰화) — 여기선 그 결과만 노출.
        #    **안전**: promoted=1 AND account_index IS NULL 만(개인/계좌 식별정보 0 보장).
        pl_rows = conn.execute(
            "SELECT id, scope_type, agent_name, theme, sector, title, body, confidence "
            "FROM agent_memories "
            "WHERE promoted=1 AND archived=0 AND account_index IS NULL "
            "ORDER BY confidence DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        # 방어적 익명화 — 승격본은 이미 익명화되었지만, 노출 직전 한 번 더 토큰화(이중 안전).
        try:
            from .growth.memory import _anonymize as _anon
        except Exception:  # noqa: BLE001
            _anon = lambda s: (s or "")  # noqa: E731
        promoted_lessons = [{
            "id": r["id"], "scope_type": r["scope_type"], "agent_name": r["agent_name"],
            "theme": r["theme"], "sector": r["sector"],
            "title": _anon(r["title"]), "body": _anon(r["body"]), "confidence": r["confidence"],
        } for r in pl_rows]

        # 4) regression 승격 — 실패→회귀테스트(task_regression_tests). 시스템 학습(개인정보 없음).
        rg_rows = conn.execute(
            "SELECT id, task_type, title, expect, status, created_at "
            "FROM task_regression_tests WHERE status='active' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        regression_promotions = [{
            "id": r["id"], "task_type": r["task_type"], "title": r["title"],
            "expect": r["expect"], "status": r["status"], "created_at": r["created_at"],
        } for r in rg_rows]

        counts = {
            "evidence": len(evidence), "lesson_candidates": len(lesson_candidates),
            "promoted_lessons": len(promoted_lessons), "regression": len(regression_promotions),
        }
        total = sum(counts.values())
        note = (None if total > 0 else
                "아직 성장 이력이 없습니다 — 자료조사 근거(evidence)·교훈 후보·승격된 공통 교훈·회귀테스트가 쌓이면 여기에 표시됩니다.")
        return {
            "ok": True,
            "account_index": account_index,
            "evidence": evidence,
            "lesson_candidates": lesson_candidates,
            "promoted_lessons": promoted_lessons,
            "regression_promotions": regression_promotions,
            "counts": counts,
            "anonymized": True,   # promoted lesson 은 익명화 보장(개인/계좌 식별정보 0)
            "point_count": total,
            "note": note,
        }
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--record", action="store_true", help="오늘의 추이 스냅샷 적재(멱등)")
    ap.add_argument("--series", action="store_true", help="일별 시계열 조회")
    ap.add_argument("--advice", action="store_true", help="조언 적용/무시 타임라인 조회")
    ap.add_argument("--growth", action="store_true", help="성장(evidence/lesson/regression) 이력 조회")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()
    try:
        if args.record:
            out = record_daily(args.account)
        elif args.advice:
            out = advice_history(args.account, limit=args.limit)
        elif args.growth:
            out = growth_history(args.account, limit=args.limit)
        else:
            out = series(args.account, days=args.days)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
