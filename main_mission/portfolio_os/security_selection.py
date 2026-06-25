"""종목/ETF **선정 엔진** (Step 2–5) — bucket별 후보군 + evidence 수집 + 비교표.

3안(자산배분)이 확정된 뒤, 각 bucket(글로벌 코어 ETF·로봇·반도체·반도체 인버스·국채)에
대해 **실재 후보(ETF/종목)를 나열하고, 실측 데이터만 모아 비교**한다.

설계 원칙 (불변):
- **비교·토론 중심, 단정 금지.** 추천이 아니라 "현 정책·관점 기준 적합도 + 장단점".
- **근거 없는 강한 추천 금지.** 데이터가 부족하면 정직하게 "후보 비교 단계, 강한 추천 불가".
- **데이터 부족·미연동은 그대로 표기**(가짜 지표 0, 가짜 evidence 0).
- **자동주문/policy 변경 0, secret 0, Anthropic API 0** — 읽기 전용.
- 후보 메타(운용보수 등)가 DB 에 미연동이면 unknown 으로 둔다(추정 금지).

읽는 소스(읽기 전용, 본문 의존 X — 함수만 호출):
- evidence_summary.briefs_by_source_type / evidence_for_account  (자료·재무/뉴스/공시/ETF구성)
- etf_analysis.analyze_etf / overlap                             (구성·겹침)
- decline_scan.scan_instrument                                   (가격/하락 징후 6축)
- price_history.load_history                                     (가격/일봉 → 변동성)
- macro_connect.macro_snapshot                                   (거시)
- user_views.list_views / investor_objective.criteria_for_account (관점/목적)
- universe_instruments (계좌 후보; 국채 후보는 A 에이전트 시드)

CLI:
  python -m main_mission.portfolio_os.security_selection --account 1 --buckets
  python -m main_mission.portfolio_os.security_selection --account 1 --bucket semiconductor
  python -m main_mission.portfolio_os.security_selection --account 1 --compare semiconductor
"""
from __future__ import annotations

import argparse
import json
import math
import sys

from .store import db as store_db
from . import evidence_summary
from . import etf_analysis
from . import decline_scan
from . import price_history
from . import macro_connect
from . import user_views
from . import investor_objective
from .candidate import candidate_evaluation


# ---------------------------------------------------------------------------
# Bucket 정의 + 후보 시드 (실재 티커)
# ---------------------------------------------------------------------------
# 주의: 이건 "후보 나열"일 뿐 추천이 아니다. 지표 미연동이면 정직하게 unknown 표기한다.
# 국채 bucket 후보는 A 에이전트(bond_bucket) 시드를 우선 사용하고, 없으면 빈 채로 둔다.

BUCKETS: dict[str, dict] = {
    "global_core": {
        "label": "글로벌 코어 ETF",
        "kind": "etf",
        "seed": ["SPY", "VOO", "QQQ", "VT", "VTI"],
        "note": "광범위 시장 노출(코어). 후보 간 차이는 구성/비용/지역 노출에서 비교한다.",
    },
    "robotics": {
        "label": "로봇/자동화",
        "kind": "etf",
        "seed": ["BOTZ", "ROBO", "ARKQ"],
        "note": "로봇·자동화 테마 ETF. 구성 겹침·집중도를 비교한다.",
    },
    "semiconductor": {
        "label": "반도체",
        "kind": "mixed",
        "seed": ["SOXX", "SMH", "005930", "000660"],
        "note": "반도체 ETF + 개별 대표주(삼성전자 005930 / SK하이닉스 000660).",
    },
    "semiconductor_inverse": {
        "label": "반도체 인버스(헤지)",
        "kind": "inverse",
        "seed": ["SOXS", "KODEX 반도체인버스"],
        "note": "헤지 전용 bucket. 인버스는 hedge bucket 한정(롱 자산 대체 아님).",
    },
    "treasury": {
        "label": "국채(방어)",
        "kind": "bond",
        "seed": [],  # A 에이전트 bond_bucket 시드 사용 (아래 _bond_seed)
        "note": "국채는 방어(현금의 일부). 후보 시드는 A 에이전트(bond_bucket)에서 가져온다.",
    },
}

# 시드 종목 메타 라벨(표시용 — 지표가 아니라 이름/시장 식별만). 가짜 지표 아님.
_TICKER_META: dict[str, dict] = {
    "SPY": {"name": "SPDR S&P 500 ETF", "market": "US", "asset_class": "equity_etf"},
    "VOO": {"name": "Vanguard S&P 500 ETF", "market": "US", "asset_class": "equity_etf"},
    "QQQ": {"name": "Invesco QQQ (Nasdaq-100)", "market": "US", "asset_class": "equity_etf"},
    "VT": {"name": "Vanguard Total World Stock ETF", "market": "US", "asset_class": "equity_etf"},
    "VTI": {"name": "Vanguard Total US Stock Market ETF", "market": "US", "asset_class": "equity_etf"},
    "BOTZ": {"name": "Global X Robotics & AI ETF", "market": "US", "asset_class": "equity_etf"},
    "ROBO": {"name": "ROBO Global Robotics & Automation ETF", "market": "US", "asset_class": "equity_etf"},
    "ARKQ": {"name": "ARK Autonomous Tech & Robotics ETF", "market": "US", "asset_class": "equity_etf"},
    "SOXX": {"name": "iShares Semiconductor ETF", "market": "US", "asset_class": "equity_etf"},
    "SMH": {"name": "VanEck Semiconductor ETF", "market": "US", "asset_class": "equity_etf"},
    "005930": {"name": "삼성전자", "market": "KRX", "asset_class": "stock"},
    "000660": {"name": "SK하이닉스", "market": "KRX", "asset_class": "stock"},
    "SOXS": {"name": "Direxion Daily Semiconductor Bear 3X (인버스)", "market": "US", "asset_class": "inverse_etf"},
    "KODEX 반도체인버스": {"name": "KODEX 반도체인버스", "market": "KRX", "asset_class": "inverse_etf"},
}


def list_buckets() -> dict:
    """선정 대상 bucket 목록(라벨/종류/후보 시드 개수)."""
    out = {}
    for key, spec in BUCKETS.items():
        seed = spec["seed"]
        out[key] = {"label": spec["label"], "kind": spec["kind"],
                    "seed_count": len(seed), "note": spec["note"]}
    return out


# ---------------------------------------------------------------------------
# 후보 수집
# ---------------------------------------------------------------------------
def _is_etf_like(asset_class: str | None) -> bool:
    return bool(asset_class) and asset_class.endswith("etf")


