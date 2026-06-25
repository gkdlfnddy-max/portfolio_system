"""정책/공시/뉴스 커넥터 (Track D) — 정책·규제·DART 주요공시·뉴스를 **요약 구조**로 저장.

CEO 본질: 사람은 매일 모든 정책/규제/공시/기사를 못 본다. 시스템이 **사실/해석/포트폴리오
영향/불확실성/추가확인** 으로 구분해 정리한다(읽기 전용 판단 보조). 가짜 뉴스 0.

저장 위치(스키마 편집 금지 — 이미 존재하는 테이블 사용):
  - `policy_events` (정책/규제 이벤트, decline/axes/policy.py 가 읽음):
      {event_date, sector, stance(adverse|favorable|neutral), severity(0~1), title, source}
  - `evidence_items` (자료 요약 — evidence_summary.add_evidence 재사용):
      source_type='filing'(공시) | 'news'(뉴스) | 'sector'(정책/규제 섹터이슈).
      → 사실/해석/불확실성은 summary + positive/negative/uncertainties 구조로 저장.

불변 원칙(CLAUDE.md §2, §11.8):
  - **공식/무료 우선.** 실시간 전체 자동연결은 하지 않는다 — **저장·요약 구조 + 수동입력**
    을 우선하고, 공식·무료 피드(예: DART 공시목록 list.json)가 연결되면 그때 자동 적재한다.
    현재 자동 피드 미연동이면 정직하게 data_connected=False(가짜 뉴스/이벤트 생성 0).
  - **사실 vs 해석 분리.** stance/severity 같은 *판단*은 사람·메모리가 정하고(기본 neutral),
    근거 없는 강한 단정 금지. 키워드 보조분류는 *후보*일 뿐 확정 아님.
  - **자동주문/policy 변경 0.** 이벤트/요약 저장까지만. 비중·주문은 사람 승인.
  - secret(.env) 0 · **Anthropic API 미사용**(분류는 키워드 룰) · 출처/날짜 필수.

확인한 공식 endpoint (financials_connect 와 동일 DART, WebSearch 검증):
  - DART 공시목록(주요공시 자동수집용, 선택적): opendart.fss.or.kr/api/list.json
      ?crtfc_key&corp_code&bgn_de&end_de&...  (현재는 수동/요약 우선 — 자동연결은 후속.)

  python -m main_mission.portfolio_os.policy_news --status
  python -m main_mission.portfolio_os.policy_news --add-policy --date 2026-06-01 \
      --sector 반도체 --title "정부 반도체 보조금 확대" --stance favorable --severity 0.6
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .store import db as store_db
from . import evidence_summary

VALID_STANCE = {"adverse", "favorable", "neutral"}

# 정책/규제 키워드 → stance *후보*(확정 아님 — 사람이 검토). 근거 없는 단정 금지.
_FAVORABLE_KW = ["보조금", "지원", "감세", "완화", "육성", "인센티브", "지원금", "부양",
                 "규제 완화", "허용", "확대"]
_ADVERSE_KW = ["규제", "제재", "과징금", "금지", "강화", "조사", "환경규제", "관세",
               "수출 통제", "징수", "인상", "제한"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# 분류 (키워드 *후보* — 사람 검토 전제, 가짜 단정 금지)
# ============================================================
def suggest_stance(title: str, *, summary: str = "") -> dict:
    """제목/요약 키워드로 stance *후보* 제안. 근거 없으면 neutral(정직).

    반환: {stance, confidence_hint, matched, is_suggestion=True} — 확정 아님.
    favorable/adverse 키워드가 둘 다 있으면 neutral(상충 → 사람 판단).
    """
    text = f"{title or ''} {summary or ''}".lower()
    fav = [k for k in _FAVORABLE_KW if k.lower() in text]
    adv = [k for k in _ADVERSE_KW if k.lower() in text]
    if fav and not adv:
        stance = "favorable"
    elif adv and not fav:
        stance = "adverse"
    else:
        stance = "neutral"   # 없음 또는 상충 — 사람 판단(가짜 단정 금지).
    return {"stance": stance, "matched_favorable": fav, "matched_adverse": adv,
            "is_suggestion": True,
            "note": "키워드 기반 stance *후보* — 확정 아님(사람/메모리가 최종 판단)."}


# ============================================================
# 정책/규제 이벤트 저장 (policy_events) — 멱등(같은 날짜·제목 중복 방지)
# ============================================================
def add_policy_event(event_date: str, title: str, *, sector: str | None = None,
                     stance: str = "neutral", severity: float | None = None,
                     source: str = "manual", conn=None) -> dict:
    """정책/규제 이벤트 1건 저장 → policy_events. 사실(제목/날짜/섹터/출처) + 판단(stance/severity).

    - stance 는 VALID_STANCE 중. severity 는 0~1(없으면 NULL — 강도 미판단 정직).
    - **판단(stance/severity)은 사람·메모리가 정한다.** 자동 분류는 suggest_stance 로 별도 제공.
    - 같은 (event_date, title) 이 이미 있으면 중복 적재 안 함(멱등 — 가짜 중복 0).
    """
    title = (title or "").strip()
    if not event_date or not title:
        return {"ok": False, "error": "event_date 와 title 은 필수."}
    if stance not in VALID_STANCE:
        return {"ok": False, "error": f"invalid stance {stance!r}; one of {sorted(VALID_STANCE)}"}
    sev = None
    if severity is not None:
        try:
            sev = max(0.0, min(1.0, float(severity)))
        except (ValueError, TypeError):
            return {"ok": False, "error": "severity 는 0~1 숫자여야 함."}
    own = conn is None
    conn = conn or store_db.connect()
    try:
        dup = conn.execute(
            "SELECT id FROM policy_events WHERE event_date=? AND title=?",
            (event_date, title)).fetchone()
        if dup:
            return {"ok": True, "id": dup["id"], "written": False,
                    "note": "동일 (날짜,제목) 이벤트 존재 — 중복 적재 안 함(멱등)."}
        cur = conn.execute(
            "INSERT INTO policy_events(event_date, sector, stance, severity, title, source, "
            "captured_at) VALUES(?,?,?,?,?,?,?)",
            (event_date, sector, stance, sev, title, source, _now()))
        conn.commit()
        return {"ok": True, "id": int(cur.lastrowid), "written": True,
                "note": "정책 이벤트 저장 — decline/axes/policy 가 읽음. 주문/정책 변경 0."}
    finally:
        if own:
            conn.close()


# ============================================================
# 공시/뉴스 요약 저장 (evidence_items via evidence_summary.add_evidence)
# ============================================================
def add_news_summary(title: str, *, source_type: str = "news", source: str | None = None,
                     source_date: str | None = None, url: str | None = None,
                     summary: str = "", confidence: float = 0.4,
                     related_ticker: str | None = None, related_etf: str | None = None,
                     related_theme: str | None = None, related_account: int | None = None,
                     facts: list | None = None, interpretation: list | None = None,
                     uncertainties: list | None = None, conn=None) -> dict:
    """공시/뉴스/정책 1건을 **요약 구조**로 evidence_items 에 저장(가짜 뉴스 0).

    구조: 사실(facts) vs 해석(interpretation) vs 불확실성(uncertainties) 구분.
      - 사실은 summary 본문 + (있으면) facts 로 보존.
      - 해석/영향은 evidence_summary 의 규칙기반 요약(positive/negative)으로 구조화 —
        근거 약하면 강한 결론 금지(watch_only)로 자동 약화(evidence_summary 게이트).
      - source_type: 'filing'(DART 공시) | 'news'(뉴스) | 'sector'(정책/규제 섹터이슈).
    confidence 기본 낮게(0.4) — 1차 출처 확인 전엔 강한 조언 금지(정직).
    """
    if source_type not in evidence_summary.VALID_SOURCE_TYPES:
        return {"ok": False, "error": f"invalid source_type {source_type!r}; "
                f"권장: filing|news|sector ({sorted(evidence_summary.VALID_SOURCE_TYPES)})"}
    title = (title or "").strip()
    if not title and not summary:
        return {"ok": False, "error": "title 또는 summary 필요(빈 요약 금지)."}
    # 사실(facts)은 summary 앞에 보존 — 사실/해석 분리(가짜 해석 주입 금지).
    body_parts = [p for p in ([title] + list(facts or []) + [summary]) if p]
    body = " | ".join(str(p) for p in body_parts)
    # 해석(interpretation)이 명시되면 positive_factors 로(검토용), uncertainties 그대로.
    eid = evidence_summary.add_evidence(
        source_type, source=source, source_date=source_date, url=url, summary=body,
        confidence=float(confidence), related_account=related_account,
        related_ticker=related_ticker, related_etf=related_etf, related_theme=related_theme,
        positive_factors=interpretation, uncertainties=uncertainties, conn=conn)
    return {"ok": True, "evidence_id": eid, "source_type": source_type,
            "note": "공시/뉴스 요약 저장(사실/해석/불확실성 구분). 근거 약하면 강한 조언 자동 약화. "
                    "주문/정책 변경 0 · 가짜 뉴스 0."}


# ============================================================
# 연동 상태 (정직 표기)
# ============================================================
def status(*, conn=None) -> dict:
    """정책/공시/뉴스 연동 상태 — 자동 피드 미연동(수동·요약 우선) 정직 표기 + 저장 현황."""
    own = conn is None
    conn = conn or store_db.connect()
    try:
        pe = conn.execute("SELECT COUNT(*) c FROM policy_events").fetchone()["c"]
        ev = conn.execute(
            "SELECT COUNT(*) c FROM evidence_items WHERE source_type IN "
            "('news','filing','sector')").fetchone()["c"]
    finally:
        if own:
            conn.close()
    return {
        "manual_input": "available",            # 수동 입력 + 요약 구조 동작.
        "dart_filings_feed": "not_connected",   # DART 공시목록 자동수집 미연동(후속).
        "news_feed": "not_connected",           # 뉴스 자동수집 미연동(공식/무료 우선, 후속).
        "policy_events_stored": int(pe),
        "policy_news_evidence_stored": int(ev),
        "data_connected": False,                # 자동 피드 미연동 — 정직(수동/요약은 동작).
        "note": "정책/공시/뉴스 자동 피드 미연동 — 수동입력 + 요약 구조만 동작(가짜 뉴스 0). "
                "공식/무료 피드(DART list.json 등) 연결 시 자동 적재로 확장. "
                "stance/severity 등 판단은 사람·메모리가 정함. 주문/정책 변경 0.",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="정책/공시/뉴스 요약 저장(공식/무료·수동 우선, 가짜 0)")
    ap.add_argument("--status", action="store_true", help="연동 상태 + 저장 현황")
    ap.add_argument("--add-policy", action="store_true", help="정책/규제 이벤트 저장(policy_events)")
    ap.add_argument("--add-news", action="store_true", help="공시/뉴스 요약 저장(evidence_items)")
    ap.add_argument("--suggest", metavar="TITLE", help="제목으로 stance 후보 제안(분류만)")
    ap.add_argument("--date", help="event_date / source_date 'YYYY-MM-DD'")
    ap.add_argument("--sector")
    ap.add_argument("--title")
    ap.add_argument("--stance", default="neutral")
    ap.add_argument("--severity", type=float)
    ap.add_argument("--summary", default="")
    ap.add_argument("--source", default="manual")
    ap.add_argument("--source-type", default="news", help="news|filing|sector")
    ap.add_argument("--ticker")
    ap.add_argument("--etf")
    ap.add_argument("--theme")
    args = ap.parse_args()
    try:
        if args.add_policy:
            out = add_policy_event(args.date, args.title or "", sector=args.sector,
                                   stance=args.stance, severity=args.severity,
                                   source=args.source)
        elif args.add_news:
            out = add_news_summary(args.title or "", source_type=args.source_type,
                                   source=args.source, source_date=args.date,
                                   summary=args.summary, related_ticker=args.ticker,
                                   related_etf=args.etf, related_theme=args.theme)
        elif args.suggest:
            out = suggest_stance(args.suggest, summary=args.summary)
        else:
            out = status()
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
