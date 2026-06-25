"""하락 징후 스캔 → 보수적 전환 권고(제안 객체만).

흐름:
  관심종목/보유종목 집합 → 각 종목 price_history → decline_signals.compute_signals →
  종목별 위험점수 + 섹터/지수 집계 → 위험이 높으면 **보수적 전환 권고** 생성.

핵심 규칙(불변):
  - **제안 객체만 반환**. 주문 자동생성 절대 금지(자동매매 금지, 사람 승인).
  - 기존 posture/대전제(cash_band)·allocation 과 **읽기 전용**으로 연결 — 정책을 바꾸지 않고
    "현금/방어 band↑, 위험자산↓, 헤지" 권고만 만든다. 적용은 사람이 정책 저장 경로로.
  - 지능 = 규칙 신호(decline_signals) + Claude+메모리 성장. Anthropic API 미사용.

데이터 없는 종목은 안전하게 skip(NotEnoughData) — 거짓 경보 금지.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import decline_signals as ds
from . import price_history as ph
from .decline import composite as composite_mod
from .decline import context as context_mod
from .store import db as store_db

# 보수적 전환 트리거 (config 의미). 집합 평균/고위험 종목 비율 기준.
SHIFT_THRESHOLDS = {
    "portfolio_risk_elevated": 25.0,  # 집합 평균 위험점수 ≥ 25 → 약한 권고
    "portfolio_risk_high": 45.0,      # ≥ 45 → 강한 권고
    "high_risk_name_frac": 0.34,      # 고위험(high+severe) 종목 비율 ≥ 1/3 → 권고
    # 권고 cash_band 상향 폭(절대 %p) — 위험 수준별
    "cash_bump_elevated_pp": 5.0,
    "cash_bump_high_pp": 10.0,
}

# confidence 별 판단 강도 경계 (config 의미). overall_confidence(6축 메타인지) 기준.
#   < low  : 단정 금지 — 관망/주의, "데이터 추가 필요", 보수전환은 '후보로만'.
#   low~mid: 약한 보수전환 — 현금밴드 소폭 상향 후보.
#   ≥ mid  : 비교적 강한 보수전환 — 단, 항상 사람 승인.
# **confidence 낮은데 강한 조언 금지** (CLAUDE.md §11.8).
# confidence→강도 임계는 candidate.py 가 SSOT. 여기서는 재노출(하위호환 import 유지).
from .candidate import CONFIDENCE_BANDS  # noqa: E402  (SSOT 단일 진실)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def confidence_judgment(overall_confidence: float | None) -> dict:
    """overall_confidence(6축 메타인지) → **판단 강도 가이드**(읽기 전용 제안).

    낮은 confidence 에서 강한 조언이 나오지 않도록 강도를 제한한다.
    반환:
      {
        "tier": "insufficient|weak|moderate",   # 판단 강도 등급
        "assert_ok": bool,                       # 단정(강한 조언) 허용 여부
        "allowed_strength": "candidate_only|weak|moderate",  # 보수전환 허용 강도
        "stance": "관망/주의|약한 보수전환|비교적 강한 보수전환",
        "note": 한글 한 줄,
      }
    confidence 미지정(None) = insufficient 와 동일하게 보수적으로 취급(데이터 추가 필요)."""
    c = overall_confidence
    lo, mid = CONFIDENCE_BANDS["low"], CONFIDENCE_BANDS["mid"]
    if c is None or c < lo:
        return {
            "tier": "insufficient",
            "assert_ok": False,
            "allowed_strength": "candidate_only",
            "stance": "관망/주의",
            "note": (f"신뢰도 {('미상' if c is None else f'{c:.2f}')} (<{lo}) — 단정 금지. "
                     "관망/주의 + 데이터 추가 필요. 보수적 전환은 '후보로만' 제시."),
        }
    if c < mid:
        return {
            "tier": "weak",
            "assert_ok": False,
            "allowed_strength": "weak",
            "stance": "약한 보수전환",
            "note": (f"신뢰도 {c:.2f} ({lo}~{mid}) — 약한 보수전환만. "
                     "현금밴드 소폭 상향 후보 수준(사람 승인)."),
        }
    return {
        "tier": "moderate",
        "assert_ok": True,
        "allowed_strength": "moderate",
        "stance": "비교적 강한 보수전환",
        "note": (f"신뢰도 {c:.2f} (≥{mid}) — 비교적 강한 보수전환 제시 가능(단, 항상 사람 승인)."),
    }


def scan_instrument(instrument_code: str, *, sector: str | None = None,
                    history: list[dict] | None = None,
                    multi_axis: bool = True, as_of_date: str | None = None,
                    axis_context: dict | None = None) -> dict:
    """한 종목 스캔. history 미지정 시 DB(price_history)에서 로드.

    데이터 부족이면 {ok:False, reason:'not_enough_data'} (예외 아님 — 집합 스캔이 멈추지 않게).

    multi_axis=True 면 기술축 외 5축(분산·거시·이벤트·심리·정책)을 포함한 **6축 메타인지
    종합**(decline.composite)을 함께 첨부한다(읽기 전용). 미연동 축은 data_available=False
    로 정직하게 표기되며 가짜 점수를 만들지 않는다. 기존 risk_score(기술축)는 호환을 위해 유지.
    """
    hist = history if history is not None else ph.load_history(instrument_code)
    try:
        res = ds.compute_signals(hist)
    except ds.NotEnoughData as e:
        return {"ok": False, "instrument_code": instrument_code, "sector": sector,
                "reason": "not_enough_data", "detail": str(e)}
    out = {"ok": True, "instrument_code": instrument_code, "sector": sector,
           "risk_score": res["risk_score"], "risk_level": res["risk_level"],
           "fired": res["fired"], "signals": res["signals"], "data_points": res["data_points"]}

    if multi_axis:
        # 6축 메타인지 종합 (읽기 전용). axis_context 미지정이면 DB 에서 축 데이터 로드.
        ctx = axis_context
        if ctx is None:
            try:
                ctx = context_mod.build_context(instrument_code, sector=sector,
                                                history=hist, as_of_date=as_of_date)
            except Exception:  # noqa: BLE001 — 축 데이터 로드 실패해도 기술축 스캔은 유효
                ctx = {"history": hist, "sector": sector, "as_of_date": as_of_date}
        comp = composite_mod.composite(ctx)
        out["composite"] = comp
        out["holistic_risk"] = comp["holistic_risk"]
        out["overall_confidence"] = comp["overall_confidence"]
        # 종목 단위 판단 강도(신뢰도 기반) — 낮으면 단정 금지(읽기 전용 가이드).
        out["confidence_judgment"] = confidence_judgment(comp["overall_confidence"])
    return out


def _aggregate(scanned: list[dict], key: str) -> list[dict]:
    """key(sector 등)별 평균 위험점수 집계 (분석된 종목만)."""
    groups: dict[str, list[dict]] = {}
    for s in scanned:
        if not s.get("ok"):
            continue
        k = s.get(key) or "미분류"
        groups.setdefault(k, []).append(s)
    out = []
    for k, items in groups.items():
        avg = round(sum(i["risk_score"] for i in items) / len(items), 1)
        out.append({key: k, "avg_risk_score": avg, "risk_level": ds.risk_level(avg),
                    "count": len(items),
                    "names": sorted(items, key=lambda i: i["risk_score"], reverse=True)})
    out.sort(key=lambda g: g["avg_risk_score"], reverse=True)
    return out


def _conservative_proposal(scanned_ok: list[dict], current_cash_band: dict | None) -> dict | None:
    """집합 위험 → 보수적 전환 권고(제안 객체). 주문 없음.

    반환 None = 권고 불필요(위험 낮음). 그 외 {action, rationale, suggested_cash_band, ...}.
    """
    if not scanned_ok:
        return None
    avg = sum(s["risk_score"] for s in scanned_ok) / len(scanned_ok)
    high_names = [s for s in scanned_ok if s["risk_level"] in ("high", "severe")]
    high_frac = len(high_names) / len(scanned_ok)

    t = SHIFT_THRESHOLDS
    triggered = (avg >= t["portfolio_risk_elevated"]
                 or high_frac >= t["high_risk_name_frac"])
    if not triggered:
        return None

    # 집합 메타인지 신뢰도(가용 종목 평균) → 판단 강도 캡. 6축 미연동이면 None(보수적 취급).
    confs = [s["overall_confidence"] for s in scanned_ok if s.get("overall_confidence") is not None]
    agg_conf = round(sum(confs) / len(confs), 3) if confs else None
    judgment = confidence_judgment(agg_conf)

    risk_strong = avg >= t["portfolio_risk_high"] or high_frac >= 0.5
    # confidence 가 강한 조언을 허용할 때만 strong 으로(신뢰도 낮으면 강제로 약화 — 강한조언 금지).
    strong = risk_strong and judgment["allowed_strength"] == "moderate"
    bump = t["cash_bump_high_pp"] if strong else t["cash_bump_elevated_pp"]
    # candidate_only(신뢰도 미달)면 현금밴드 상향 폭도 후보 수준으로만(과한 단정 방지).
    if judgment["allowed_strength"] == "candidate_only":
        bump = min(bump, t["cash_bump_elevated_pp"])

    # 현금밴드 상향 권고 (읽기 전용 — 적용은 사람). 현재 밴드 있으면 그 위로, 없으면 절대치 제안.
    cur_min = (current_cash_band or {}).get("min")
    cur_max = (current_cash_band or {}).get("max")
    suggested = None
    if cur_min is not None and cur_max is not None:
        suggested = {"min": round(min(cur_min + bump, 90.0), 1),
                     "max": round(min(cur_max + bump, 95.0), 1),
                     "from": {"min": cur_min, "max": cur_max}}
    else:
        base = 30.0 if strong else 20.0
        suggested = {"min": base, "max": round(base + 15.0, 1), "from": None}

    rationale = []
    rationale.append(f"스캔 종목 평균 위험점수 {avg:.0f} ({ds.risk_level(avg)}).")
    if high_names:
        rationale.append(f"고위험 종목 {len(high_names)}/{len(scanned_ok)}개: "
                         + ", ".join(f"{n['instrument_code']}({n['risk_score']:.0f})" for n in high_names[:5]) + ".")
    # 가장 흔히 발화한 신호
    sig_count: dict[str, int] = {}
    for s in scanned_ok:
        for f in s["fired"]:
            sig_count[f] = sig_count.get(f, 0) + 1
    top = sorted(sig_count.items(), key=lambda kv: kv[1], reverse=True)[:3]
    if top:
        rationale.append("주요 선행신호: " + ", ".join(f"{k}×{v}" for k, v in top) + ".")

    # strength 라벨: 위험은 강한데 신뢰도가 낮으면 'candidate'(후보로만) 로 강등.
    if strong:
        strength = "strong"
    elif judgment["allowed_strength"] == "candidate_only":
        strength = "candidate"
    else:
        strength = "moderate"

    return {
        "action": "shift_conservative",
        "strength": strength,
        "rationale": " ".join(rationale),
        "suggested_cash_band": suggested,          # 현금/방어 band↑ (읽기 전용 제안)
        "reduce_risk_assets": judgment["assert_ok"] or judgment["allowed_strength"] != "candidate_only",
        "consider_hedge": strong,                  # 강한 경우만 헤지(인버스 한도 내) '검토 후보'
        "portfolio_avg_risk": round(avg, 1),
        "high_risk_fraction": round(high_frac, 2),
        "overall_confidence": agg_conf,            # 집합 메타인지 신뢰도(없으면 None)
        "confidence_judgment": judgment,           # 판단 강도 가이드(낮으면 단정 금지)
        "asserted": judgment["assert_ok"],         # 단정(강한 조언) 허용 여부
        "allowed_actions": _allowed_actions(judgment, strong),
        "auto_order_created": False,               # 명시: 주문 자동생성 없음
        "apply_via": "사람 승인 — profile/policy 저장 경로로만 반영(자동 적용 금지)",
    }


def _allowed_actions(judgment: dict, strong: bool) -> list[str]:
    """confidence 강도에 따라 **허용된 운용기준 조정 후보**만 나열(주문 아님).

    절대 금지: '하락 확정' 단정 · 매수/매도 단정 · 자동 적용. 여기 나열은 전부 *후보*다.
    confidence 낮으면 관망/주의로만, 충분하면 보수전환 후보들을 추가."""
    base = ["관망", "리스크 경고"]
    if judgment["allowed_strength"] == "candidate_only":
        base.append("데이터 추가 수집(후보)")
        return base
    # weak/moderate 공통 보수전환 후보(운용기준 조정 — 주문 아님)
    base += [
        "현금밴드 상향(후보)",
        "위험자산 축소(후보)",
        "테마 노출 축소(후보)",
        "신규매수 보류(후보)",
        "리밸런싱 속도 완화(후보)",
    ]
    if strong:
        base.append("헤지 검토(인버스 한도 내, 후보)")
    return base


def scan(instruments: list[dict], *, account_index: int | None = None,
         current_cash_band: dict | None = None, multi_axis: bool = True,
         as_of_date: str | None = None) -> dict:
    """관심/보유 종목 집합 스캔.

    instruments: [{instrument_code, (sector), (index), (history)}].
      history 를 직접 주면 DB 불필요(순수 분석 — 테스트/백테스트).
    current_cash_band: 현재 대전제 cash_band(읽기 전용) — 권고 상향 기준.

    반환: {scanned, by_sector, by_index, proposal(보수적전환|None), summary}
    **제안만**. 주문 0.
    """
    scanned = []
    for inst in instruments:
        scanned.append(scan_instrument(
            inst["instrument_code"], sector=inst.get("sector"),
            history=inst.get("history"), multi_axis=multi_axis,
            as_of_date=as_of_date, axis_context=inst.get("axis_context")))
        # index 키도 보존(집계용)
        if inst.get("index"):
            scanned[-1]["index"] = inst["index"]

    ok = [s for s in scanned if s.get("ok")]
    by_sector = _aggregate(scanned, "sector")
    by_index = _aggregate(scanned, "index")
    proposal = _conservative_proposal(ok, current_cash_band)

    summary = {
        "total": len(instruments),
        "analyzed": len(ok),
        "skipped_no_data": len(scanned) - len(ok),
        "avg_risk_score": round(sum(s["risk_score"] for s in ok) / len(ok), 1) if ok else None,
        "high_risk_count": sum(1 for s in ok if s["risk_level"] in ("high", "severe")),
    }
    return {"ok": True, "account_index": account_index, "scanned_at": _now(),
            "scanned": scanned, "by_sector": by_sector, "by_index": by_index,
            "proposal": proposal, "summary": summary, "auto_order_created": False}


def scan_account_universe(account_index: int) -> dict:
    """계좌의 관심종목(universe_instruments)+보유종목(holdings) 을 DB 에서 모아 스캔.

    cash_band 는 policy 에서 읽어 권고 기준으로만 사용(읽기 전용).
    """
    conn = store_db.connect()
    try:
        uni = conn.execute(
            "SELECT ticker, asset_class FROM universe_instruments WHERE account_index=? AND is_active=1",
            (account_index,)).fetchall()
        snap = conn.execute(
            "SELECT id FROM account_snapshots WHERE account_index=? ORDER BY id DESC LIMIT 1",
            (account_index,)).fetchone()
        holds = []
        if snap:
            holds = conn.execute(
                "SELECT ticker FROM holdings WHERE snapshot_id=?", (snap["id"],)).fetchall()
    finally:
        conn.close()

    codes: dict[str, dict] = {}
    for u in uni:
        codes[u["ticker"]] = {"instrument_code": u["ticker"], "sector": u["asset_class"]}
    for h in holds:
        codes.setdefault(h["ticker"], {"instrument_code": h["ticker"], "sector": None})

    # cash_band 읽기 (읽기 전용)
    cash_band = None
    try:
        from . import policy as policy_mod
        cash_band = policy_mod.compile_policy(account_index).get("cash_band")
    except Exception:
        cash_band = None

    return scan(list(codes.values()), account_index=account_index, current_cash_band=cash_band)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int)
    ap.add_argument("--code", action="append", help="단일 종목 스캔(반복 가능)")
    args = ap.parse_args()
    try:
        if args.code:
            out = scan([{"instrument_code": c} for c in args.code])
        elif args.account is not None:
            out = scan_account_universe(args.account)
        else:
            out = {"ok": False, "error": "--account N | --code TICKER"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