def _bond_seed(account_index: int, conn) -> list[dict]:
    """국채 bucket 후보 — A 에이전트(bond_bucket) 시드를 universe_instruments 에서 가져온다.

    A 에이전트가 채권/국채 후보를 universe 에 시드해 두면(asset_class='bond' 또는 이름에 '국채/국고채'),
    그것을 후보로 쓴다. 없으면 빈 리스트(미연동 정직) — 가짜 국채 종목을 만들지 않는다.
    """
    rows = conn.execute(
        "SELECT ticker, name, market, asset_class, is_inverse, is_leveraged "
        "FROM universe_instruments WHERE account_index=? AND is_active=1",
        (account_index,),
    ).fetchall()
    cands = []
    for r in rows:
        name = r["name"] or ""
        ac = (r["asset_class"] or "").lower()
        if ac == "bond" or any(k in name for k in ("국채", "국고채", "treasury", "Treasury")):
            cands.append({
                "ticker": r["ticker"], "name": r["name"] or r["ticker"],
                "market": r["market"], "asset_class": r["asset_class"] or "bond",
                "source": "universe(A:bond_bucket)",
            })
    return cands


def bucket_candidates(account_index: int, bucket: str, *, conn=None) -> dict:
    """bucket 의 후보 리스트.

    - 시드(실재 티커) + 계좌 universe_instruments 에 등록된 같은 종류 후보를 합친다(중복 제거).
    - 국채 bucket 은 A 에이전트 bond_bucket 시드를 사용한다.
    - **후보 나열일 뿐 추천 아님.** 메타가 미연동이면 정직하게 표기.
    """
    spec = BUCKETS.get(bucket)
    if spec is None:
        return {"ok": False, "error": f"알 수 없는 bucket: {bucket!r} "
                f"(허용: {tuple(BUCKETS)})"}

    own = conn is None
    conn = conn or store_db.connect()
    try:
        candidates: list[dict] = []
        seen: set[str] = set()

        if bucket == "treasury":
            for c in _bond_seed(account_index, conn):
                if c["ticker"] in seen:
                    continue
                seen.add(c["ticker"])
                candidates.append(c)
        else:
            for tk in spec["seed"]:
                if tk in seen:
                    continue
                seen.add(tk)
                meta = _TICKER_META.get(tk, {})
                candidates.append({
                    "ticker": tk,
                    "name": meta.get("name"),
                    "market": meta.get("market"),
                    "asset_class": meta.get("asset_class"),
                    "source": "seed",
                    "meta_connected": tk in _TICKER_META,
                })

        # 계좌 universe 에 등록된 동일 종류 후보 추가(사용자가 직접 넣은 후보).
        urows = conn.execute(
            "SELECT ticker, name, market, asset_class, is_inverse FROM universe_instruments "
            "WHERE account_index=? AND is_active=1", (account_index,),
        ).fetchall()
        for r in urows:
            tk = r["ticker"]
            if tk in seen:
                continue
            ac = (r["asset_class"] or "").lower()
            is_inv = bool(r["is_inverse"]) or "inverse" in ac
            match = False
            if bucket == "semiconductor_inverse":
                match = is_inv
            elif bucket == "treasury":
                match = False  # bond 는 _bond_seed 가 전담
            elif spec["kind"] == "etf":
                match = _is_etf_like(ac) and not is_inv
            elif spec["kind"] == "mixed":
                match = (not is_inv) and ac in ("equity_etf", "stock", "etf", "")
            if not match:
                continue
            seen.add(tk)
            candidates.append({
                "ticker": tk, "name": r["name"], "market": r["market"],
                "asset_class": r["asset_class"], "source": "universe(user)",
                "meta_connected": bool(r["name"]),
            })
    finally:
        if own:
            conn.close()

    bond_unseeded = (bucket == "treasury" and not candidates)
    return {
        "ok": True,
        "account_index": account_index,
        "bucket": bucket,
        "label": spec["label"],
        "kind": spec["kind"],
        "candidates": candidates,
        "candidate_count": len(candidates),
        "note": (spec["note"] if not bond_unseeded else
                 spec["note"] + " (현재 A 에이전트 bond_bucket 시드 미연동 — 후보 비어 있음, 정직 표기.)"),
        "honest_flags": (["국채 후보 미연동(A:bond_bucket 시드 필요)"] if bond_unseeded else []),
    }


# ---------------------------------------------------------------------------
# 데이터 가용성 상태 (후보별)
# ---------------------------------------------------------------------------
def _is_etf_candidate(cand: dict) -> bool:
    ac = (cand.get("asset_class") or "").lower()
    return ac.endswith("etf") or ac == "etf"


def data_availability(account_index: int, cand: dict, *, conn=None) -> dict:
    """후보 1개의 데이터 가용성(connected/미연동) — 가짜 표기 금지.

    축: 재무·뉴스·공시·ETF구성·거시·수급·가격/일봉. ETF 면 재무="직접대상 아님".
    """
    own = conn is None
    conn = conn or store_db.connect()
    try:
        tk = cand["ticker"]
        is_etf = _is_etf_candidate(cand)

        # 가격/일봉
        hist = price_history.load_history(tk)
        price_status = "connected" if hist else "미연동"

        # ETF 구성 (ETF 만 해당)
        if is_etf:
            etf = etf_analysis.analyze_etf(tk, conn=conn)
            etf_status = "connected" if etf.get("data_connected") else "미연동"
        else:
            etf_status = "해당 없음(개별종목)"

        # 거시 (계좌 공통)
        macro = macro_connect.macro_snapshot(conn=conn)
        macro_status = "connected" if macro.get("data_available") else "미연동"

        # 자료(evidence) — 후보 ticker/etf 에 연결된 항목 유무
        ev = _evidence_index(account_index, conn=conn)
        ev_for = ev.get(tk, {})
        fin_status = ("직접대상 아님(ETF)" if is_etf else
                      ("connected" if ev_for.get("financials") else "미연동"))
        news_status = "connected" if ev_for.get("news") else "미연동"
        filing_status = "connected" if ev_for.get("filing") else "미연동"
        flow_status = "connected" if ev_for.get("flow") else "미연동"
    finally:
        if own:
            conn.close()

    return {
        "ticker": cand["ticker"],
        "financials": fin_status,
        "news": news_status,
        "filing": filing_status,
        "etf_constituents": etf_status,
        "macro": macro_status,
        "flow": flow_status,
        "price_daily": price_status,
    }


def _evidence_index(account_index: int, *, conn=None) -> dict:
    """후보별로 어떤 source_type 의 evidence 가 붙어 있는지 인덱스화.

    {ticker: {source_type: [evidence_item, ...]}} 형태. 데이터 없으면 빈 dict(정직).
    """
    res = evidence_summary.evidence_for_account(account_index, conn=conn)
    idx: dict[str, dict] = {}
    for it in res.get("items", []):
        for key in (it.get("related_ticker"), it.get("related_etf")):
            if not key:
                continue
            idx.setdefault(key, {}).setdefault(it.get("source_type") or "기타", []).append({
                "id": it.get("id"), "source": it.get("source"),
                "source_date": it.get("source_date"), "summary": it.get("summary"),
                "confidence": it.get("eff_confidence"), "stale": it.get("stale"),
            })
    return idx


