"""방어자산 내부 구성 — 순현금 / 단기국채 / 장기국채 분해 + 국채 ETF 후보.

CEO 방침(불변):
  - 채권은 **국채만(government_only)**. 회사채/하이일드/신흥국채/복잡상품 금지.
  - 채권은 **현금의 일부(방어자산 family)** — 위험자산에서 빼지 않고 현금밴드에서 carve.
  - bond_target_pct = **방어자산(현금밴드) 대비 국채 비율(0~100)**, 전체 대비 %가 아니다.
    예) 방어 40, 국채비율 40% → 국채 16(전체%), 순현금 24, 위험 60.
  - 듀레이션 mixed 면 기본 **단기50/장기50** (bond_duration_split 로 사용자 변경 가능, 합 100).

이 모듈은 **계산·후보 제시 전용**이다. 자동 주문/policy 변경은 일절 하지 않는다.
국채 ETF 후보는 **시드(seed) 목록**일 뿐 — 실시간 지표/가격은 미연동이며, 각 항목에
'후보·검증 필요·데이터 미연동' 을 정직하게 표기한다(가짜 지표 금지).

  python -m main_mission.portfolio_os.bond_bucket --account 1
"""
from __future__ import annotations

import argparse
import json
import sys

from . import regionbond

# --- 국채 ETF 후보 (seed only) ----------------------------------------------
# CEO 방침: government_only. 아래는 **실재 티커 시드**이지만 지표/가격은 **미연동**이다.
# 어떤 수익률·보수율·듀레이션 수치도 임의로 적지 않는다(가짜 지표 금지).
# 실제 사용 전 반드시 (1) 상장/거래 여부 (2) 정확한 만기대(duration band)
# (3) 보수율/유동성 을 KIS/공식 데이터로 검증해야 한다.
_DISCLAIMER = "후보·검증 필요·데이터 미연동"

_GOV_BOND_ETF_SEED = [
    # 미국 국채 (US Treasury) — 만기대별 (iShares 실 티커)
    {"ticker": "SHY", "name": "iShares 1-3Y Treasury (미국 단기국채 후보)",
     "region": "미국", "duration_band": "short", "bond_type": "government"},
    {"ticker": "IEF", "name": "iShares 7-10Y Treasury (미국 중기국채 후보)",
     "region": "미국", "duration_band": "intermediate", "bond_type": "government"},
    {"ticker": "TLT", "name": "iShares 20Y+ Treasury (미국 장기국채 후보)",
     "region": "미국", "duration_band": "long", "bond_type": "government"},
    # 한국 국채 (KTB) — 만기대별 (KRX 상장 실 종목코드, WebSearch 확인 2026-06).
    #   ※ 종목코드는 확인했으나 보수율/유동성/잔존만기 등 지표는 미연동 — 검증 필요(정직).
    {"ticker": "153130", "name": "KODEX 단기채권 (한국 단기국채 후보)",
     "region": "한국", "duration_band": "short", "bond_type": "government",
     "isin": "KR7153130000"},
    {"ticker": "114260", "name": "KODEX 국고채3년 (한국 단기~중기 국채 후보)",
     "region": "한국", "duration_band": "short", "bond_type": "government",
     "isin": "KR7114260003"},
    {"ticker": "471230", "name": "KODEX 국고채10년액티브 (한국 중기국채 후보)",
     "region": "한국", "duration_band": "intermediate", "bond_type": "government",
     "isin": "KR7471230003"},
    {"ticker": "439870", "name": "KODEX 국고채30년액티브 (한국 장기국채 후보)",
     "region": "한국", "duration_band": "long", "bond_type": "government",
     "isin": "KR7439870007"},
    {"ticker": "451530", "name": "TIGER 국고채30년스트립액티브 (한국 장기국채 후보)",
     "region": "한국", "duration_band": "long", "bond_type": "government",
     "isin": "KR7451530000"},
]


def govbond_etf_candidates(duration_pref: str | None = None,
                           region: str | None = None) -> list[dict]:
    """국채 ETF **후보** 목록(seed). government_only 만. 지표/가격 미연동(정직 표기).

    duration_pref/region 으로 필터(미지정 시 전체). mixed 는 short+long 만 노출.
    """
    dp = (duration_pref or "").strip().lower()
    rg = (region or "").strip()
    wanted_bands: set[str] | None
    if dp == "mixed":
        wanted_bands = {"short", "long"}
    elif dp in ("short", "intermediate", "long"):
        wanted_bands = {dp}
    else:
        wanted_bands = None  # 전체

    out: list[dict] = []
    for e in _GOV_BOND_ETF_SEED:
        if e["bond_type"] != "government":   # 방어: 국채만 (불변)
            continue
        if wanted_bands is not None and e["duration_band"] not in wanted_bands:
            continue
        if rg and e["region"] != rg:
            continue
        out.append({**e,
                    "status": _DISCLAIMER,
                    "data_connected": False,
                    "note": "종목코드는 KRX 상장 실 티커. 보수율/유동성/잔존만기 지표는 미연동 — 검증 필요."})
    return out


