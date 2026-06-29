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


# ---------------------------------------------------------------------------
# CLI — 웹 '개별주 자동 추천' 배선용 (상위 N 정직 추천, 자동 적용/주문 0)
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="개별주 상위 N 자동 추천(정직 — 가짜 티커/점수 금지, 자동적용 0)")
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--n", type=int, default=10, help="추천 개수(1~30)")
    ap.add_argument("--extra", default="", help="콤마구분 추가 후보 티커(테마/CEO 제공)")
    a = ap.parse_args(argv)
    extra = [t.strip() for t in a.extra.split(",") if t.strip()]
    out = recommend_stocks(a.account, n=max(1, min(30, a.n)), extra_tickers=extra or None)
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
