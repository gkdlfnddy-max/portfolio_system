"""변이별 전략 요약 (Track 2) — 3안(보수/기준/공격) 각각을 사람이 읽을 수 있는 설명으로.

지능 원칙(CLAUDE.md §17): **Anthropic/LLM API 미사용.** 모든 설명은 **규칙 + 실제 allocation 결과**
(selection.options 가 돌려준 실측 rows/precheck/estimate)에서만 생성한다. mock/하드코딩 숫자 금지.

- rows(kind=cash|bond|anchor|tilt|hedge) → 공통 bucket 인터페이스(Track 3 buckets 와 동일):
    cash→pure_cash('순현금'), bond→bond('국채'), anchor→core_etf('글로벌 코어 ETF'),
    tilt→theme('테마'), hedge→hedge('인버스 헤지').
  방어자산 = 순현금 + 국채. 위험자산 = 100 - 방어.
- allocation 이 이미 방향 게이트를 했다(롱 테마만 tilt, 헤지 테마는 kind=hedge).
  여기서는 **재분류하지 않고** 충실히 렌더링만 한다.

  python -m main_mission.portfolio_os.allocation_explain --account 1
"""
from __future__ import annotations

import argparse
import json
import sys

from . import selection as selection_mod

# variant → (한글 라벨, 성향 요지)
_VARIANT_LABEL = {
    "conservative": "보수",
    "base": "기준",
    "aggressive": "공격",
}

# rows.kind → (bucket_type, 라벨, 역할 설명)
_BUCKET_MAP = {
    "cash": ("pure_cash", "순현금", "즉시 매수여력 — 하락 시 방어 및 분할 진입 재원"),
    "bond": ("bond", "국채", "방어자산(현금 family) — 금리·변동성 완충"),
    "anchor": ("core_etf", "글로벌 코어 ETF", "포트폴리오 중심축 — 광범위 분산으로 변동성 완화"),
    "tilt": ("theme", "테마", "성장 기대 — 변동성 노출이 큰 적극 비중"),
    "hedge": ("hedge", "인버스 헤지", "하락 대비 보험 포지션 — 롱과 분리 운용"),
}

_BUCKET_ORDER = ["pure_cash", "bond", "core_etf", "theme", "hedge"]


def _round1(x: float) -> float:
    return round(float(x or 0.0), 1)


def _build_buckets(rows: list[dict]) -> list[dict]:
    """rows → bucket 집계. bond bucket 은 0% 라도 항상 포함(금리방어 가시성)."""
    agg: dict[str, dict] = {}
    for r in rows:
        kind = r.get("kind")
        mapping = _BUCKET_MAP.get(kind)
        if not mapping:
            continue  # 알 수 없는 kind 는 무시(재분류 금지)
        btype, label, role = mapping
        ref = r.get("ref")
        w = _round1(r.get("weight_pct"))
        entry = agg.setdefault(btype, {"bucket_type": btype, "label": label, "role": role,
                                       "pct": 0.0, "_refs": []})
        entry["pct"] = _round1(entry["pct"] + w)
        if ref and w > 0:
            entry["_refs"].append(ref)

    # bond 는 항상 존재해야 한다(없으면 0%).
    if "bond" not in agg:
        btype, label, role = _BUCKET_MAP["bond"]
        agg["bond"] = {"bucket_type": btype, "label": label, "role": role, "pct": 0.0, "_refs": []}

    out: list[dict] = []
    for btype in _BUCKET_ORDER:
        if btype not in agg:
            continue
        e = agg[btype]
        refs = e.pop("_refs")
        if btype == "theme":
            e["explanation"] = (
                f"테마 {len(refs)}종({', '.join(refs)})을 합산 {e['pct']}% 편입 — 기대수익은 크나 변동성 노출."
                if refs else "편입 테마 없음."
            )
        elif btype == "core_etf":
            e["explanation"] = (
                f"{', '.join(refs)} 중심 — 광범위 분산으로 포트폴리오 변동성을 완화합니다."
                if refs else "코어 ETF 비중 없음."
            )
        elif btype == "hedge":
            e["explanation"] = (
                f"인버스 {len(refs)}종({', '.join(refs)}) {e['pct']}% — 하락 대비 보험, 롱과 분리."
                if refs else "헤지 포지션 없음."
            )
        elif btype == "bond":
            e["explanation"] = (
                f"국채 {e['pct']}% — 방어자산(현금 family), 금리·변동성 완충."
                if e["pct"] > 0 else "국채 0% — 금리방어 자산 없음(현금으로만 방어)."
            )
        else:  # pure_cash
            e["explanation"] = (
                f"순현금 {e['pct']}% — 즉시 매수여력이자 하락 방어 재원."
            )
        out.append(e)
    return out


