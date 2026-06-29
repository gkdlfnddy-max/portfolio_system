"""개별주 추천 엔진 — 후보 집합을 평가해 상위 N개를 CandidateEvaluation 으로 반환.

CEO 요청("개별주 10개 알아서 추천")의 **메커니즘**. 정직 원칙:
  - 가짜 티커/가짜 점수 금지 — 후보는 (a) 종목 시드 bucket, (b) 계좌 유니버스,
    (c) 호출자/테마가 준 리스트에서만. 각 티커는 DART corp_map 으로 **실재 검증**.
  - 테마/섹터 분류 데이터가 없으면 시스템이 임의로 '로봇 10개'를 지어내지 않는다(정직).
    → 충분한 후보가 없으면 가진 만큼만 + universe_note 로 부족을 명시.
  - 추천 강도는 confidence 기반(데이터 얇으면 watch). 자동 적용/주문 0.
"""
from __future__ import annotations

from .candidate import candidate_evaluation, recommendation_strength
from .store import db as store_db

# 현재 시드에 개별주가 포함된 bucket(반도체=ETF+개별주 혼합). 테마 시드 늘면 여기 확장.
_STOCK_BEARING_BUCKETS = ("semiconductor",)


def _verify_ticker(ticker: str) -> bool:
    """DART corp_map 으로 실재 상장 종목인지 검증(가짜 티커 차단)."""
    try:
        from . import financials_connect as fc
        cc = fc.resolve_corp_code(ticker)
        return bool(cc)
    except Exception:  # noqa: BLE001 — 미연동/미존재면 검증 실패로 간주
        return False


def _eval_extra(account_index: int, ticker: str, conn) -> dict | None:
    """호출자/테마가 준 티커 1건을 정직 평가 → CandidateEvaluation. 미검증이면 None."""
    if not _verify_ticker(ticker):
        return None
    from . import price_history, security_selection as ss
    bars = price_history.load_history(ticker)
    price_ok = bool(bars)
    try:
        qf = ss.quality_filter(ticker, conn=conn)
    except Exception:  # noqa: BLE001
        qf = {"passed": None}
    passed = qf.get("passed")
    # 정직 confidence: 가격+우량주필터 통과 시만 상승, 데이터 얇으면 낮음.
    conf = 0.0
    if price_ok:
        conf += 0.2
    if passed is True:
        conf += 0.3
    available = price_ok or passed is not None
    return candidate_evaluation(
        "stock", ticker, display_name=ticker, bucket="individual",
        data_quality={"available": available,
                      "level": "connected" if (price_ok and passed is True) else
                               ("partial" if available else "unavailable"),
                      "price": price_ok, "quality_filter": passed},
        confidence=conf,
        risk_summary={"quality_filter": qf},
        reason_to_include=("가격/재무 신호 일부 확인" if available else ""),
        reason_to_exclude=("" if available else "데이터 미연동 — 판단 보류(정직)"),
    )


def recommend_stocks(account_index: int, *, n: int = 10, extra_tickers=None, conn=None) -> dict:
    """개별주 상위 N 추천(정직). 후보 = 종목 bucket + 계좌 유니버스 + extra_tickers.

    반환: {ok, requested, returned, candidates[CandidateEvaluation], universe_note,
           requires_user_approval=True, auto_order_created=False}
    """
    from . import security_selection as ss
    by_id: dict[str, dict] = {}

    # (a) 종목 시드 bucket 의 normalized 중 candidate_type=='stock'
    for bucket in _STOCK_BEARING_BUCKETS:
        try:
            cl = ss.classify_bucket(account_index, bucket, conn=conn)
        except Exception:  # noqa: BLE001
            continue
        if cl.get("ok"):
            for c in cl.get("normalized", []):
                if c.get("candidate_type") == "stock" and c.get("candidate_id"):
                    by_id[c["candidate_id"]] = c

    # (c) 호출자/테마 제공 티커(실재 검증 후)
    for tk in (extra_tickers or []):
        if tk in by_id:
            continue
        ev = _eval_extra(account_index, tk, conn)
        if ev is not None:
            by_id[tk] = ev

    # 랭킹: confidence 내림차순(동률이면 강도). 자동적용 아님.
    ranked = sorted(by_id.values(),
                    key=lambda c: (float(c.get("confidence") or 0.0),
                                   (c.get("recommendation_strength") or {}).get("level") == "moderate"),
                    reverse=True)
    top = ranked[:n]
    short = len(top) < n
    note = (f"후보 {len(by_id)}개 — {n}개 요청에 부족. 개별주 시드/계좌 유니버스가 적고 "
            "테마·섹터 분류 데이터가 없어 시스템이 임의로 채우지 않습니다(정직). "
            "테마별 종목 리스트를 주시거나 섹터 분류를 적재하면 N개까지 확장됩니다."
            if short else f"{n}개 추천(요청 충족).")
    return {
        "ok": True, "account_index": account_index, "requested": n,
        "returned": len(top), "candidates": top,
        "universe_note": note,
        "requires_user_approval": True, "auto_order_created": False,
    }