def evidence_for(account_index: int, cand: dict, *, conn=None) -> dict:
    """후보에 부착할 evidence — 가용 소스만 모은다. 없으면 빈 채로 정직.

    가짜 evidence 를 만들지 않는다. ETF 구성/가격 같은 실측은 별도(데이터 가용성)에서 처리.
    """
    own = conn is None
    conn = conn or store_db.connect()
    try:
        idx = _evidence_index(account_index, conn=conn)
        by_type = idx.get(cand["ticker"], {})
        total = sum(len(v) for v in by_type.values())
    finally:
        if own:
            conn.close()
    return {
        "ticker": cand["ticker"],
        "evidence_count": total,
        "by_source_type": by_type,
        "note": ("연결된 자료 없음 — 강한 결론 불가(정직)." if total == 0 else
                 "보유/관심 연결 evidence(읽기 전용). 가짜 생성 안 함."),
    }


# ---------------------------------------------------------------------------
# 개별주 **저평가 우량주 필터** (quality_filter)
# ---------------------------------------------------------------------------
# 핵심 원칙(불변):
#  - 재무(매출성장·영업이익률·순이익·부채비율·현금흐름·ROE)·밸류에이션(PER/PBR/EV-EBITDA)·
#    실적/컨센서스는 **구조화 수치로 미연동**(현재 연동: 가격/일봉만).
#  - 데이터 미연동이면 **passed=None + "필터 적용 불가(데이터 필요)"** 로 정직 표기.
#    → 가짜 통과/가짜 점수 0. 급등주/적자테마/부실을 우량주로 표기하지 않는다.
#  - evidence_items 에 'financials' 자료가 있어도 그것은 *정성 자료*일 뿐,
#    구조화된 재무/밸류에이션 수치가 아니므로 통과 판정 근거가 될 수 없다(정직).

# 우량주 판정에 필요한 구조화 지표(현재 전부 미연동 — 적재 지점 표기용).
_QUALITY_METRICS: dict[str, dict] = {
    # 재무 안정/성장/수익성
    "revenue_growth_yoy":   {"group": "financial", "label": "매출성장(YoY)", "good": "양호한 성장"},
    "operating_margin_pct": {"group": "financial", "label": "영업이익률", "good": "높을수록 우량"},
    "net_income":           {"group": "financial", "label": "순이익", "good": "흑자(적자테마 배제)"},
    "debt_to_equity_pct":   {"group": "financial", "label": "부채비율", "good": "낮을수록 안정"},
    "operating_cash_flow":  {"group": "financial", "label": "영업현금흐름", "good": "양(+) 현금창출"},
    "roe_pct":              {"group": "financial", "label": "ROE", "good": "높을수록 자본효율"},
    # 밸류에이션(저평가)
    "per":                  {"group": "valuation", "label": "PER", "good": "낮을수록 저평가"},
    "pbr":                  {"group": "valuation", "label": "PBR", "good": "낮을수록 저평가"},
    "ev_ebitda":            {"group": "valuation", "label": "EV/EBITDA", "good": "낮을수록 저평가"},
    # 실적/컨센서스
    "earnings_surprise":    {"group": "consensus", "label": "실적 서프라이즈", "good": "컨센서스 상회"},
    "consensus_trend":      {"group": "consensus", "label": "컨센서스 추세", "good": "상향 조정"},
}


def _structured_financials(ticker: str, *, conn) -> dict | None:
    """구조화된 재무/밸류에이션 수치를 읽는 단일 지점.

    `fundamentals` 테이블(financials_connect 가 DART 공식 데이터로 적재)에서 해당 종목의
    **최신 기간** 1행을 읽어 quality_filter 판정용 키로 매핑한다. 행이 없으면 None →
    quality_filter 는 정직하게 "필터 적용 불가(데이터 필요)" 로 둔다(가짜 통과/점수 0).
    (evidence_items 의 'financials' 는 정성 자료라 수치 판정에 쓰지 않는다.)

    fundamentals 컬럼 → _QUALITY_METRICS 키 매핑:
      op_margin→operating_margin_pct, debt_ratio→debt_to_equity_pct,
      cash_flow_op→operating_cash_flow, roe→roe_pct, per/pbr/ev_ebitda 그대로,
      net_income 그대로. (revenue_growth_yoy/earnings_surprise/consensus_trend 는
      단일 행만으로 산출 불가 → 미포함 = 미연동 정직, 해당 지표는 판정 보류.)
    값이 None 인 컬럼은 키에서 제외(가짜 0 금지 — quality_filter 가 available=False 로 처리).
    """
    tk = (ticker or "").strip()
    if not tk:
        return None
    try:
        row = conn.execute(
            "SELECT revenue, op_income, net_income, op_margin, debt_ratio, cash_flow_op, "
            "roe, per, pbr, ev_ebitda FROM fundamentals WHERE ticker=? "
            "ORDER BY period DESC LIMIT 1", (tk,)).fetchone()
    except Exception:  # noqa: BLE001 — 테이블 미존재 등은 미연동으로 정직 처리.
        return None
    if row is None:
        return None
    mapping = {
        "net_income": row["net_income"],
        "operating_margin_pct": row["op_margin"],
        "debt_to_equity_pct": row["debt_ratio"],
        "operating_cash_flow": row["cash_flow_op"],
        "roe_pct": row["roe"],
        "per": row["per"],
        "pbr": row["pbr"],
        "ev_ebitda": row["ev_ebitda"],
    }
    out = {k: v for k, v in mapping.items() if v is not None}
    return out or None  # 추출된 수치가 하나도 없으면 미연동으로 둔다(정직).