def _cash_band(account_index: int) -> tuple[float, float, float]:
    """계좌 현금밴드(min/max/target=방어 총량).
    사용자 프로필 기반 compile_policy.cash_band 를 우선 사용(allocation/perspective 와 동일 소스 — 일관성).
    예: 프로필 현금 40/40 → target 40 → 방어 40(=국채+순현금). 폴백: risk_limits."""
    try:
        from . import policy as _policy
        cb = (_policy.compile_policy(account_index) or {}).get("cash_band") or {}
        cmin, cmax, tgt = cb.get("min"), cb.get("max"), cb.get("target")
        if cmin is not None and cmax is not None:
            cmin, cmax = float(cmin), float(cmax)
            if cmin > cmax:
                cmin, cmax = cmax, cmin
            target = float(tgt) if tgt is not None else round((cmin + cmax) / 2, 1)
            return cmin, cmax, target
    except Exception:  # noqa: BLE001 — compile_policy 실패 시 risk_limits 폴백
        pass
    from . import policy_rules
    pol = policy_rules.effective_policy(account_index)
    limits = pol.get("limits", {}) or {}
    cmin = limits.get("cash_min_pct")
    cmax = limits.get("cash_max_pct")
    cmin = 10.0 if cmin is None else float(cmin)
    cmax = 40.0 if cmax is None else float(cmax)
    if cmin > cmax:
        cmin, cmax = cmax, cmin
    target = round((cmin + cmax) / 2, 1)
    return cmin, cmax, target


def compute_breakdown(defensive_pct: float, bond_ratio_pct: float,
                      duration_pref: str | None,
                      duration_split: dict | None = None) -> dict:
    """방어 총량(절대%) + 국채비율(방어 대비%) → 순현금/단기국채/장기국채 절대%(전체 기준).

    allocation._variant 의 carve 로직과 **동일**: 국채는 위험자산에서 빼지 않고 방어에서 carve.
      defensive = pure_cash + govbond,  govbond = defensive × ratio/100,  risk = 100 − defensive
    듀레이션:
      - short/intermediate/long → 해당 band 에 국채 전량
      - mixed → duration_split({short,long} 합100, 기본 50/50) 로 단기/장기 분할
    """
    defensive = round(max(0.0, min(100.0, float(defensive_pct or 0.0))), 1)
    ratio = round(max(0.0, min(100.0, float(bond_ratio_pct or 0.0))), 1)
    govbond = round(defensive * ratio / 100.0, 1)
    pure_cash = round(defensive - govbond, 1)
    risk = round(100.0 - defensive, 1)

    dp = (duration_pref or "").strip().lower() or None
    short_bond = long_bond = mid_bond = 0.0
    if govbond > 0:
        if dp == "mixed":
            sp = duration_split or {"short": 50.0, "long": 50.0}
            s = float(sp.get("short", 50.0)); l = float(sp.get("long", 50.0))
            tot = s + l if (s + l) > 0 else 100.0
            short_bond = round(govbond * s / tot, 1)
            long_bond = round(govbond - short_bond, 1)   # 잔여를 장기로 흡수(합 일치)
        elif dp == "long":
            long_bond = govbond
        elif dp == "intermediate":
            mid_bond = govbond
        else:  # short 또는 미지정 기본 → 단기
            short_bond = govbond

    out = {
        "defensive_bucket_pct": defensive,
        "bond_ratio_pct": ratio,            # 방어 대비 국채 비율
        "govbond_pct": govbond,             # 국채 전체% (절대)
        "pure_cash_pct": pure_cash,         # 순현금 전체%
        "risk_asset_pct": risk,             # 위험자산 전체%
        "duration_pref": dp,
        "short_govbond_pct": short_bond,
        "intermediate_govbond_pct": mid_bond,
        "long_govbond_pct": long_bond,
        "bond_allowed_types": regionbond.BOND_ALLOWED_DEFAULT,
    }
    # 불변식 자가검증: 순현금 + 국채 = 방어, 방어 + 위험 = 100, 듀레이션 분할 합 = 국채.
    assert round(pure_cash + govbond, 1) == defensive, out
    assert round(defensive + risk, 1) == 100.0, out
    assert round(short_bond + mid_bond + long_bond, 1) == govbond, out
    return out