# ===========================================================================
# 2계층 결합 추천 — 공통 후보(instrument_master 테마/섹터) × 계좌 성향(account lens).
#   CEO 지시: 후보군은 공통 DB 에서, 필터/정렬은 계좌별로. "좋은 종목" 고정이 아니라
#   "이 계좌에 적합한 종목/ETF". draft 저장만 — 승인 전 주문 아님.
# ===========================================================================
def _account_lens(account_index: int, conn) -> dict:
    """계좌 성향 렌즈 — 목적(goal)·위험성향(risk)·제외종목(excludes). 미설정은 None(정직)."""
    goal = None
    try:
        from . import account_memory as am
        ctx = am.account_context(account_index)
        o = (ctx.get("objective") or {}).get("objective") or {}
        goal = o.get("investment_goal")
    except Exception:  # noqa: BLE001
        pass
    risk = None
    try:
        r = conn.execute("SELECT risk_tolerance FROM investor_profile WHERE account_index=?",
                         (account_index,)).fetchone()
        risk = r["risk_tolerance"] if r else None
    except Exception:  # noqa: BLE001
        pass
    excludes = set()
    try:
        for row in conn.execute(
                "SELECT ticker FROM universe_instruments WHERE account_index=? AND is_active=0",
                (account_index,)).fetchall():
            excludes.add(row["ticker"])
    except Exception:  # noqa: BLE001
        pass
    return {"goal": goal, "risk": risk, "excludes": excludes}


def _fit_to_account(row: dict, lens: dict) -> dict:
    """공통 후보 1건 → 이 계좌 적합도(fit_to_account). goal/risk 로 개별주↔ETF 차등.

    같은 종목이라도 계좌 목적/성향에 따라 점수·사유가 달라진다(복붙 금지).
    """
    from . import asset_for_account as afa
    is_etf = bool(row.get("is_etf"))
    is_hedge = bool(row.get("is_inverse")) or bool(row.get("is_leveraged"))
    goal, risk = lens.get("goal"), lens.get("risk")
    score = 0.5
    reasons: list[str] = []

    disp = afa._GOAL_DISPOSITION.get(goal or "")
    if disp is not None:
        direct_ok = disp.get("direct_equity_ok", True)
        if is_etf:
            score += 0.25 if not direct_ok else 0.10
            reasons.append(f"목적={goal}: ETF 적합" if not direct_ok else f"목적={goal}: ETF 무난")
        else:
            if direct_ok:
                score += 0.25
                reasons.append(f"목적={goal}: 개별주 직접편입 적극")
            else:
                score -= 0.20
                reasons.append(f"목적={goal}: 개별주 직접편입 후순위(ETF/방어 우선)")
    else:
        reasons.append("계좌 목적 미설정 — 중립(데이터 기준)")

    if risk == "aggressive":
        score += (0.08 if not is_etf else 0.0)
        reasons.append("공격 성향")
    elif risk == "defensive":
        score += (0.10 if is_etf else -0.10)
        reasons.append("방어 성향: ETF 가점·개별주 감점")

    if is_hedge and (goal in ("loss_reduction", "cash_preservation", "volatility_reduction") or risk == "defensive"):
        score += 0.10
        reasons.append("헤지/인버스: 방어 목적에 한해 가점")
    elif is_hedge:
        score -= 0.10
        reasons.append("헤지/인버스: 공격/일반 목적엔 감점")

    score = max(0.0, min(1.0, round(score, 3)))
    return {"score": score, "goal": goal, "risk": risk,
            "kind": ("etf" if is_etf else "stock"),
            "reason": " · ".join(reasons)}