def quality_filter(ticker: str, *, conn=None) -> dict:
    """개별주 **저평가 우량주 필터**.

    재무(매출성장·영업이익률·순이익·부채비율·현금흐름·ROE)·밸류에이션(PER/PBR/EV-EBITDA)·
    실적/컨센서스를 종합해 *저평가·재무안정·현금흐름·저부채* 를 통과 기준으로 본다.

    **데이터 미연동(구조화 재무 수치 없음)이면 `passed=None` + "필터 적용 불가(데이터 필요)"**
    로 정직 표기한다. 가짜 통과/가짜 점수 0. 급등주/적자테마/부실 종목을 우량주로 표기하지 않는다.
    """
    tk = (ticker or "").strip()
    own = conn is None
    conn = conn or store_db.connect()
    try:
        meta = _TICKER_META.get(tk, {})
        is_etf_meta = (meta.get("asset_class") or "").endswith("etf")
        fin = _structured_financials(tk, conn=conn)

        # evidence 에 정성 'financials' 자료가 붙어 있는지(수치는 아님 — 참고 표기용).
        has_fin_evidence = _has_financials_evidence(tk, conn=conn)
    finally:
        if own:
            conn.close()

    if is_etf_meta:
        return {
            "ok": True, "ticker": tk, "applicable": False, "passed": None,
            "reason": "ETF/지수형 — 개별주 우량주 필터 대상 아님(ETF 는 etf_scorecard 사용).",
            "metrics": {}, "honest": True,
        }

    if fin is None:
        # 구조화 재무/밸류에이션 수치 미연동 → 절대 가짜 통과/점수 만들지 않음.
        return {
            "ok": True, "ticker": tk, "applicable": True, "passed": None,
            "reason": "필터 적용 불가(데이터 필요) — 재무/밸류에이션 구조화 수치 미연동.",
            "missing_metrics": [m["label"] for m in _QUALITY_METRICS.values()],
            "required_groups": ["financial", "valuation", "consensus"],
            "note": ("현재 연동: 가격/일봉만. 재무/밸류에이션/컨센서스 수치 미연동이라 "
                     "우량/저평가 통과 판정 불가. 가짜 점수 만들지 않음(정직). "
                     + ("정성 'financials' 자료는 있으나 수치가 아니라 판정 근거 아님."
                        if has_fin_evidence else "정성 'financials' 자료도 없음.")),
            "qualitative_financials_evidence": has_fin_evidence,
            "honest": True,
        }

    # --- 여기 아래는 fundamentals 가 연동된 뒤에만 도달(현재 도달 불가). ---
    # 데이터가 연동되면 group별 기준으로 통과 판정 + 사유. 가짜 점수 금지 유지.
    checks: list[dict] = []
    metrics_out: dict[str, dict] = {}
    for key, spec in _QUALITY_METRICS.items():
        val = fin.get(key)
        metrics_out[key] = {"label": spec["label"], "value": val,
                            "available": val is not None}
        if val is None:
            continue
        ok, why = _judge_quality_metric(key, val)
        checks.append({"metric": key, "label": spec["label"], "value": val,
                       "ok": ok, "why": why})

    if not checks:
        return {"ok": True, "ticker": tk, "applicable": True, "passed": None,
                "reason": "필터 적용 불가(데이터 필요) — 판정 가능한 수치 없음.",
                "metrics": metrics_out, "honest": True}

    failed = [c for c in checks if not c["ok"]]
    passed = len(failed) == 0
    return {
        "ok": True, "ticker": tk, "applicable": True, "passed": passed,
        "reason": ("재무안정·저부채·현금흐름·저평가 기준 통과" if passed else
                   "기준 미달: " + "; ".join(f"{c['label']}({c['why']})" for c in failed)),
        "checks": checks, "metrics": metrics_out,
        "failed_metrics": [c["metric"] for c in failed],
        "honest": True,
    }


def _judge_quality_metric(key: str, val) -> tuple[bool, str]:
    """단일 지표 통과 판정(수치 연동 시에만 호출). 보수적 기준 — 부실/적자/고평가 배제."""
    try:
        v = float(val)
    except (TypeError, ValueError):
        return True, "수치 아님(판정 보류)"
    if key == "net_income":
        return (v > 0, "흑자" if v > 0 else "적자(우량 배제)")
    if key == "operating_cash_flow":
        return (v > 0, "현금창출(+)" if v > 0 else "현금유출(-)")
    if key == "debt_to_equity_pct":
        return (v <= 200.0, "저부채" if v <= 200.0 else "고부채(>200%)")
    if key == "operating_margin_pct":
        return (v >= 5.0, "수익성 양호" if v >= 5.0 else "박한 마진(<5%)")
    if key == "roe_pct":
        return (v >= 8.0, "자본효율 양호" if v >= 8.0 else "낮은 ROE(<8%)")
    if key == "revenue_growth_yoy":
        return (v >= 0.0, "성장/유지" if v >= 0.0 else "역성장")
    if key == "per":
        return (0 < v <= 25.0, "저평가권" if 0 < v <= 25.0 else "고평가/적자(PER)")
    if key == "pbr":
        return (0 < v <= 3.0, "저평가권" if 0 < v <= 3.0 else "고평가(PBR)")
    if key == "ev_ebitda":
        return (0 < v <= 15.0, "저평가권" if 0 < v <= 15.0 else "고평가(EV/EBITDA)")
    return True, "기준 미정의(판정 보류)"


def _has_financials_evidence(ticker: str, *, conn) -> bool:
    """evidence_items 에 해당 종목 'financials' source_type 자료가 있는지(정성 — 수치 아님)."""
    row = conn.execute(
        "SELECT 1 FROM evidence_items WHERE related_ticker=? AND source_type='financials' LIMIT 1",
        (ticker,)).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# 보조 지표 (실측만)
# ---------------------------------------------------------------------------
def _volatility(hist: list[dict]) -> dict:
    """일간 수익률 표준편차(연율화) — 가격이 충분하면 계산, 아니면 unknown."""
    closes = [b["close"] for b in hist if b.get("close")]
    if len(closes) < 20:
        return {"value": None, "available": False,
                "reason": f"가격 데이터 부족(bars={len(closes)}, 최소 20 필요)"}
    rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) < 2:
        return {"value": None, "available": False, "reason": "수익률 계산 불가"}
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    daily_sd = math.sqrt(var)
    annual = round(daily_sd * math.sqrt(252) * 100, 1)  # %
    return {"value": annual, "available": True, "bars": len(closes),
            "unit": "annualized_pct"}


def _flow_signal(ticker: str, *, conn) -> dict:
    """후보의 **수급(투자자별 매매동향) 신호** — investor_flows 실데이터 기반(가짜 0 금지).

    외국인·기관 이탈(동반 순매도) → "진입 속도 조절" 설명 / 조정 중 기관 순매수 → "방어 후보".
    **설명 중심·단정 금지.** 데이터 없으면 available=False(미연동 — 미반영).
    "외국인 매도 = 무조건 매도" 식 단정 금지(확률적·해석적 표현만).
    """
    from .decline.axes import distribution as dist_axis
    rows = conn.execute(
        "SELECT trade_date, foreign_net, institution_net, retail_net, volume "
        "FROM investor_flows WHERE instrument_code=? ORDER BY trade_date DESC LIMIT 60",
        (ticker,)).fetchall()
    flows = [dict(r) for r in reversed(rows)]
    if not flows:
        return {"available": False,
                "reason": "투자자 매매동향(investor_flows) 미연동 — 수급 미반영(정직).",
                "tone": "neutral", "note": None}

    # 분산축 scorer 를 재사용 — 동일 해석(외국인·기관 이탈/기관 방어매수)을 후보 비교에 투영.
    ax = dist_axis.score({"investor_flows": flows})
    if not ax.get("data_available"):
        return {"available": False,
                "reason": ax.get("detail", "수급 데이터 부족 — 미반영"),
                "tone": "neutral", "note": None}

    fired = {s["name"] for s in ax.get("signals", []) if s["fired"]}
    risk = ax.get("risk_0_100", 0.0)
    dist = "smart_money_distribution" in fired
    buffer = "institution_buy_buffer" in fired

    if dist:
        tone = "caution"
        note = ("외국인·기관 동반 순매도(세력 이탈 신호) — 진입 속도 조절 권고(분할/지정가 무릎). "
                "단정 아님: 수급은 추세 전 신호일 수도, 단기 노이즈일 수도 있음." +
                (" 기관 방어 매수가 일부 받쳐 완화." if buffer else ""))
    elif buffer:
        tone = "supportive"
        note = ("조정 중 기관계(연기금 등) 순매수 — 하방 방어 후보 가능성. "
                "단정 아님(방어가 일시적일 수 있음).")
    else:
        tone = "neutral"
        note = "뚜렷한 수급 분산/방어 신호 없음(중립)."
    return {"available": True, "tone": tone, "distribution_risk": risk,
            "fired": sorted(fired), "confidence": ax.get("confidence"),
            "detail": ax.get("detail"), "note": note}