def _summary(label: str, defensive: float, risk: float, buckets: list[dict],
             est: dict) -> str:
    """규칙 기반 한글 핵심 전략 요약 — 실측 숫자만 사용."""
    bmap = {b["bucket_type"]: b for b in buckets}
    core = bmap.get("core_etf", {}).get("pct", 0.0)
    theme = bmap.get("theme")
    theme_pct = theme["pct"] if theme else 0.0
    theme_refs = []
    if theme and theme["pct"] > 0:
        # explanation 에서 테마명 재추출 대신 rows 기반이 더 정확하나, 요약은 개수/비중 중심.
        theme_refs = theme.get("_refs_cache", [])
    hedge = bmap.get("hedge")
    hedge_pct = hedge["pct"] if hedge else 0.0

    parts = [
        f"[{label}안] 방어자산(순현금+국채)을 약 {defensive}%로 두고, "
        f"글로벌 코어 ETF {core}%를 중심축으로 삼습니다."
    ]
    if theme_pct > 0:
        n = theme.get("_theme_count", 0)
        parts.append(
            f"성장 테마를 {n}종, 합산 {theme_pct}% 분산 편입합니다. "
            f"테마 비중이 {'높아' if theme_pct >= 20 else '있어'} 기대수익은 크지만 변동성에 노출됩니다."
        )
    else:
        parts.append("테마 편입은 없거나 미미해 변동성 노출이 제한적입니다.")
    if hedge_pct > 0:
        parts.append(f"하락 대비 인버스 헤지 {hedge_pct}%를 롱과 분리해 보험으로 둡니다.")

    cur_cash = est.get("current_cash_pct")
    rounds = est.get("expected_rebalance_rounds")
    if cur_cash is not None and cur_cash >= 90 and rounds and rounds > 1:
        parts.append(
            f"현재 현금 {cur_cash}%에서 한 번에 진입하지 않고 {rounds}회 분할로 목표비중까지 이동합니다."
        )
    elif rounds and rounds > 1:
        parts.append(f"목표 도달까지 {rounds}회 분할 진입(지정가)으로 이동합니다.")
    return " ".join(parts)


def _suitable_for(variant: str, defensive: float, risk: float) -> str:
    if variant == "conservative":
        return (
            f"방어를 우선하는 분 — 하락장 대비, 원금 변동을 줄이고 싶을 때. "
            f"방어자산 약 {defensive}%로 위험자산({risk}%)을 절제합니다."
        )
    if variant == "aggressive":
        return (
            f"성장을 최대화하려는 분 — 변동성을 감수하고 상승 여력을 키우고 싶을 때. "
            f"위험자산 약 {risk}%로 공격적으로 편입합니다."
        )
    return (
        f"균형을 원하는 분 — 방어({defensive}%)와 성장({risk}%) 사이에서 표준적으로 운용하고 싶을 때."
    )


def _key_risks(variant: str, buckets: list[dict], defensive: float) -> list[str]:
    risks: list[str] = []
    bmap = {b["bucket_type"]: b for b in buckets}
    theme = bmap.get("theme")
    theme_pct = theme["pct"] if theme else 0.0
    hedge = bmap.get("hedge")
    hedge_pct = hedge["pct"] if hedge else 0.0
    bond = bmap.get("bond")
    bond_pct = bond["pct"] if bond else 0.0

    if theme_pct >= 25:
        risks.append(f"테마 총합 {theme_pct}%가 커 특정 섹터 급락 시 손실 폭이 큽니다.")
    elif theme_pct > 0:
        risks.append(f"테마 {theme_pct}%는 고변동 자산 — 단기 가격 흔들림에 노출됩니다.")
    if hedge_pct > 0:
        risks.append("인버스 헤지는 상승장에서 비용(손실)이 되므로 롱과 분리해 관리해야 합니다.")
    if bond_pct == 0:
        risks.append("국채 0% — 금리방어 자산이 없어 방어가 순현금에만 의존합니다.")
    if defensive < 15:
        risks.append(f"방어자산 {defensive}%로 얇아 급락 시 추가 매수 재원이 부족할 수 있습니다.")
    if not risks:
        risks.append("구조적 리스크는 낮으나, 시장 전반 변동에는 항상 노출됩니다.")
    return risks