def recommend(account_index: int, *, theme: str | None = None, sector: str | None = None,
              kind: str = "all", n: int = 10, conn=None) -> dict:
    """공통 후보(instrument_master) × 계좌 렌즈 → 계좌 맞춤 추천(draft). 주문 0.

    theme/sector 로 후보군을 공통 DB 에서 가져오고, 계좌 제외종목을 빼고, fit_to_account 로 정렬.
    kind: stock(개별주)|etf|all.
    """
    from . import instrument_master as im
    own = conn is None
    conn = conn or store_db.connect()
    try:
        if theme:
            rows = im.by_theme(theme, kind=kind, conn=conn)
            req_kind, req_key = "theme", theme
        elif sector:
            rows = im.by_sector(sector, kind=kind, conn=conn)
            req_kind, req_key = "sector", sector
        else:
            # 테마/섹터 미지정 — 종목 bearing bucket 의 stock 후보(기존 경로 재사용).
            base = recommend_stocks(account_index, n=max(n, 20), conn=conn)
            rows = [{"ticker": c.get("candidate_id"), "name": c.get("display_name"),
                     "market": None, "asset_class": "stock", "is_etf": 0,
                     "is_inverse": 0, "is_leveraged": 0} for c in base.get("candidates", [])]
            req_kind, req_key = "individual", None

        lens = _account_lens(account_index, conn)
        cands = []
        for r in rows:
            tk = r.get("ticker")
            if not tk or tk in lens["excludes"]:
                continue  # 계좌 제외종목 필터(성향 반영)
            ev = _eval_extra(account_index, tk, conn) or candidate_evaluation(
                "etf" if r.get("is_etf") else "stock", tk, display_name=r.get("name") or tk,
                bucket="individual", confidence=0.0,
                data_quality={"available": False, "level": "unavailable"})
            ev["display_name"] = r.get("name") or ev.get("display_name") or tk
            ev["asset_class"] = r.get("asset_class")
            ev["candidate_type"] = "etf" if r.get("is_etf") else "stock"
            ev["fit_to_account"] = _fit_to_account(r, lens)
            cands.append(ev)

        # 정렬: 계좌 적합도(fit) → confidence. "이 계좌에 적합한" 순서(복붙 아님).
        cands.sort(key=lambda c: (float(c["fit_to_account"]["score"]),
                                  float(c.get("confidence") or 0.0)), reverse=True)
        top = cands[:n]
        note = (f"공통 후보 {len(rows)}개 → 계좌 제외/성향 반영 후 {len(top)}개 추천. "
                + ("계좌 목적/성향 미설정 — 데이터 기준 중립 정렬(설정 시 맞춤도↑)."
                   if not (lens["goal"] or lens["risk"]) else
                   f"계좌 목적={lens['goal']}/성향={lens['risk']} 반영."))
        return {"ok": True, "account_index": account_index, "request_kind": req_kind,
                "request_key": req_key, "kind": kind, "returned": len(top),
                "candidates": top, "universe_note": note,
                "lens": {"goal": lens["goal"], "risk": lens["risk"],
                         "excluded_count": len(lens["excludes"])},
                "requires_user_approval": True, "auto_order_created": False}
    finally:
        if own:
            conn.close()