def _user_view_fit(account_index: int, cand: dict, criteria: dict, *, conn) -> dict:
    """관점 적합성 — user_views/objective 대비 (단정 아님, 기록된 견해와의 정합성만)."""
    tk = cand["ticker"]
    views = user_views.list_views(account_index, status="active")
    matched = [v for v in views
               if (v.get("ticker") and v["ticker"] == tk)
               or (v.get("etf") and v["etf"] == tk)]
    stances = sorted({v.get("stance") for v in matched if v.get("stance")})
    goal_set = bool(criteria.get("is_set"))
    if not matched and not goal_set:
        return {"fit": "unknown", "matched_views": 0,
                "detail": "관련 견해/목적 미설정 — 적합성 판단 보류(정직)."}
    detail_parts = []
    if matched:
        detail_parts.append(f"관련 견해 {len(matched)}건(stance: {', '.join(stances) or 'n/a'})")
    if goal_set:
        detail_parts.append(f"목적='{criteria.get('label') or criteria.get('goal')}'")
    return {"fit": "context_available", "matched_views": len(matched),
            "stances": stances, "goal": criteria.get("goal"),
            "detail": "; ".join(detail_parts) + " 대비 비교(추천 아님)."}


# ---------------------------------------------------------------------------
# 비교표
# ---------------------------------------------------------------------------
def _confidence(avail: dict, ev_count: int, vol_available: bool,
                overlap_known: bool) -> dict:
    """데이터 가용성에 비례한 confidence(0~1). 부족하면 강한 결론 금지 플래그."""
    connected = sum(1 for k, v in avail.items()
                    if k != "ticker" and isinstance(v, str) and v == "connected")
    # 가용 축 개수 + evidence + 보조지표로 신뢰도 산출(실측 기반, 가짜 없음).
    score = 0.0
    score += min(connected, 4) * 0.12   # 데이터 축
    score += min(ev_count, 3) * 0.08    # evidence
    score += 0.08 if vol_available else 0.0
    score += 0.08 if overlap_known else 0.0
    score = round(min(score, 0.9), 2)   # 상한 0.9 — 단정 방지
    strong_ok = score >= 0.5 and (ev_count > 0 or connected >= 2)
    return {"value": score, "strong_conclusion_allowed": strong_ok,
            "connected_axes": connected, "evidence_count": ev_count}


def compare_bucket(account_index: int, bucket: str, *, conn=None) -> dict:
    """bucket 후보 비교표 — 후보별 {장점, 리스크, 비용, 중복노출, 변동성, 관점적합성, confidence}.

    **데이터 부족이면 강한 결론 금지** → "후보 비교 단계, 강한 추천 불가" 표기.
    추천이 아니라 비교/토론 자료다(자동주문/policy 0).
    """
    own = conn is None
    conn = conn or store_db.connect()
    try:
        cb = bucket_candidates(account_index, bucket, conn=conn)
        if not cb.get("ok"):
            return cb
        criteria = investor_objective.criteria_for_account(account_index)
        cands = cb["candidates"]
        etf_tickers = [c["ticker"] for c in cands if _is_etf_candidate(c)]

        rows = []
        for cand in cands:
            tk = cand["ticker"]
            is_etf = _is_etf_candidate(cand)
            avail = data_availability(account_index, cand, conn=conn)
            ev = evidence_for(account_index, cand, conn=conn)

            # 비용(운용보수) — DB 메타 미연동: unknown (추정 금지)
            cost = {"expense_ratio_pct": None, "available": False,
                    "reason": "운용보수 메타 미연동 — unknown(추정 금지)."}

            # 중복노출(겹침) — ETF 끼리만, etf_analysis.overlap (구성 있을 때만)
            overlaps = []
            overlap_known = False
            if is_etf:
                for other in etf_tickers:
                    if other == tk:
                        continue
                    ov = etf_analysis.overlap(tk, other, conn=conn)
                    if ov.get("data_connected"):
                        overlap_known = True
                        overlaps.append({"with": other,
                                         "overlap_weight_pct": ov.get("overlap_weight_pct"),
                                         "shared_count": ov.get("shared_count"),
                                         "concentration_flag": ov.get("concentration_flag")})
            overlap_block = (overlaps if overlaps else
                             ({"available": False, "reason": "ETF 구성 미연동 — 겹침 계산 불가"}
                              if is_etf else
                              {"available": False, "reason": "개별종목 — ETF 겹침 해당 없음"}))

            # 변동성 — 가격 있으면 계산
            hist = price_history.load_history(tk)
            vol = _volatility(hist)

            # 하락 징후(6축) — 가격 있으면 scan, 데이터 부족이면 정직 미연동
            sector = "반도체" if bucket.startswith("semiconductor") else None
            scan = decline_scan.scan_instrument(tk, sector=sector, history=hist or None)
            if scan.get("ok"):
                risk = {"available": True, "risk_level": scan.get("risk_level"),
                        "risk_score": scan.get("risk_score"),
                        "holistic_risk": scan.get("holistic_risk"),
                        "overall_confidence": scan.get("overall_confidence")}
            else:
                risk = {"available": False, "reason": scan.get("reason", "no_data")}

            view_fit = _user_view_fit(account_index, cand, criteria, conn=conn)
            flow = _flow_signal(tk, conn=conn)  # 수급(투자자별 매매동향) — 실데이터, 가짜 0 금지
            conf = _confidence(avail, ev["evidence_count"], vol["available"], overlap_known)

            # 개별주 우량주 필터 / ETF 스코어카드 — 종류에 맞게 1개만.
            if is_etf:
                quality = None
                scorecard = etf_scorecard(tk, account_index, conn=conn)
            else:
                quality = quality_filter(tk, conn=conn)
                scorecard = None

            # 장점/리스크 — 실측에서 도출, 데이터 없으면 정직 표기(단정 아님).
            pros, cons = _pros_cons(cand, bucket, avail, vol, risk, overlaps, ev,
                                    quality, scorecard, flow)

            rows.append({
                "ticker": tk, "name": cand.get("name"),
                "asset_class": cand.get("asset_class"),
                "data_availability": avail,
                "pros": pros, "risks": cons,
                "cost": cost,
                "overlap_exposure": overlap_block,
                "volatility": vol,
                "decline_risk": risk,
                "flow_signal": flow,
                "view_fit": view_fit,
                "quality_filter": quality,        # 개별주만(ETF 면 None)
                "etf_scorecard": scorecard,       # ETF 만(개별주면 None)
                "evidence": {"count": ev["evidence_count"],
                             "by_source_type": list(ev["by_source_type"].keys())},
                "confidence": conf,
            })
    finally:
        if own:
            conn.close()

    # bucket 전체 데이터 충분성 판단 — 하나라도 강한 결론 가능?
    any_strong = any(r["confidence"]["strong_conclusion_allowed"] for r in rows)
    headline = ("후보 비교 자료(읽기 전용). 데이터가 충분한 후보는 정합성까지 비교 가능."
                if any_strong else
                "후보 비교 단계 — 데이터 부족으로 강한 추천 불가(정직). 자료 적재 후 재평가.")
    return {
        "ok": True, "account_index": account_index, "bucket": bucket,
        "label": cb["label"], "kind": cb["kind"],
        "candidate_count": len(rows),
        "comparison": rows,
        "strong_conclusion_possible": any_strong,
        "honest_flags": cb.get("honest_flags", []),
        "headline": headline,
        "note": "비교·토론 중심. 추천/주문/policy 변경 아님. 근거 없는 강한 단정 금지.",
    }


