"""ETF 분석 — 구성·상위비중·섹터/국가 노출·**보유 ETF 간 중복(겹침)**.

개별주와 다르게 ETF 는 *구성·비중·섹터/국가·중복보유*가 핵심이다.
예: 반도체 ETF + AI ETF 를 함께 보유하면 NVIDIA/TSMC/Samsung 이 양쪽에 겹쳐
    실제 단일 종목 노출이 의도보다 커질 수 있다 → 겹침(overlap)을 계산해 드러낸다.

핵심 규칙(불변):
  - **분석/조회만.** 주문·정책 자동변경 없음. 겹침이 크면 '집중 위험'으로 표기(후보는 portfolio_impact 가 만든다).
  - 데이터(etf_constituents) 없으면 **정직하게 '미연동'** 으로 표기 — 가짜 구성/비중 만들지 않음.
  - 지능 = 규칙 계산 + Claude+메모리. **Anthropic API 미사용.**

읽는 것(읽기 전용): etf_constituents · universe_instruments · holdings.

  python -m main_mission.portfolio_os.etf_analysis --etf TIGER반도체
  python -m main_mission.portfolio_os.etf_analysis --account 1   # 계좌 보유/관심 ETF 겹침
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .store import db as store_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _latest_as_of(conn, etf_ticker: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(as_of) AS m FROM etf_constituents WHERE etf_ticker=?", (etf_ticker,)).fetchone()
    return row["m"] if row and row["m"] else None


def load_constituents(etf_ticker: str, conn=None) -> list[dict]:
    """ETF 의 최신 기준일(as_of) 구성종목. as_of 가 NULL 만 있으면 그것도 포함."""
    own = conn is None
    conn = conn or store_db.connect()
    try:
        as_of = _latest_as_of(conn, etf_ticker)
        if as_of is not None:
            rows = conn.execute(
                "SELECT constituent_ticker, constituent_name, weight_pct, sector, country, as_of "
                "FROM etf_constituents WHERE etf_ticker=? AND as_of=? ORDER BY weight_pct DESC",
                (etf_ticker, as_of)).fetchall()
        else:
            rows = conn.execute(
                "SELECT constituent_ticker, constituent_name, weight_pct, sector, country, as_of "
                "FROM etf_constituents WHERE etf_ticker=? ORDER BY weight_pct DESC",
                (etf_ticker,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def _exposure_by(constituents: list[dict], key: str) -> list[dict]:
    """key(sector|country)별 비중 합계."""
    agg: dict[str, float] = {}
    for c in constituents:
        k = c.get(key) or "미분류"
        agg[k] = agg.get(k, 0.0) + (c.get("weight_pct") or 0.0)
    out = [{key: k, "weight_pct": round(v, 2)} for k, v in agg.items()]
    out.sort(key=lambda d: d["weight_pct"], reverse=True)
    return out


def analyze_etf(etf_ticker: str, *, top_n: int = 10, conn=None) -> dict:
    """단일 ETF 구성 분석 — 상위비중·섹터/국가 노출. 데이터 없으면 정직 미연동."""
    cons = load_constituents(etf_ticker, conn=conn)
    if not cons:
        return {"ok": True, "etf_ticker": etf_ticker, "data_connected": False,
                "constituent_count": 0, "top_holdings": [], "sector_exposure": [],
                "country_exposure": [],
                "note": "구성종목 데이터 미연동(etf_constituents 없음) — 정직하게 분석 불가."}
    total_w = round(sum(c.get("weight_pct") or 0.0 for c in cons), 2)
    return {
        "ok": True, "etf_ticker": etf_ticker, "data_connected": True,
        "as_of": cons[0].get("as_of"),
        "constituent_count": len(cons),
        "total_weight_pct": total_w,
        "top_holdings": [{"ticker": c["constituent_ticker"], "name": c.get("constituent_name"),
                          "weight_pct": c.get("weight_pct"), "sector": c.get("sector"),
                          "country": c.get("country")} for c in cons[:top_n]],
        "sector_exposure": _exposure_by(cons, "sector"),
        "country_exposure": _exposure_by(cons, "country"),
        "note": "ETF 구성 분석(읽기 전용) — 주문/정책 영향 없음.",
    }


def overlap(etf_a: str, etf_b: str, conn=None) -> dict:
    """두 ETF 간 구성 겹침(공통 종목) + 겹친 비중. 반도체ETF+AI ETF → NVIDIA/TSMC/Samsung."""
    own = conn is None
    conn = conn or store_db.connect()
    try:
        a = {c["constituent_ticker"]: c for c in load_constituents(etf_a, conn=conn)}
        b = {c["constituent_ticker"]: c for c in load_constituents(etf_b, conn=conn)}
    finally:
        if own:
            conn.close()
    if not a or not b:
        return {"ok": True, "etf_a": etf_a, "etf_b": etf_b, "data_connected": False,
                "shared": [], "shared_count": 0,
                "note": "한쪽 이상 구성 데이터 미연동 — 겹침 계산 불가(정직)."}
    shared_keys = sorted(set(a) & set(b),
                         key=lambda k: ((a[k].get("weight_pct") or 0) + (b[k].get("weight_pct") or 0)),
                         reverse=True)
    shared = []
    for k in shared_keys:
        wa = a[k].get("weight_pct") or 0.0
        wb = b[k].get("weight_pct") or 0.0
        shared.append({"ticker": k, "name": a[k].get("constituent_name") or b[k].get("constituent_name"),
                       "weight_in_a": round(wa, 2), "weight_in_b": round(wb, 2),
                       # 겹침 비중 = 양쪽에서 더 작은 비중(최소 공통 노출, Jaccard-by-weight 의미).
                       "min_overlap_weight": round(min(wa, wb), 2)})
    overlap_w = round(sum(s["min_overlap_weight"] for s in shared), 2)
    return {
        "ok": True, "etf_a": etf_a, "etf_b": etf_b, "data_connected": True,
        "shared_count": len(shared),
        "shared": shared,
        "overlap_weight_pct": overlap_w,   # 두 ETF 가 공유하는 비중(집중도 신호)
        "concentration_flag": overlap_w >= 20.0,  # 겹침 20%+ = 집중 위험 표기(후보는 별도)
        "note": ("ETF 겹침(중복보유) 분석 — 의도보다 단일 종목 노출이 커질 수 있음(읽기 전용). "
                 "겹침이 크면 집중 위험으로 표기(주문/정책 변경 아님)."),
    }


def candidate_overlap_with_holdings(candidate_etf: str, holding_etfs: list[str],
                                    conn=None) -> dict:
    """후보 ETF 가 **기존 보유 ETF 집합**과 갖는 중복노출(겹침) 강화 분석.

    단순 1:1 overlap 을 넘어, 후보의 각 구성종목이 *보유 ETF 들 전체*에 걸쳐 얼마나
    겹치는지(공통종목 + 합산 간접노출)를 본다. 예: 후보가 NVDA 10% 인데 보유 ETF A 에
    NVDA 8%, B 에 6% 면 → 그 단일 종목 노출이 의도보다 누적돼 커진다.

    합산 간접노출 = 후보 구성종목 중 보유 ETF 에도 들어있는 종목의 *후보 내 비중* 합.
    종목별로는 (후보 비중) + (보유 ETF 들 내 비중 합)을 함께 보여 누적 집중을 드러낸다.
    20%+ 면 concentration_flag. 데이터 미연동이면 정직하게 data_connected:false (가짜 0).
    """
    own = conn is None
    conn = conn or store_db.connect()
    try:
        cand = {(_norm(c["constituent_ticker"])): c
                for c in load_constituents(candidate_etf, conn=conn)}
        # 보유 ETF 들의 종목별 비중 누적(ticker → 합산 비중, 등장 ETF 목록).
        held_weight: dict[str, float] = {}
        held_in: dict[str, list[str]] = {}
        any_held_data = False
        for e in holding_etfs:
            if e == candidate_etf:
                continue
            cons = load_constituents(e, conn=conn)
            if cons:
                any_held_data = True
            for c in cons:
                k = _norm(c["constituent_ticker"])
                held_weight[k] = held_weight.get(k, 0.0) + (c.get("weight_pct") or 0.0)
                held_in.setdefault(k, []).append(e)
    finally:
        if own:
            conn.close()

    if not cand or not any_held_data:
        return {"ok": True, "candidate_etf": candidate_etf, "data_connected": False,
                "shared": [], "shared_count": 0,
                "combined_indirect_exposure_pct": 0.0, "concentration_flag": False,
                "note": ("후보 또는 보유 ETF 구성 미연동 — 중복노출 계산 불가(정직, 가짜 0)."
                         )}
    shared = []
    for k, c in cand.items():
        if k not in held_weight:
            continue
        cw = c.get("weight_pct") or 0.0
        shared.append({
            "ticker": c["constituent_ticker"],
            "name": c.get("constituent_name"),
            "weight_in_candidate": round(cw, 2),
            "weight_in_held_etfs_sum": round(held_weight[k], 2),
            "held_in": held_in[k],
            # 누적 노출 신호 = 후보 비중 + 보유 ETF 들 합산 비중.
            "combined_exposure_pct": round(cw + held_weight[k], 2),
        })
    shared.sort(key=lambda s: s["combined_exposure_pct"], reverse=True)
    # 합산 간접노출 = 겹치는 후보 구성종목들의 후보 내 비중 합(후보 도입 시 추가되는 겹침분).
    combined = round(sum(s["weight_in_candidate"] for s in shared), 2)
    return {
        "ok": True, "candidate_etf": candidate_etf,
        "holding_etfs": [e for e in holding_etfs if e != candidate_etf],
        "data_connected": True,
        "shared": shared, "shared_count": len(shared),
        "combined_indirect_exposure_pct": combined,
        "concentration_flag": combined >= 20.0,
        "note": ("후보↔보유 ETF 중복노출(읽기 전용): 공통종목·합산 간접노출. "
                 "겹침 20%+ 면 단일 종목 노출 집중 위험으로 표기(주문/정책 변경 아님)."),
    }


def _norm(t: str | None) -> str:
    return (t or "").strip().upper()


def stock_impact_on_etfs(stock_ticker: str, etf_tickers: list[str], conn=None) -> dict:
    """개별 종목이 (보유) ETF 들에 미치는 영향 — 그 종목이 각 ETF 에서 차지하는 비중.

    예: 삼성전자에 악재 → 삼성전자를 담은 ETF 들이 함께 흔들린다. 그 종목이 ETF 별로
        몇 % 비중인지, 합산하면 ETF 경유 간접 노출이 얼마나 되는지 드러낸다(읽기 전용).
    데이터(etf_constituents) 없으면 정직하게 data_connected:false (가짜 0).
    """
    tk = (stock_ticker or "").strip().upper()
    own = conn is None
    conn = conn or store_db.connect()
    try:
        per_etf = []
        any_data = False
        for e in etf_tickers:
            cons = load_constituents(e, conn=conn)
            if cons:
                any_data = True
            match = next((c for c in cons if (c.get("constituent_ticker") or "").upper() == tk), None)
            per_etf.append({
                "etf_ticker": e,
                "data_connected": bool(cons),
                "contains": match is not None,
                "weight_pct": (match.get("weight_pct") if match else None),
                "constituent_name": (match.get("constituent_name") if match else None),
            })
    finally:
        if own:
            conn.close()
    if not any_data:
        return {"ok": True, "stock_ticker": tk, "data_connected": False,
                "in_etfs": [], "etf_count_holding": 0, "sum_weight_in_etfs_pct": 0.0,
                "note": "ETF 구성 데이터 미연동(etf_constituents 없음) — 영향 분석 불가(정직)."}
    holding = [p for p in per_etf if p["contains"]]
    sum_w = round(sum((p["weight_pct"] or 0.0) for p in holding), 2)
    return {
        "ok": True, "stock_ticker": tk, "data_connected": True,
        "in_etfs": holding,
        "etf_count_holding": len(holding),
        "sum_weight_in_etfs_pct": sum_w,   # ETF 들 내 합산 비중(간접 노출 신호; 직접 포지션 아님)
        "per_etf": per_etf,
        "note": ("개별종목→ETF 영향(읽기 전용): 이 종목이 각 ETF 에서 차지하는 비중. "
                 "ETF 경유 간접 노출이 의도보다 클 수 있음(주문/정책 변경 아님)."),
    }


def stock_impact_on_account(stock_ticker: str, account_index: int) -> dict:
    """계좌의 보유/관심 ETF 들에 대해 개별종목 영향 분석(편의 래퍼)."""
    conn = store_db.connect()
    try:
        etfs = _account_etfs(conn, account_index)
        out = stock_impact_on_etfs(stock_ticker, etfs, conn=conn)
    finally:
        conn.close()
    out["account_index"] = account_index
    out["account_etfs"] = etfs
    return out


def _account_etfs(conn, account_index: int) -> list[str]:
    """계좌 보유 + 관심 ETF 티커(중복 제거). asset_class 에 etf 포함 또는 holdings 의 etf 추정."""
    etfs: set[str] = set()
    rows = conn.execute(
        "SELECT ticker, asset_class FROM universe_instruments "
        "WHERE account_index=? AND is_active=1", (account_index,)).fetchall()
    for r in rows:
        if r["asset_class"] and "etf" in r["asset_class"].lower():
            etfs.add(r["ticker"])
    # holdings 중 etf_constituents 에 존재하는 티커도 ETF 로 간주(데이터 기반).
    sid = conn.execute(
        "SELECT id FROM account_snapshots WHERE account_index=? ORDER BY id DESC LIMIT 1",
        (account_index,)).fetchone()
    if sid:
        for h in conn.execute("SELECT ticker FROM holdings WHERE snapshot_id=?", (sid["id"],)).fetchall():
            has = conn.execute(
                "SELECT 1 FROM etf_constituents WHERE etf_ticker=? LIMIT 1", (h["ticker"],)).fetchone()
            if has:
                etfs.add(h["ticker"])
    return sorted(etfs)


def analyze_account_etfs(account_index: int) -> dict:
    """계좌의 ETF 들 — 개별 분석 + 모든 쌍 겹침(집중 위험 후보 표기). 데이터 없으면 정직 미연동."""
    conn = store_db.connect()
    try:
        etfs = _account_etfs(conn, account_index)
        per_etf = [analyze_etf(e, conn=conn) for e in etfs]
        overlaps = []
        for i in range(len(etfs)):
            for j in range(i + 1, len(etfs)):
                ov = overlap(etfs[i], etfs[j], conn=conn)
                if ov.get("shared_count", 0) > 0:
                    overlaps.append(ov)
    finally:
        conn.close()
    overlaps.sort(key=lambda o: o.get("overlap_weight_pct", 0.0), reverse=True)
    connected = any(p.get("data_connected") for p in per_etf)
    return {
        "ok": True, "account_index": account_index, "analyzed_at": _now(),
        "etf_count": len(etfs), "etfs": etfs,
        "data_connected": connected,
        "per_etf": per_etf,
        "overlaps": overlaps,
        "concentration_flags": [o for o in overlaps if o.get("concentration_flag")],
        "auto_order_created": False,
        "note": ("계좌 ETF 분석(읽기 전용) — 구성·노출·겹침만. 주문/정책 변경 없음. "
                 "데이터 미연동 ETF 는 정직하게 '미연동'으로 표기." if connected else
                 "ETF 구성 데이터 미연동 — 겹침 분석 불가(정직). etf_constituents 적재 필요."),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--etf", help="단일 ETF 구성 분석")
    ap.add_argument("--overlap", nargs=2, metavar=("ETF_A", "ETF_B"), help="두 ETF 겹침")
    ap.add_argument("--account", type=int, help="계좌 ETF 겹침 분석")
    ap.add_argument("--stock-impact", nargs=2, metavar=("STOCK", "ACCOUNT"),
                    help="개별종목이 계좌 보유/관심 ETF 들에 미치는 영향")
    ap.add_argument("--candidate-overlap", nargs=2, metavar=("CANDIDATE_ETF", "ACCOUNT"),
                    help="후보 ETF 가 계좌 보유 ETF 들과 갖는 중복노출(합산 간접노출)")
    args = ap.parse_args()
    try:
        if args.candidate_overlap:
            cand, acct = args.candidate_overlap[0], int(args.candidate_overlap[1])
            conn = store_db.connect()
            try:
                held = _account_etfs(conn, acct)
                out = candidate_overlap_with_holdings(cand, held, conn=conn)
                out["account_index"] = acct
                out["account_etfs"] = held
            finally:
                conn.close()
        elif args.overlap:
            out = overlap(args.overlap[0], args.overlap[1])
        elif args.stock_impact:
            out = stock_impact_on_account(args.stock_impact[0], int(args.stock_impact[1]))
        elif args.etf:
            out = analyze_etf(args.etf)
        elif args.account is not None:
            out = analyze_account_etfs(args.account)
        else:
            out = {"ok": False,
                   "error": "--etf TICKER | --overlap A B | --account N | --stock-impact STOCK ACCOUNT"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