def _rebalance_reason(est: dict) -> str:
    cur_cash = est.get("current_cash_pct")
    target_cash = est.get("target_cash_pct")
    drift = est.get("expected_drift_pct")
    rounds = est.get("expected_rebalance_rounds")
    total_krw = est.get("expected_rebalance_total_krw")

    why_drift = (
        f"현재 현금 {cur_cash}%에서 목표 현금 {target_cash}%로 이동하므로 "
        f"약 {drift}%p(≈{total_krw:,}원)의 비중 조정이 필요합니다."
        if cur_cash is not None and target_cash is not None and drift is not None
        else "현재 비중과 목표비중의 차이만큼 조정이 필요합니다."
    )
    if rounds and rounds > 1:
        why_rounds = (
            f"한 번에 옮기지 않고 {rounds}회로 분할하는 이유는 1회 주문 한도(one_order)·진입 속도(pace) "
            f"규칙 때문입니다. 시장가가 아닌 지정가로 무릎 지점을 노려 회차마다 나눠 진입합니다."
        )
    else:
        why_rounds = "조정 규모가 1회 한도 이내라 분할 없이 단일 회차로 처리됩니다."
    return why_drift + " " + why_rounds


def explain_options(account_index: int) -> dict:
    """selection.options 의 실측 결과를 변이별 전략 설명으로 변환."""
    base = selection_mod.options(account_index)
    if not base.get("ok"):
        return {"ok": False, "error": base.get("error", "옵션 생성 실패")}

    proposal_id = base.get("proposal_id")
    variants = base.get("variants", {})
    options: dict[str, dict] = {}

    for variant in ("conservative", "base", "aggressive"):
        v = variants.get(variant)
        if not v:
            continue
        rows = v.get("rows", [])
        est = v.get("estimate", {}) or {}

        # bucket 집계 + 테마 개수/이름 캐시(요약/리스크용)
        theme_refs = [r.get("ref") for r in rows if r.get("kind") == "tilt" and _round1(r.get("weight_pct")) > 0]
        buckets = _build_buckets(rows)
        for b in buckets:
            if b["bucket_type"] == "theme":
                b["_refs_cache"] = theme_refs
                b["_theme_count"] = len(theme_refs)

        pure_cash = next((b["pct"] for b in buckets if b["bucket_type"] == "pure_cash"), 0.0)
        bond = next((b["pct"] for b in buckets if b["bucket_type"] == "bond"), 0.0)
        defensive = _round1(pure_cash + bond)
        risk = _round1(100.0 - defensive)

        label = _VARIANT_LABEL.get(variant, variant)
        summary = _summary(label, defensive, risk, buckets, est)
        # 요약/리스크 생성 후 내부 캐시 키 제거(외부 인터페이스 깔끔히)
        for b in buckets:
            b.pop("_refs_cache", None)
            b.pop("_theme_count", None)

        options[variant] = {
            "variant": variant,
            "label": label,
            "summary": summary,
            "suitable_for": _suitable_for(variant, defensive, risk),
            "key_risks": _key_risks(variant, buckets, defensive),
            "rebalance_reason": _rebalance_reason(est),
            "buckets": buckets,
            "defensive_pct": defensive,
            "risk_pct": risk,
            "drift": est.get("expected_drift_pct"),
            "rebalance_total_krw": est.get("expected_rebalance_total_krw"),
            "rounds": est.get("expected_rebalance_rounds"),
        }

    return {"ok": True, "account_index": account_index, "proposal_id": proposal_id, "options": options}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    args = ap.parse_args()
    try:
        out = explain_options(args.account)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