def _pros_cons(cand, bucket, avail, vol, risk, overlaps, ev,
               quality=None, scorecard=None, flow=None) -> tuple[list[str], list[str]]:
    """실측에서만 장점/리스크 도출. 데이터 없으면 '데이터 부족' 으로 정직 표기."""
    pros, cons = [], []
    # 데이터 가용성 자체를 장점/한계로 노출(단정 아님).
    connected_axes = [k for k, v in avail.items()
                      if k != "ticker" and isinstance(v, str) and v == "connected"]
    if connected_axes:
        pros.append(f"가용 데이터 축: {', '.join(connected_axes)}")
    else:
        cons.append("연결된 실측 데이터 없음 — 비교 근거 부족(정직).")

    if vol.get("available"):
        pros.append(f"변동성(연율) {vol['value']}% 측정 가능(bars={vol['bars']})")
    else:
        cons.append(f"변동성 산출 불가({vol.get('reason')})")

    if risk.get("available"):
        lvl = risk.get("risk_level")
        msg = f"하락 징후 6축 분석 가용(risk_level={lvl})"
        (cons if lvl in ("high", "elevated") else pros).append(msg)
    else:
        cons.append(f"하락 징후 분석 불가({risk.get('reason')})")

    if overlaps:
        flagged = [o for o in overlaps if o.get("concentration_flag")]
        if flagged:
            cons.append(f"ETF 겹침 집중 위험: {[o['with'] for o in flagged]} (중복노출↑)")
        else:
            pros.append("ETF 겹침 계산 가능(집중 위험 임계 미만)")

    if ev["evidence_count"] > 0:
        pros.append(f"연결 자료 {ev['evidence_count']}건(읽기 전용 근거)")
    else:
        cons.append("연결 자료 0건 — 정성 근거 미흡")

    # 개별주 우량주 필터 결과 반영(가짜 통과 금지 — passed=None 이면 정직 표기).
    if quality is not None and quality.get("applicable"):
        if quality.get("passed") is True:
            pros.append("우량주 필터 통과(저평가·재무안정·현금흐름·저부채)")
        elif quality.get("passed") is False:
            cons.append(f"우량주 필터 미달: {quality.get('reason')}")
        else:  # passed is None
            cons.append("우량주 필터 적용 불가(재무/밸류에이션 데이터 미연동) — 강한 결론 금지")

    # ETF 스코어카드 결과 반영(기존 보유와 중복노출 집중 시 리스크).
    if scorecard is not None:
        ow = scorecard.get("scorecard", {}).get("overlap_with_holdings", {})
        if ow.get("status") == "connected" and ow.get("concentration_flag"):
            cons.append(f"기존 보유와 중복노출 집중(겹침 {ow.get('max_overlap_weight_pct')}%)")
        if scorecard.get("unconnected_axes"):
            cons.append(f"ETF 선정 기준 미연동 항목 {len(scorecard['unconnected_axes'])}개"
                        " (운용보수/괴리율/추적오차 등 — unknown, 추정 금지)")

    # 수급(투자자별 매매동향) 반영 — 설명 중심·단정 금지. 데이터 없으면 미반영(정직).
    if flow is not None and flow.get("available"):
        tone = flow.get("tone")
        if tone == "caution":
            cons.append("수급: " + flow.get("note", "외국인·기관 이탈 — 진입 속도 조절"))
        elif tone == "supportive":
            pros.append("수급: " + flow.get("note", "기관 방어 매수 — 하방 방어 후보"))
        else:
            pros.append("수급: 투자자 매매동향 연동(분산/방어 신호 중립)")
    elif flow is not None:
        cons.append("수급: 투자자 매매동향 미연동 — 수급 반영 불가(정직)")

    if bucket == "semiconductor_inverse":
        cons.append("인버스 — 헤지 전용(롱 대체 금지), 장기 보유 시 추적오차/감쇠 위험")
    return pros, cons


# ---------------------------------------------------------------------------
# ETF **선정 기준 스코어카드** (etf_scorecard)
# ---------------------------------------------------------------------------
# 점검 축: 기초지수·상위구성·섹터/국가노출·운용보수·거래량·괴리율·추적오차·환헤지·분배금·
#          기존 보유와 중복노출·최근성과·하락징후·거시민감도.
# 연동 가능(현재): 상위구성·섹터/국가노출(etf_constituents) · 기존보유 중복노출(holdings) ·
#                  최근성과/하락징후(price_history) · 거시(macro_connect, 계좌 공통).
# 미연동(추정 금지 → "미연동"/unknown): 기초지수메타·운용보수·거래량·괴리율·추적오차·환헤지·분배금.

# ETF 메타(운용보수/괴리율/추적오차 등) 미연동 항목 — 적재 지점 표기.
_ETF_META_FIELDS = {
    "underlying_index": "기초지수",
    "expense_ratio_pct": "운용보수",
    "avg_volume": "거래량",
    "premium_discount_pct": "괴리율",
    "tracking_error_pct": "추적오차",
    "fx_hedged": "환헤지",
    "distribution_yield_pct": "분배금",
}