def save_draft(account_index: int, *, request_kind: str, request_key: str | None,
               kind: str, candidates: list, note: str | None = None, conn=None) -> dict:
    """추천 결과를 계좌별 draft 로 저장(append). policy/주문 미반영 — draft 전용."""
    import json
    own = conn is None
    conn = conn or store_db.connect()
    try:
        conn.execute("UPDATE account_reco_draft SET status='superseded' "
                     "WHERE account_index=? AND request_key IS ? AND status='draft'",
                     (account_index, request_key))
        cur = conn.execute(
            "INSERT INTO account_reco_draft(account_index, request_kind, request_key, kind_filter, "
            "candidates, note, status) VALUES(?,?,?,?,?,?,'draft')",
            (account_index, request_kind, request_key, kind,
             json.dumps(candidates, ensure_ascii=False), note))
        conn.commit()
        return {"ok": True, "draft_id": cur.lastrowid, "saved": len(candidates)}
    finally:
        if own:
            conn.close()


def record_feedback(account_index: int, *, ticker: str, action: str, reco_draft_id: int | None = None,
                    request_key: str | None = None, note: str | None = None, conn=None) -> dict:
    """CEO 피드백(선택/삭제/수정/무시) 기록 — 계좌별 학습 입력. 주문 아님."""
    if action not in ("selected", "removed", "modified", "ignored"):
        return {"ok": False, "error": f"action 은 selected|removed|modified|ignored: {action!r}"}
    own = conn is None
    conn = conn or store_db.connect()
    try:
        conn.execute(
            "INSERT INTO account_reco_feedback(account_index, ticker, action, reco_draft_id, "
            "request_key, note) VALUES(?,?,?,?,?,?)",
            (account_index, ticker, action, reco_draft_id, request_key, note))
        conn.commit()
        return {"ok": True, "account_index": account_index, "ticker": ticker, "action": action}
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# CLI — 웹 '개별주 자동 추천' 배선용 (상위 N 정직 추천, 자동 적용/주문 0)
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="종목/ETF 추천(정직 — 가짜 티커/점수 금지, 자동적용 0)")
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--n", type=int, default=10, help="추천 개수(1~30)")
    ap.add_argument("--extra", default="", help="콤마구분 추가 후보 티커(테마/CEO 제공)")
    # 2계층 결합(공통 후보 × 계좌 성향) — 테마/섹터 지정 시 instrument_master 후보군 사용.
    ap.add_argument("--theme", help="테마 후보(공통 DB) → 계좌 맞춤 추천")
    ap.add_argument("--sector", help="섹터 후보(공통 DB) → 계좌 맞춤 추천")
    ap.add_argument("--kind", default="all", choices=["stock", "etf", "all"], help="개별주|ETF|전체")
    ap.add_argument("--save-draft", action="store_true", help="추천 결과를 계좌 draft 로 저장")
    # 피드백 기록(선택/삭제/수정/무시)
    ap.add_argument("--feedback", help="action: selected|removed|modified|ignored")
    ap.add_argument("--ticker", help="--feedback 대상 티커")
    ap.add_argument("--draft-id", type=int, default=None)
    a = ap.parse_args(argv)

    if a.feedback:
        if not a.ticker:
            sys.stdout.write(json.dumps({"ok": False, "error": "--feedback 에는 --ticker 필요"}, ensure_ascii=False))
            return 0
        out = record_feedback(a.account, ticker=a.ticker, action=a.feedback,
                              reco_draft_id=a.draft_id, request_key=(a.theme or a.sector))
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        return 0

    n = max(1, min(30, a.n))
    if a.theme or a.sector:
        out = recommend(a.account, theme=a.theme, sector=a.sector, kind=a.kind, n=n)
        if a.save_draft and out.get("ok"):
            d = save_draft(a.account, request_kind=out["request_kind"], request_key=out["request_key"],
                           kind=a.kind, candidates=out["candidates"], note=out.get("universe_note"))
            out["draft_id"] = d.get("draft_id")
    else:
        extra = [t.strip() for t in a.extra.split(",") if t.strip()]
        out = recommend_stocks(a.account, n=n, extra_tickers=extra or None)
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