def _confirmed_defensive(account_index: int) -> dict | None:
    """확정된 selected allocation 의 방어 구성(순현금+국채). CEO 결정: **확정안 = 단일 진실(frozen)**.
    종목선정·주문은 프로필 base 가 아니라 *확정안*을 읽어야 한다. 미확정이면 None(프로필 미리보기 폴백)."""
    try:
        from . import selection
        cur = selection.current(account_index)
        if not cur:
            return None
        alloc = cur.get("allocation")
        rows = json.loads(alloc) if isinstance(alloc, str) else (alloc or [])
        cash = sum(float(r.get("weight_pct") or 0) for r in rows if r.get("kind") == "cash")
        bond = sum(float(r.get("weight_pct") or 0) for r in rows if r.get("kind") == "bond")
        defensive = round(cash + bond, 1)
        ratio = round(bond / defensive * 100, 1) if defensive > 0 else 0.0
        return {"defensive_pct": defensive, "bond_ratio_pct": ratio, "variant": cur.get("variant")}
    except Exception:  # noqa: BLE001 — 확정안 조회 실패는 프로필 미리보기로 폴백
        return None


def defensive_breakdown(account_index: int) -> dict:
    """방어자산 내부 구성 + 국채 ETF 후보(계산 전용).

    **확정안(selected allocation)이 있으면 그 방어 구성을 truth 로 사용**(CEO: 확정안=단일 진실).
    미확정이면 프로필 base 로 미리보기(정직 표기). 국채만(government_only).
    """
    from . import profile as profile_mod
    prof = profile_mod.get(account_index) or {}

    cmin, cmax, target = _cash_band(account_index)
    bond_ratio = float(prof.get("bond_target_pct")) if prof.get("bond_target_pct") is not None else 0.0
    confirmed = _confirmed_defensive(account_index)
    if confirmed is not None:
        target = confirmed["defensive_pct"]        # 확정안 방어 총량(순현금+국채)
        bond_ratio = confirmed["bond_ratio_pct"]   # 확정안 국채 비율(방어 대비%)
        source = f"확정안({confirmed['variant']}) 기준 — 단일 진실"
    else:
        source = "미확정 — 프로필 기준 미리보기(3안 확정 시 확정안이 truth)"
    duration = (prof.get("bond_duration_pref") or None)

    split = None
    raw_split = prof.get("bond_duration_split")
    if (duration or "").strip().lower() == "mixed":
        if isinstance(raw_split, str) and raw_split.strip():
            try:
                split = json.loads(raw_split)
            except (ValueError, TypeError):
                split = None
        if not split:
            split = {"short": 50.0, "long": 50.0}   # 기본 단기50/장기50

    breakdown = compute_breakdown(target, bond_ratio, duration, split)

    warnings: list[str] = []
    allowed = (prof.get("bond_allowed_types") or regionbond.BOND_ALLOWED_DEFAULT)
    if str(allowed).strip().lower() != regionbond.BOND_ALLOWED_DEFAULT:
        warnings.append(f"bond_allowed_types='{allowed}' — 국채만(government_only)으로 강제. 비국채 무시.")
    # 저장된 자유텍스트에 비국채 의도가 남아 있으면 정직하게 표기.
    intent = (prof.get("posture_text") or "") + " " + (prof.get("views_text") or "")
    non_gov = regionbond.detect_non_government_bonds(intent)
    if non_gov:
        warnings.append("비국채 의도 감지(반영 안 함): " + ", ".join(non_gov))

    return {
        "ok": True,
        "account_index": account_index,
        "source": source,                   # '확정안 기준' vs '미확정 미리보기' (정직 표기)
        "confirmed": confirmed is not None,
        "cash_band": {"min": cmin, "max": cmax, "target": target},
        "breakdown": breakdown,
        "duration_split": split,            # mixed 일 때만 (단기/장기 합100)
        "govbond_etf_candidates": govbond_etf_candidates(duration),
        "bond_allowed_types": regionbond.BOND_ALLOWED_DEFAULT,
        "warnings": warnings,
        "note": "계산·후보 제시 전용(자동 주문/policy 변경 없음). ETF 후보 지표는 미연동 — 검증 필요.",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int)
    ap.add_argument("--candidates-only", action="store_true")
    ap.add_argument("--duration", help="short|intermediate|long|mixed (후보 필터)")
    args = ap.parse_args()
    try:
        if args.candidates_only:
            out = {"ok": True, "govbond_etf_candidates": govbond_etf_candidates(args.duration)}
        elif args.account is not None:
            out = defensive_breakdown(args.account)
        else:
            out = {"ok": False, "error": "--account 또는 --candidates-only 가 필요합니다."}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