def etf_scorecard(ticker: str, account_index: int, *, conn=None) -> dict:
    """ETF **선정 기준 스코어카드** — 항목별 connected/미연동 정직 표기.

    연동 항목은 실측으로 채우고, 미연동 항목(운용보수/괴리율/추적오차/환헤지/분배금/거래량/
    기초지수메타)은 **"미연동"(unknown)** 으로 둔다(추정 금지). 가짜 점수 0.
    데이터가 부족하면 강한 결론을 막는 신호(strong_conclusion_allowed)를 함께 반환한다.
    """
    tk = (ticker or "").strip()
    own = conn is None
    conn = conn or store_db.connect()
    try:
        meta = _TICKER_META.get(tk, {})
        ac = (meta.get("asset_class") or "").lower()
        is_etf_meta = ac.endswith("etf") or ac == "etf"

        card: dict[str, dict] = {}

        # 1) 상위구성 + 섹터/국가 노출 (etf_constituents)
        etf = etf_analysis.analyze_etf(tk, conn=conn)
        if etf.get("data_connected"):
            card["top_holdings"] = {"status": "connected",
                                    "value": etf["top_holdings"][:5],
                                    "constituent_count": etf["constituent_count"]}
            card["sector_exposure"] = {"status": "connected", "value": etf["sector_exposure"]}
            card["country_exposure"] = {"status": "connected", "value": etf["country_exposure"]}
        else:
            for k in ("top_holdings", "sector_exposure", "country_exposure"):
                card[k] = {"status": "미연동", "value": None,
                           "reason": "etf_constituents 미연동 — 구성/노출 분석 불가."}

        # 2) 기존 보유와 **중복노출** (holdings + 후보 ETF 구성 겹침)
        card["overlap_with_holdings"] = _overlap_with_holdings(tk, account_index, conn=conn)

        # 3) 최근성과 + 하락징후 (price_history)
        hist = price_history.load_history(tk)
        if hist:
            scan = decline_scan.scan_instrument(tk, history=hist)
            if scan.get("ok"):
                card["decline_risk"] = {"status": "connected",
                                        "risk_level": scan.get("risk_level"),
                                        "holistic_risk": scan.get("holistic_risk"),
                                        "overall_confidence": scan.get("overall_confidence")}
            else:
                card["decline_risk"] = {"status": "미연동",
                                        "reason": scan.get("reason", "no_data")}
            perf = _recent_performance(hist)
            card["recent_performance"] = {"status": "connected", "value": perf}
        else:
            card["decline_risk"] = {"status": "미연동", "reason": "가격/일봉 미연동"}
            card["recent_performance"] = {"status": "미연동", "reason": "가격/일봉 미연동"}

        # 4) 거시민감도 (macro_connect, 계좌 공통 — 종목별 민감도는 미연동)
        macro = macro_connect.macro_snapshot(conn=conn)
        card["macro_sensitivity"] = (
            {"status": "context_only",
             "note": "거시 스냅샷은 가용하나 ETF별 민감도(베타)는 미연동 — 추정 금지.",
             "macro_available": True}
            if macro.get("data_available") else
            {"status": "미연동", "reason": "거시 스냅샷 미연동"})

        # 5) 미연동 메타(운용보수/괴리율/추적오차/환헤지/분배금/거래량/기초지수)
        for key, label in _ETF_META_FIELDS.items():
            card[key] = {"status": "미연동", "label": label, "value": None,
                         "reason": f"{label} 메타 미연동 — unknown(추정 금지)."}
    finally:
        if own:
            conn.close()

    connected = [k for k, v in card.items() if v.get("status") == "connected"]
    unconnected = [k for k, v in card.items() if v.get("status") == "미연동"]
    # 데이터 충분성: 구성/노출 connected + 중복노출 계산 가능해야 강한 결론 허용.
    overlap_known = card["overlap_with_holdings"].get("status") == "connected"
    strong_ok = ("top_holdings" in connected) and overlap_known
    return {
        "ok": True, "ticker": tk, "account_index": account_index,
        "is_etf": is_etf_meta or etf.get("data_connected", False),
        "scorecard": card,
        "connected_axes": connected,
        "unconnected_axes": unconnected,
        "strong_conclusion_allowed": strong_ok,
        "headline": ("ETF 스코어카드 — 연동 항목 실측, 미연동은 정직 표기(추정 0)."
                     if connected else
                     "ETF 데이터 대부분 미연동 — 선정 기준 평가 불가(정직). 자료 적재 필요."),
        "note": "선정 기준 점검(읽기 전용). 주문/policy 변경 0. 미연동=unknown(가짜 점수 금지).",
    }


def _account_holding_etf_tickers(account_index: int, *, conn) -> list[str]:
    """계좌 보유 + 관심 ETF 티커(중복노출 계산 대상)."""
    etfs: set[str] = set()
    rows = conn.execute(
        "SELECT ticker, asset_class FROM universe_instruments "
        "WHERE account_index=? AND is_active=1", (account_index,)).fetchall()
    for r in rows:
        if r["asset_class"] and "etf" in r["asset_class"].lower():
            etfs.add(r["ticker"])
    sid = conn.execute(
        "SELECT id FROM account_snapshots WHERE account_index=? ORDER BY id DESC LIMIT 1",
        (account_index,)).fetchone()
    if sid:
        for h in conn.execute("SELECT ticker FROM holdings WHERE snapshot_id=?",
                              (sid["id"],)).fetchall():
            has = conn.execute("SELECT 1 FROM etf_constituents WHERE etf_ticker=? LIMIT 1",
                               (h["ticker"],)).fetchone()
            if has:
                etfs.add(h["ticker"])
    return sorted(etfs)


def _overlap_with_holdings(candidate_etf: str, account_index: int, *, conn) -> dict:
    """후보 ETF 와 **기존 보유/관심 ETF** 간 중복노출 — 겹침 비중 + 집중 플래그.

    20%+ 겹침이면 concentration_flag. 후보 자신은 제외. ETF 구성 미연동이면 정직 미연동.
    """
    holding_etfs = [e for e in _account_holding_etf_tickers(account_index, conn=conn)
                    if e != candidate_etf]
    cand_cons = etf_analysis.load_constituents(candidate_etf, conn=conn)
    if not cand_cons:
        return {"status": "미연동", "value": None,
                "reason": "후보 ETF 구성(etf_constituents) 미연동 — 중복노출 계산 불가."}
    if not holding_etfs:
        return {"status": "connected", "value": [],
                "max_overlap_weight_pct": 0.0, "concentration_flag": False,
                "note": "기존 보유/관심 ETF 없음 — 중복노출 대상 없음."}
    pairs = []
    any_other_data = False
    for other in holding_etfs:
        ov = etf_analysis.overlap(candidate_etf, other, conn=conn)
        if ov.get("data_connected"):
            any_other_data = True
            pairs.append({"with": other,
                          "overlap_weight_pct": ov.get("overlap_weight_pct"),
                          "shared_count": ov.get("shared_count"),
                          "concentration_flag": ov.get("concentration_flag")})
    if not any_other_data:
        return {"status": "미연동", "value": None,
                "reason": "기존 보유 ETF 구성 미연동 — 중복노출 계산 불가."}
    max_ov = max((p["overlap_weight_pct"] or 0.0) for p in pairs) if pairs else 0.0
    return {"status": "connected", "value": pairs,
            "max_overlap_weight_pct": round(max_ov, 2),
            "concentration_flag": max_ov >= 20.0,
            "note": ("기존 보유와 겹침 20%+ — 단일 종목 노출 집중 위험."
                     if max_ov >= 20.0 else "기존 보유와 중복노출 임계 미만.")}


def _recent_performance(hist: list[dict]) -> dict:
    """최근 성과(누적 수익률, %) — 가용 구간으로 정직 계산. 부족하면 unknown."""
    closes = [b["close"] for b in hist if b.get("close")]
    out = {"bars": len(closes)}
    for label, n in (("ret_20d_pct", 20), ("ret_60d_pct", 60)):
        if len(closes) > n and closes[-n - 1]:
            out[label] = round((closes[-1] / closes[-n - 1] - 1.0) * 100, 2)
        else:
            out[label] = None
    return out


