"""Evidence 요약 엔진 (자료 정리) — 재무/공시/뉴스/ETF구성/거시/수급을
   포트폴리오 판단용으로 *규칙기반*으로 정리한다.

목적(본질):
  사람은 모든 재무제표/기사/공시/리포트/ETF구성/거시지표를 꼼꼼히 못 본다.
  → 시스템이 대신 정리. 각 evidence = {무엇이 새로 나왔나, 관련 종목/ETF/섹터,
    긍정/부정/불확실, 단기/장기 영향, 내 포트폴리오 영향, 추가 확인 필요}.

불변 원칙:
  - **Anthropic API 미사용.** 분류·요약 구조화는 전부 키워드/룰 기반.
    문장 자체 생성은 규칙으로 만든 구조(positive/negative/uncertain)일 뿐 LLM 호출 0.
  - **근거 없는 강한 조언 금지.** confidence(=base·freshness 반영 eff)가 낮으면
    suggested_action 을 강하게 내지 않는다(watch_only 수준으로 약화).
  - **출처/날짜/freshness/confidence 필수.** stale(오래됨) 자동 표시.
  - **가짜 evidence 생성 금지.** 실제 DART/뉴스/재무 데이터는 외부 커넥터가 필요 →
    수동 입력 + ingestion 인터페이스(stub)로 프레임만 완성하고, 미연동을 정직 표기.

저장: 기존 `evidence_items` 테이블 (스키마 편집 금지). freshness/stale 은 evidence.py 재사용.

성장:
  반복해서 맞는 evidence 유형(source_type) 의 confidence 를 살짝 올리고(use/accept),
  무시·틀린 유형은 내린다. 기존 lessons/track_record 패턴을 참고하되 **새 API 0**,
  evidence_items 자체 누적 통계(accepted/ignored)만 사용한다.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .store import db as store_db
from . import evidence as _ev  # freshness decay / stale 판정 재사용

# evidence_items.source_type 허용값 (스키마 주석과 동일).
VALID_SOURCE_TYPES = {
    "financials", "filing", "news", "sector", "etf", "macro", "flow",
}

# accepted_or_ignored 허용값.
VALID_FEEDBACK = {"accepted", "ignored", "modified", "rejected_as_wrong"}

# stale 판정: 수집 freshness 기준 eff_confidence 가 이 값 미만이면 stale.
STALE_EFF_THRESHOLD = 0.25
# 강한 suggested_action 을 허용하는 eff_confidence 하한 (이하면 watch_only 로 약화).
STRONG_ACTION_MIN_CONF = 0.45

# ---------------------------------------------------------------------------
# 규칙기반 분류 사전 (키워드 → 입장). 한/영 혼용 키워드.
# 가짜 결론을 만들지 않도록, 매칭된 키워드만 근거로 남긴다.
# ---------------------------------------------------------------------------
_POSITIVE_KW = [
    "수주", "흑자", "최대 실적", "사상 최대", "수요 증가", "공급 부족", "증설",
    "수출 증가", "가격 상승", "상향", "목표가 상향", "배당 확대", "자사주",
    "수혜", "수요 강세", "호실적", "성장", "beat", "surge", "record high",
    "upgrade", "demand", "shortage",
]
_NEGATIVE_KW = [
    "적자", "감익", "감소", "하향", "목표가 하향", "리콜", "소송", "규제",
    "공급 과잉", "재고 증가", "수요 둔화", "가격 하락", "감산", "구조조정",
    "유상증자", "횡령", "분식", "어닝쇼크", "miss", "downgrade", "lawsuit",
    "oversupply", "slowdown", "recall",
]
_UNCERTAIN_KW = [
    "전망", "예상", "가능성", "검토", "추정", "불확실", "변동성", "관망",
    "혼조", "엇갈", "may", "could", "uncertain", "guidance", "outlook",
]
# 단기/장기 영향 신호 키워드.
_SHORT_TERM_KW = ["단기", "분기", "이번 주", "당일", "급등", "급락", "quarter", "this week"]
_LONG_TERM_KW = ["장기", "구조적", "중장기", "수년", "사이클", "전환", "structural", "long-term"]

# source_type 별 "확인 관점" — 자료 유형마다 사람이 놓치기 쉬운 체크포인트를 다르게 안내.
# (가짜 결론 생성 아님 — 어떤 관점으로 원문을 봐야 하는지 알려주는 후속확인 가이드)
_SOURCE_TYPE_LENS = {
    "financials": {
        "label": "재무제표",
        "checks": ["매출/영업이익 추세(YoY·QoQ)", "마진·현금흐름", "부채·재고 변화"],
    },
    "filing": {
        "label": "공시",
        "checks": ["공시 종류(유증/자사주/M&A/소송 등)", "지분 변동", "거래정지/관리종목 여부"],
    },
    "news": {
        "label": "뉴스/기사",
        "checks": ["1차 출처(기업 발표/공식)인지", "추측성·전망 여부", "이미 주가 반영 여부"],
    },
    "sector": {
        "label": "섹터/테마 이슈",
        "checks": ["섹터 전반 vs 개별사 영향", "수급/사이클 단계", "정책·규제 방향"],
    },
    "etf": {"label": "ETF 자료", "checks": ["구성/비중 변화", "유출입 흐름", "겹침 노출"]},
    "macro": {"label": "거시", "checks": ["금리/환율/유가 방향", "리스크온·오프", "자산군 영향"]},
    "flow": {"label": "수급", "checks": ["외인/기관 순매수 지속성", "프로그램 매매", "공매도 잔고"]},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _kw_hits(text: str, kws: list[str]) -> list[str]:
    if not text:
        return []
    low = text.lower()
    return [k for k in kws if k.lower() in low]


# ---------------------------------------------------------------------------
# 규칙기반 요약 (Anthropic 아님)
# ---------------------------------------------------------------------------
def summarize(evidence: dict) -> dict:
    """evidence(dict) → 구조화 요약.

    입력 키(있으면 사용): summary, source_type, source_date, base_confidence(or confidence),
      freshness_at(없으면 source_date/created_at), positive_factors, negative_factors,
      uncertainties (사전 제공 시 우선).

    반환: {긍정요인, 부정요인, 불확실성, 단기영향, 장기영향, portfolio_impact_hint,
           추가확인, eff_confidence, stale, conflicting, stance}
    모든 분류는 키워드 룰. 외부 호출 없음.
    """
    text = " ".join(str(evidence.get(k) or "") for k in ("summary",))
    base = evidence.get("base_confidence")
    if base is None:
        base = evidence.get("confidence")
    base = float(base) if base is not None else 0.5
    freshness_at = (evidence.get("freshness_at") or evidence.get("source_date")
                    or evidence.get("created_at"))

    # 명시 제공된 요인 우선, 없으면 키워드 추출 (가짜 생성 금지 — 매칭만).
    pos = _aslist(evidence.get("positive_factors")) or _kw_hits(text, _POSITIVE_KW)
    neg = _aslist(evidence.get("negative_factors")) or _kw_hits(text, _NEGATIVE_KW)
    unc = _aslist(evidence.get("uncertainties")) or _kw_hits(text, _UNCERTAIN_KW)

    short_sig = bool(_kw_hits(text, _SHORT_TERM_KW))
    long_sig = bool(_kw_hits(text, _LONG_TERM_KW))

    eff = _ev.decayed_confidence(base, freshness_at)
    stale = eff < STALE_EFF_THRESHOLD
    conflicting = bool(pos) and bool(neg)

    stance = _classify_stance(pos, neg, unc, conflicting)
    source_type = evidence.get("source_type")
    followups = _followups(pos, neg, unc, conflicting, eff)
    followups += _source_type_followups(source_type)
    return {
        "긍정요인": pos,
        "부정요인": neg,
        "불확실성": unc,
        "단기영향": _impact_word(pos, neg) if short_sig or not long_sig else "neutral",
        "장기영향": _impact_word(pos, neg) if long_sig else "uncertain",
        "portfolio_impact_hint": _impact_word(pos, neg),
        "추가확인": followups,
        "source_type": source_type,
        "source_lens": (_SOURCE_TYPE_LENS.get(source_type, {}).get("label") if source_type else None),
        "eff_confidence": eff,
        "base_confidence": round(base, 4),
        "stale": stale,
        "conflicting": conflicting,
        "stance": stance,
    }


def _source_type_followups(source_type: str | None) -> list[str]:
    """source_type 별 확인 관점을 후속확인 가이드로 제공(자료 유형마다 다르게)."""
    lens = _SOURCE_TYPE_LENS.get(source_type or "")
    if not lens:
        return []
    return [f"[{lens['label']}] 확인 관점: " + " · ".join(lens["checks"])]


def _aslist(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    s = str(v).strip()
    if not s:
        return []
    # JSON 배열 문자열이면 파싱.
    try:
        obj = json.loads(s)
        if isinstance(obj, list):
            return [str(x) for x in obj if str(x).strip()]
    except (ValueError, TypeError):
        pass
    return [s]


def _impact_word(pos: list, neg: list) -> str:
    if pos and not neg:
        return "positive"
    if neg and not pos:
        return "negative"
    if pos and neg:
        return "mixed"
    return "neutral"


def _classify_stance(pos, neg, unc, conflicting) -> str:
    """입장 태깅 (evidence.VALID_STANCES 와 호환). 매수/매도 확정 아님."""
    if conflicting:
        return "conflicting_evidence"
    if not pos and not neg:
        return "insufficient_evidence"
    if pos and not neg:
        return "long_support"
    if neg and not pos:
        return "risk_warning"
    return "watch_only"


def _followups(pos, neg, unc, conflicting, eff) -> list[str]:
    out: list[str] = []
    if conflicting:
        out.append("상충 자료: 긍정/부정 동시 — 추가 1차 자료로 검증 필요")
    if unc:
        out.append("불확실 표현 다수 — 확정 전 후속 공시/실적 확인")
    if eff < STRONG_ACTION_MIN_CONF:
        out.append("confidence 낮음(또는 오래됨) — 강한 액션 보류, 관망")
    if not pos and not neg:
        out.append("방향성 키워드 없음 — 원문 직접 확인 필요")
    return out


def _suggested_action(summary_out: dict) -> str:
    """confidence 게이트: 근거 약하면 강한 조언 금지(watch_only 로 약화)."""
    eff = summary_out["eff_confidence"]
    stance = summary_out["stance"]
    if eff < STRONG_ACTION_MIN_CONF or summary_out["conflicting"] or summary_out["stale"]:
        return "watch_only"  # 약한 권고: 관망/추가확인
    if stance == "long_support":
        return "consider_long_review"   # 비중 검토 후보(주문 아님)
    if stance == "risk_warning":
        return "consider_trim_or_hedge_review"  # 축소/헤지 검토 후보(주문 아님)
    return "watch_only"


# ---------------------------------------------------------------------------
# 표준 출력 형식 (브리프) — 사람이 매일 못 보는 자료를 한 장으로 정리.
# 출력형식: 자료 / 관련 종목·ETF / 긍정 / 부정 / 불확실 / 포트폴리오 영향 /
#           추가 확인 / confidence / freshness  (Track E 사양)
# ---------------------------------------------------------------------------
def brief(evidence: dict) -> dict:
    """단일 evidence(dict) → 표준 출력형식 브리프(규칙기반, 외부 호출 0).

    입력 키(있으면 사용): summary, source_type, source, source_date, url,
      confidence/base_confidence, related_ticker, related_etf, related_theme,
      positive_factors/negative_factors/uncertainties.

    근거 게이트: confidence 낮음/stale/상충이면 suggested_action 을 watch_only 로 약화.
    """
    s = summarize(evidence)
    action = _suggested_action(s)
    related = {
        "ticker": _norm(evidence.get("related_ticker")),
        "etf": _norm(evidence.get("related_etf")),
        "theme": (evidence.get("related_theme") or None),
    }
    return {
        "자료": {
            "source_type": s["source_type"],
            "유형": s["source_lens"],
            "출처": evidence.get("source"),
            "날짜": evidence.get("source_date"),
            "url": evidence.get("url"),
            "요약": evidence.get("summary") or "",
        },
        "관련종목ETF": {k: v for k, v in related.items() if v},
        "긍정요인": s["긍정요인"],
        "부정요인": s["부정요인"],
        "불확실성": s["불확실성"],
        "포트폴리오영향": {
            "방향": s["portfolio_impact_hint"],
            "단기": s["단기영향"],
            "장기": s["장기영향"],
        },
        "추가확인": s["추가확인"],
        "confidence": s["eff_confidence"],
        "base_confidence": s["base_confidence"],
        "freshness": s["eff_confidence"],   # decay 반영 유효 confidence = freshness 신호
        "stale": s["stale"],
        "conflicting": s["conflicting"],
        "stance": s["stance"],
        "suggested_action": action,         # 근거 게이트 통과분만 강한 검토 후보
        "data_source_status": data_source_status(),
    }


def briefs_by_source_type(account_index: int, *, limit: int = 50, conn=None) -> dict:
    """계좌 보유/관심에 연결된 evidence 를 source_type 별로 묶어 브리프 형식으로 정리.

    재무제표(financials)/공시(filing)/뉴스(news)/섹터(sector)/etf/macro/flow 그룹화.
    근거 게이트·stale·상충 처리는 brief()/evidence_for_account() 와 동일하게 유지.
    데이터 없으면 빈 그룹 + 정직한 data_source_status.
    """
    res = evidence_for_account(account_index, limit=limit, conn=conn)
    groups: dict[str, list[dict]] = {}
    for it in res["items"]:
        st = it.get("source_type") or "기타"
        groups.setdefault(st, []).append({
            "id": it["id"],
            "자료": {
                "유형": _SOURCE_TYPE_LENS.get(st, {}).get("label"),
                "출처": it["source"], "날짜": it["source_date"], "url": it["url"],
                "요약": it["summary"],
            },
            "관련종목ETF": {k: v for k, v in
                          {"ticker": it["related_ticker"], "etf": it["related_etf"],
                           "theme": it["related_theme"]}.items() if v},
            "긍정요인": it["positive_factors"],
            "부정요인": it["negative_factors"],
            "불확실성": it["uncertainties"],
            "포트폴리오영향": it["portfolio_impact"],
            "추가확인": _source_type_followups(st),
            "confidence": it["eff_confidence"],
            "freshness": it["eff_confidence"],
            "stale": it["stale"],
            # 근거 게이트: stale 이면 강한 액션 금지(watch_only).
            "suggested_action": ("watch_only" if it["stale"] else it["suggested_action"]),
        })
    return {
        "account": account_index,
        "by_source_type": groups,
        "holdings_tickers": res["holdings_tickers"],
        "universe_tickers": res["universe_tickers"],
        "conflicts": res["conflicts"],
        "stale_count": res["stale_count"],
        "data_source_status": res["data_source_status"],
        "note": "보유/관심 연결 evidence 를 자료유형별로 정리(읽기 전용). 가짜 생성 금지.",
    }


# ---------------------------------------------------------------------------
# 적재
# ---------------------------------------------------------------------------
def add_evidence(source_type: str, *, source: str | None = None,
                 source_date: str | None = None, url: str | None = None,
                 summary: str = "", confidence: float = 0.5,
                 related_account: int | None = None, related_ticker: str | None = None,
                 related_etf: str | None = None, related_theme: str | None = None,
                 positive_factors=None, negative_factors=None, uncertainties=None,
                 conn=None) -> int:
    """evidence 1건 적재 → id. 규칙기반 summarize 로 요인/영향/액션 채움.

    출처(source)·날짜(source_date)·freshness·confidence 필수 기록.
    suggested_action 은 confidence 게이트 통과분만 강하게(아니면 watch_only).
    """
    if source_type not in VALID_SOURCE_TYPES:
        raise ValueError(
            f"invalid source_type {source_type!r}; one of {sorted(VALID_SOURCE_TYPES)}")
    now = _now()
    raw = {
        "summary": summary,
        "source_type": source_type,
        "source_date": source_date,
        "confidence": float(confidence),
        # freshness 기준: source_date 가 있으면 그 날짜, 없으면 수집 시각.
        "freshness_at": source_date or now,
        "positive_factors": positive_factors,
        "negative_factors": negative_factors,
        "uncertainties": uncertainties,
    }
    s = summarize(raw)
    action = _suggested_action(s)

    own = conn is None
    conn = conn or store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO evidence_items("
            "source, source_type, source_date, url, freshness, stale, confidence, "
            "related_account, related_ticker, related_etf, related_theme, summary, "
            "positive_factors, negative_factors, uncertainties, portfolio_impact, "
            "suggested_action, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source, source_type, source_date, url,
                s["eff_confidence"], 1 if s["stale"] else 0, float(confidence),
                related_account, _norm(related_ticker), _norm(related_etf), _norm(related_theme),
                summary,
                json.dumps(s["긍정요인"], ensure_ascii=False),
                json.dumps(s["부정요인"], ensure_ascii=False),
                json.dumps(s["불확실성"], ensure_ascii=False),
                s["portfolio_impact_hint"],
                action, now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        if own:
            conn.close()


def _norm(t: str | None) -> str | None:
    return t.strip().upper() if isinstance(t, str) and t.strip() else None


# ---------------------------------------------------------------------------
# 회수 + 요약 뷰
# ---------------------------------------------------------------------------
def _row_to_view(r) -> dict:
    """evidence_items row → 표준 뷰(eff_confidence 재계산, stale 갱신)."""
    base = float(r["confidence"] or 0.0)
    freshness_at = r["source_date"] or r["created_at"]
    eff = _ev.decayed_confidence(base, freshness_at)
    return {
        "id": r["id"],
        "source": r["source"],
        "source_type": r["source_type"],
        "source_date": r["source_date"],
        "captured_at": r["created_at"],   # 수집 시각(EvidenceRecord.captured_at 매핑용)
        "url": r["url"],
        "related_account": r["related_account"],
        "related_ticker": r["related_ticker"],
        "related_etf": r["related_etf"],
        "related_theme": r["related_theme"],
        "summary": r["summary"],
        "positive_factors": _aslist(r["positive_factors"]),
        "negative_factors": _aslist(r["negative_factors"]),
        "uncertainties": _aslist(r["uncertainties"]),
        "portfolio_impact": r["portfolio_impact"],
        "suggested_action": r["suggested_action"],
        "accepted_or_ignored": r["accepted_or_ignored"],
        "base_confidence": round(base, 4),
        "eff_confidence": eff,
        "stale": eff < STALE_EFF_THRESHOLD,
    }


def evidence_for_account(account_index: int, *, limit: int = 50, conn=None) -> dict:
    """내 보유(holdings)/관심(universe_instruments) 관련 evidence 만 추려 반환.

    연결 키: related_ticker/related_etf 가 보유/유니버스 ticker 와 일치, 또는
             related_theme 가 유니버스의 (간이) 테마와 일치.
    related_account 가 다른 계좌면 격리(제외), None(계좌무관)은 공통 포함.

    반환:
      {account, holdings_tickers, universe_tickers, items[], conflicts[], stale_count,
       data_source_status}
    """
    own = conn is None
    conn = conn or store_db.connect()
    try:
        # 최신 스냅샷의 보유 ticker.
        snap = conn.execute(
            "SELECT id FROM account_snapshots WHERE account_index=? "
            "ORDER BY captured_at DESC, id DESC LIMIT 1",
            (account_index,),
        ).fetchone()
        holdings: set[str] = set()
        if snap:
            for hr in conn.execute(
                "SELECT ticker FROM holdings WHERE snapshot_id=?", (snap["id"],)
            ).fetchall():
                if hr["ticker"]:
                    holdings.add(_norm(hr["ticker"]))
        # 관심(유니버스) ticker.
        universe: set[str] = set()
        for ur in conn.execute(
            "SELECT ticker FROM universe_instruments WHERE account_index=? AND is_active=1",
            (account_index,),
        ).fetchall():
            if ur["ticker"]:
                universe.add(_norm(ur["ticker"]))
        my_tickers = holdings | universe

        rows = conn.execute(
            "SELECT * FROM evidence_items WHERE related_account IS NULL OR related_account=? "
            "ORDER BY created_at DESC LIMIT 2000",
            (account_index,),
        ).fetchall()

        items: list[dict] = []
        for r in rows:
            tk = r["related_ticker"]
            etf = r["related_etf"]
            # 보유/관심과 연결되는가? ticker/etf 가 내 종목집합과 교집합.
            linked = (tk and tk in my_tickers) or (etf and etf in my_tickers)
            if not linked:
                continue
            items.append(_row_to_view(r))

        items.sort(key=lambda d: d["eff_confidence"], reverse=True)
        items = items[:limit]
        conflicts = _detect_conflicts(items)
        stale_count = sum(1 for it in items if it["stale"])
        return {
            "account": account_index,
            "holdings_tickers": sorted(holdings),
            "universe_tickers": sorted(universe),
            "items": items,
            "conflicts": conflicts,
            "stale_count": stale_count,
            "data_source_status": data_source_status(),
        }
    finally:
        if own:
            conn.close()


def _detect_conflicts(items: list[dict]) -> list[dict]:
    """같은 종목/ETF 에 대해 긍정(long_support 류)·부정(risk_warning 류)이 공존 → 상충 표시."""
    by_key: dict[str, dict] = {}
    for it in items:
        key = it["related_ticker"] or it["related_etf"]
        if not key:
            continue
        b = by_key.setdefault(key, {"pos": [], "neg": []})
        # 액션/요인으로 방향 추정 (가짜 단정 금지 — 요인 기반).
        if it["positive_factors"] and not it["negative_factors"]:
            b["pos"].append(it["id"])
        elif it["negative_factors"] and not it["positive_factors"]:
            b["neg"].append(it["id"])
    out = []
    for key, b in by_key.items():
        if b["pos"] and b["neg"]:
            out.append({"key": key, "positive_evidence_ids": b["pos"],
                        "negative_evidence_ids": b["neg"],
                        "note": "상충: 같은 대상에 긍정·부정 자료 공존 → 강한 조언 보류"})
    return out


# ---------------------------------------------------------------------------
# 성장 — 피드백 누적 → source_type 신뢰도 보정
# ---------------------------------------------------------------------------
def record_feedback(evidence_id: int, feedback: str, *, note: str | None = None,
                    conn=None) -> dict:
    """사용자 반응 기록 (accepted|ignored|modified|rejected_as_wrong).

    반복해서 맞는(accepted) source_type 은 신뢰 ↑, 무시/틀린 것은 ↓ (다음 적재에 반영).
    새 API 없이 evidence_items 누적 통계만 사용.
    """
    if feedback not in VALID_FEEDBACK:
        raise ValueError(f"invalid feedback {feedback!r}; one of {sorted(VALID_FEEDBACK)}")
    own = conn is None
    conn = conn or store_db.connect()
    try:
        conn.execute(
            "UPDATE evidence_items SET accepted_or_ignored=?, user_feedback=COALESCE(?,user_feedback) "
            "WHERE id=?",
            (feedback, note, evidence_id),
        )
        conn.commit()
        return {"ok": True, "evidence_id": evidence_id, "feedback": feedback}
    finally:
        if own:
            conn.close()


def source_type_trust(source_type: str, *, conn=None) -> dict:
    """source_type 별 신뢰 보정값. accepted 비율 기반 multiplier (성장).

    multiplier ∈ [0.6, 1.3]. 샘플 적으면 1.0(중립). 새 API 0 — evidence_items 통계만.
    """
    own = conn is None
    conn = conn or store_db.connect()
    try:
        rows = conn.execute(
            "SELECT accepted_or_ignored AS f FROM evidence_items "
            "WHERE source_type=? AND accepted_or_ignored IS NOT NULL",
            (source_type,),
        ).fetchall()
        n = len(rows)
        if n < 3:
            return {"source_type": source_type, "samples": n, "multiplier": 1.0,
                    "note": "샘플 부족 — 중립"}
        good = sum(1 for r in rows if r["f"] in ("accepted", "modified"))
        bad = sum(1 for r in rows if r["f"] in ("ignored", "rejected_as_wrong"))
        ratio = good / max(1, good + bad)
        # 0.0→0.6, 0.5→~1.0, 1.0→1.3 선형 매핑(부드럽게).
        mult = round(0.6 + 0.7 * ratio, 3)
        return {"source_type": source_type, "samples": n, "good": good, "bad": bad,
                "accept_ratio": round(ratio, 3), "multiplier": mult}
    finally:
        if own:
            conn.close()


def effective_confidence(source_type: str, base: float, freshness_at: str | None, *,
                         conn=None) -> float:
    """freshness decay + source_type 신뢰 보정(성장)을 합친 유효 confidence.

    근거 없는 강한 조언 게이트는 호출측이 STRONG_ACTION_MIN_CONF 와 비교해 사용.
    """
    decayed = _ev.decayed_confidence(base, freshness_at)
    mult = source_type_trust(source_type, conn=conn)["multiplier"]
    return round(min(1.0, decayed * mult), 4)


# ---------------------------------------------------------------------------
# 데이터 소스 정직 표기 + ingestion stub
# ---------------------------------------------------------------------------
def data_source_status() -> dict:
    """실 자료 소스 연동 상태(정직 표기). 가짜 evidence 생성 금지."""
    return {
        "manual_input": "available",          # 수동 입력은 동작
        "dart_filings": "not_connected",      # DART 공시 API 미연동
        "news_api": "not_connected",          # 뉴스 API 미연동
        "financials_feed": "not_connected",   # 재무제표 피드 미연동
        "macro_ecos": "not_connected",        # 한국은행 ECOS 등 거시 미연동
        "flow_data": "not_connected",         # 수급(외인/기관) 미연동
        "note": "실 자료 소스 미연동(DART/뉴스/재무/거시/수급 API). "
                "현재는 수동 입력 + ingestion stub 프레임만 동작. 자동 적재 금지.",
    }


def ingest_stub(source_type: str, payload: dict, *, conn=None) -> dict:
    """외부 커넥터 자리(인터페이스). **실 연동 전까지 자동 적재하지 않는다.**

    가짜 evidence 를 만들지 않기 위해, 커넥터가 없는 source_type 은 거부하고
    "수동 입력만 허용" 을 정직히 반환한다. 실제 커넥터가 생기면 여기서 add_evidence 호출.
    """
    status = data_source_status()
    key = {
        "filing": "dart_filings", "news": "news_api", "financials": "financials_feed",
        "macro": "macro_ecos", "flow": "flow_data",
    }.get(source_type)
    connected = key is not None and status.get(key) == "available"
    if not connected:
        return {
            "ok": False, "ingested": 0, "source_type": source_type,
            "reason": "connector_not_connected",
            "note": "실 자료 커넥터 미연동 — 자동 적재 거부(가짜 evidence 방지). "
                    "수동 add_evidence() 로 입력하세요.",
        }
    # 커넥터가 연결된 경우에만 도달 (현재 None) — 방어적 분기.
    eid = add_evidence(source_type, **payload, conn=conn)  # pragma: no cover
    return {"ok": True, "ingested": 1, "evidence_id": eid}  # pragma: no cover