# ---------------------------------------------------------------------------
# 최종 후보/대안/제외/추가확인 분류 (추천 아님 — 적합도)
# ---------------------------------------------------------------------------
def _candidate_type_for(bucket: str, asset_class: str | None) -> str:
    if bucket == "treasury":
        return "treasury"
    if bucket == "semiconductor_inverse":
        return "inverse"
    if _is_etf_like(asset_class):
        return "etf"
    return "stock"


def _row_to_candidate_eval(row: dict, *, bucket: str, category: str, reason: str):
    """compare_bucket 행 + 분류 카테고리 → CandidateEvaluation(공통 SSOT).

    additive 정규화 — 기존 출력은 그대로 두고 표준 포맷 view 를 추가 제공한다.
    selection 단계는 비중을 정하지 않으므로 suggested_weight/max_weight=None(가짜 숫자 금지).
    안전 불변식(approval_required=True · auto_order_created=False · auto_applied=False)은 factory 강제.
    """
    conf = row.get("confidence") or {}
    connected = conf.get("connected_axes", 0)
    ev_count = conf.get("evidence_count", 0)
    available = bool(connected or ev_count)
    if not available:
        level = "unavailable"
    elif conf.get("strong_conclusion_allowed"):
        level = "connected"
    else:
        level = "partial"
    include = category in ("final", "alternatives")
    return candidate_evaluation(
        _candidate_type_for(bucket, row.get("asset_class")),
        row.get("ticker"),
        display_name=row.get("name") or row.get("ticker") or "",
        bucket=bucket,
        data_quality={"available": available, "level": level,
                      "connected_axes": connected, "evidence_count": ev_count},
        confidence=conf.get("value", 0.0),
        risk_summary=row.get("decline_risk"),
        evidence_summary=row.get("evidence"),
        reason_to_include=(reason if include else ""),
        reason_to_exclude=("" if include else reason),
    )


def classify_bucket(account_index: int, bucket: str, *, conn=None) -> dict:
    """비교 결과를 '현 정책·관점 기준 적합도'로 분류 — 추천이 아니라 정리.

    - final_candidates: 데이터 충분 + 위험/정합성 적합 (강한 결론 허용분만)
    - alternatives:     비교 가능하나 근거 부족 → 추가 자료 시 승격 가능
    - excluded:         명백한 부적합(예: 인버스를 롱 bucket 에) — 사유 표기
    - need_more_data:   데이터 미연동으로 판단 보류
    **강한 결론 불가면 final 비우고 정직 표기.**
    """
    cmp = compare_bucket(account_index, bucket, conn=conn)
    if not cmp.get("ok"):
        return cmp

    final, alts, excluded, need_more = [], [], [], []
    for r in cmp["comparison"]:
        conf = r["confidence"]
        risk = r["decline_risk"]
        entry = {"ticker": r["ticker"], "name": r.get("name"),
                 "confidence": conf["value"]}
        # 제외: 하락위험 high 가 명확히 측정된 경우(헤지 bucket 제외)
        if risk.get("available") and risk.get("risk_level") == "high" \
                and bucket != "semiconductor_inverse":
            excluded.append({**entry, "reason": "하락 징후 6축 high — 진입 부적합(측정 기반)"})
            continue
        # 제외: 개별주 우량주 필터가 명확히 미달(재무 부실/적자/고평가 — 측정 기반).
        qf = r.get("quality_filter")
        if qf is not None and qf.get("passed") is False:
            excluded.append({**entry, "reason": f"우량주 필터 미달(재무 부실): {qf.get('reason')}"})
            continue
        if conf["connected_axes"] == 0 and conf["evidence_count"] == 0:
            need_more.append({**entry, "reason": "실측/자료 전무 — 판단 보류(정직)"})
            continue
        # 개별주인데 우량주 필터가 데이터 미연동(passed=None)이면 final 승격 금지.
        stock_quality_blocked = qf is not None and qf.get("applicable") and qf.get("passed") is None
        if conf["strong_conclusion_allowed"] and not stock_quality_blocked:
            final.append({**entry, "reason": "데이터·정합성 충분 → 적합 후보(추천 아님)"})
        elif stock_quality_blocked:
            alts.append({**entry, "reason": "개별주 우량주 필터 데이터 미연동 → 재무 자료 보강 시 재평가"})
        else:
            alts.append({**entry, "reason": "비교 가능하나 근거 부족 → 자료 보강 시 승격"})

    # additive 정규화 — 모든 후보를 CandidateEvaluation 공통 포맷으로(기존 키 무변경).
    by_ticker = {r["ticker"]: r for r in cmp["comparison"]}
    normalized = []
    for cat, entries in (("final", final), ("alternatives", alts),
                         ("excluded", excluded), ("need_more_data", need_more)):
        for e in entries:
            row = by_ticker.get(e.get("ticker"),
                                {"ticker": e.get("ticker"), "name": e.get("name")})
            normalized.append(_row_to_candidate_eval(
                row, bucket=bucket, category=cat, reason=e.get("reason", "")))

    return {
        "ok": True, "account_index": account_index, "bucket": bucket,
        "label": cmp["label"],
        "final_candidates": final,
        "alternatives": alts,
        "excluded": excluded,
        "need_more_data": need_more,
        "normalized": normalized,
        "strong_conclusion_possible": cmp["strong_conclusion_possible"],
        "headline": ("적합도 분류(현 정책·관점 기준, 추천 아님)." if cmp["strong_conclusion_possible"]
                     else "데이터 부족 — final 후보 없음(정직). 자료 적재 후 재평가."),
        "note": "자동주문/policy 변경 0. '적합도' 정리일 뿐 매수 지시 아님.",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="종목/ETF 선정 엔진(비교 중심, 읽기 전용)")
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--buckets", action="store_true", help="bucket 목록")
    ap.add_argument("--bucket", help="해당 bucket 후보 + 데이터 가용성")
    ap.add_argument("--compare", help="해당 bucket 비교표")
    ap.add_argument("--classify", help="해당 bucket 적합도 분류")
    ap.add_argument("--quality", help="개별주 우량주 필터(TICKER)")
    ap.add_argument("--scorecard", help="ETF 선정 기준 스코어카드(TICKER)")
    args = ap.parse_args()
    try:
        if args.buckets:
            out = {"ok": True, "buckets": list_buckets()}
        elif args.bucket:
            cb = bucket_candidates(args.account, args.bucket)
            if cb.get("ok"):
                conn = store_db.connect()
                try:
                    cb["data_availability"] = [
                        data_availability(args.account, c, conn=conn) for c in cb["candidates"]]
                finally:
                    conn.close()
            out = cb
        elif args.compare:
            out = compare_bucket(args.account, args.compare)
        elif args.classify:
            out = classify_bucket(args.account, args.classify)
        elif args.quality:
            out = quality_filter(args.quality)
        elif args.scorecard:
            out = etf_scorecard(args.scorecard, args.account)
        else:
            out = {"ok": False, "error": "--buckets | --bucket B | --compare B | --classify B "
                   "| --quality T | --scorecard T"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
