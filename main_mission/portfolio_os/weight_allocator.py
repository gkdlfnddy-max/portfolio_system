"""비중 조절 엔진 — **확정안 bucket 한도 안에서** 선택 종목/ETF 에 비중 배분.

CEO 본질(불변):
  "좋아 보이는 걸 많이 담기"가 아니라 **확정안(selected allocation) bucket 한도 안에서
  현명하게 배분**한다. 예) 로봇 theme 8.8% 면 로봇 ETF/종목 합이 8.8% 를 **초과 금지**.
  개별주 bucket 10% 면 개별주 합 10% 초과 금지, 단일 종목 1~2%.

읽는 소스(읽기 전용 — 함수만 호출, 본문 의존 X):
  - selection.current(account)              확정안(= bucket별 weight, **truth**)
  - bond_bucket.defensive_breakdown(account) 순현금/국채/위험 (방어 bucket — equity 배분 대상 아님)
  - security_selection.bucket_candidates    bucket 후보(선택 가능 ticker 검증)
  - user_views.list_views / investor_objective.criteria_for_account  관점 가중(옵션)
  - policy_rules.effective_policy / risk/gate  단일종목 20%·섹터 30%·숏 10% 등 hard 한도

불변 규칙 (CEO):
  - **확정안 = 단일 진실.** 각 bucket 의 weight 합(=100)은 변경하지 않는다(truth frozen).
  - **bucket weight 초과 0.** 한 bucket 의 선택 종목 합은 그 bucket weight 를 넘지 않는다.
  - **총합 100 불변.** 배분은 bucket 내부에서만 일어난다.
  - **승인 전 미반영(draft).** DB write 0, 자동 주문 0, policy 변경 0, secret 0, Anthropic API 0.
  - **단정 금지.** 각 종목 "적정 비중 N%(이유)" 는 제안일 뿐 — requires_user_approval.

CLI:
  python -m main_mission.portfolio_os.weight_allocator --account 1 --confirmed
  python -m main_mission.portfolio_os.weight_allocator --account 1 --picks '{"robotics":["BOTZ"]}'
  python -m main_mission.portfolio_os.weight_allocator --account 1 --individual-options
"""
from __future__ import annotations

import argparse
import json
import sys

from . import selection as selection_mod
from . import bond_bucket as bond_mod
from . import security_selection as sec_mod
from . import policy_rules
from . import user_views
from . import investor_objective


# ---------------------------------------------------------------------------
# bucket 키 매핑 — 확정안 row(kind/ref) → security_selection bucket 키
# ---------------------------------------------------------------------------
# 확정안 rows 의 kind: cash | bond | anchor | tilt | hedge.
#   - anchor  → 글로벌 코어 ETF (global_core bucket)
#   - tilt    → 테마 (robotics/semiconductor/... ref 텍스트로 bucket 매핑)
#   - hedge   → 인버스/헤지 (semiconductor_inverse bucket)
#   - cash/bond → 방어자산(bond_bucket 담당) — equity 배분 대상 아님.
# ref 텍스트로 security_selection BUCKETS 키를 추정. 못 찾으면 정직하게 ref 자체를 bucket 키로 둔다.
ROUND = 1


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _theme_bucket_key(ref: str | None) -> str:
    """tilt ref 텍스트 → security_selection bucket 키(휴리스틱). 못 찾으면 ref 원문."""
    r = _norm(ref)
    if not r:
        return "tilt"
    if any(k in r for k in ("로봇", "robot", "자동화")):
        return "robotics"
    if any(k in r for k in ("반도체", "semi", "soxx", "soxx")):
        return "semiconductor"
    if any(k in r for k in ("코어", "core", "글로벌", "global", "anchor", "앵커")):
        return "global_core"
    return ref  # 매핑 불가 — ref 원문을 bucket 키로(정직)


def _hedge_bucket_key(ref: str | None) -> str:
    r = _norm(ref)
    if any(k in r for k in ("반도체", "semi", "soxs")):
        return "semiconductor_inverse"
    return ref or "hedge"


def confirmed_buckets(account_index: int) -> dict:
    """확정안(selected allocation)을 bucket 단위로 정리 — **배분의 truth**.

    반환:
      {
        ok, account_index, variant, source,
        total_pct(=100 검증),
        buckets: [{key, kind, ref, weight_pct, allocatable}],  # equity 배분 대상 bucket
        defensive: {cash_pct, govbond_pct, ...},               # 방어(배분 대상 아님)
      }
    미확정이면 ok=False(정직) — 확정안 없이 배분 금지(확정안=truth).
    """
    cur = selection_mod.current(account_index)
    if not cur:
        return {"ok": False, "account_index": account_index,
                "error": "확정안(selected allocation) 없음 — 3안 확정 후 배분 가능(확정안=단일 진실).",
                "requires_user_approval": True, "auto_order_created": False}
    alloc = cur.get("allocation")
    rows = json.loads(alloc) if isinstance(alloc, str) else (alloc or [])
    total = round(sum(float(r.get("weight_pct") or 0) for r in rows), ROUND)

    buckets: list[dict] = []
    cash_pct = govbond_pct = 0.0
    for r in rows:
        kind = _norm(r.get("kind"))
        w = round(float(r.get("weight_pct") or 0), ROUND)
        ref = r.get("ref")
        if kind == "cash":
            cash_pct = round(cash_pct + w, ROUND)
            continue
        if kind == "bond":
            govbond_pct = round(govbond_pct + w, ROUND)
            continue
        if kind in ("anchor",):
            key = "global_core"
        elif kind == "tilt":
            key = _theme_bucket_key(ref)
        elif kind == "hedge":
            key = _hedge_bucket_key(ref)
        else:
            key = ref or kind
        if w <= 0:
            continue
        buckets.append({"key": key, "kind": kind, "ref": ref, "weight_pct": w})

    return {
        "ok": True,
        "account_index": account_index,
        "variant": cur.get("variant"),
        "source": f"확정안({cur.get('variant')}) — 단일 진실(frozen)",
        "total_pct": total,
        "total_is_100": total == 100.0,
        "buckets": buckets,
        "defensive": {"cash_pct": cash_pct, "govbond_pct": govbond_pct,
                      "defensive_pct": round(cash_pct + govbond_pct, ROUND)},
    }


# ---------------------------------------------------------------------------
# 한도 로드
# ---------------------------------------------------------------------------
def _limits(account_index: int) -> dict:
    """배분에 쓰는 hard 한도(계좌 실효정책 우선, 폴백 기본값)."""
    base = {"single_name_max_pct": 20.0, "sector_max_pct": 30.0,
            "inverse_max_pct": 10.0, "leverage_max_pct": 15.0}
    try:
        eff = policy_rules.effective_policy(account_index)
        for k, v in (eff.get("limits") or {}).items():
            if v is not None and k in base:
                base[k] = float(v)
    except Exception:  # noqa: BLE001 — 실효정책 실패는 기본값으로 흡수
        pass
    return base


# ---------------------------------------------------------------------------
# 관점 가중 (옵션) — user_views 의 conviction 으로 bucket 내부 가중
# ---------------------------------------------------------------------------
def _view_weights(account_index: int, tickers: list[str]) -> dict[str, float] | None:
    """선택 종목별 conviction(0~1) 기반 가중치. 견해 없으면 None(→ 균등)."""
    try:
        views = user_views.list_views(account_index, status="active")
    except Exception:  # noqa: BLE001
        return None
    conv: dict[str, float] = {}
    for v in views:
        for key in (v.get("ticker"), v.get("etf")):
            if key and key in tickers:
                c = v.get("conviction")
                if c is not None:
                    conv[key] = max(conv.get(key, 0.0), float(c))
    if not conv:
        return None
    # 견해 없는 종목은 평균 conviction 으로(완전 0 방지 — 선택됐으므로 최소 기본).
    avg = sum(conv.values()) / len(conv)
    return {tk: conv.get(tk, avg) for tk in tickers}


# ---------------------------------------------------------------------------
# bucket 내부 분배
# ---------------------------------------------------------------------------
def _distribute(weight_pct: float, tickers: list[str],
                weights: dict[str, float] | None) -> dict[str, float]:
    """bucket weight 를 선택 종목에 분배. **합 = weight_pct (초과 금지)**.

    균등(weights=None) 또는 관점 가중. 반올림 잔차는 마지막 종목이 흡수해 합을 정확히 맞춘다.
    """
    n = len(tickers)
    if n == 0:
        return {}
    if weights:
        tot = sum(max(0.0, weights.get(tk, 0.0)) for tk in tickers)
        if tot <= 0:
            weights = None
    out: dict[str, float] = {}
    acc = 0.0
    for i, tk in enumerate(tickers):
        if i == n - 1:
            w = round(weight_pct - acc, ROUND)  # 잔차 흡수 → 합 정확히 = weight_pct
        elif weights:
            tot = sum(max(0.0, weights.get(t, 0.0)) for t in tickers)
            w = round(weight_pct * max(0.0, weights.get(tk, 0.0)) / tot, ROUND)
        else:
            w = round(weight_pct / n, ROUND)
        out[tk] = w
        acc = round(acc + w, ROUND)
    return out


def _valid_tickers(account_index: int, bucket_key: str, picks: list[str], conn) -> tuple[list[str], list[str]]:
    """picks 중 해당 bucket 의 실제 후보에 존재하는 것만 통과(검증). 나머지는 unknown 으로 분리."""
    cb = sec_mod.bucket_candidates(account_index, bucket_key, conn=conn)
    if not cb.get("ok"):
        return [], list(picks)  # bucket 키가 security_selection 에 없음 → 전부 unknown(정직)
    valid_set = {c["ticker"] for c in cb.get("candidates", [])}
    valid = [tk for tk in picks if tk in valid_set]
    unknown = [tk for tk in picks if tk not in valid_set]
    return valid, unknown


# ---------------------------------------------------------------------------
# 메인: allocate
# ---------------------------------------------------------------------------
def allocate(account_index: int, picks: dict, *, weighting: str = "equal") -> dict:
    """확정안 bucket 한도 안에서 선택 종목/ETF 에 비중 배분 → **draft target holdings**.

    picks = {bucket_key: [ticker, ...]}.  각 bucket 의 **확정안 weight** 를 그 bucket 의
    선택 후보에 분배한다(합 = bucket weight, 초과 금지). 미선택 bucket 은 확정안 weight 를
    그대로 보존(앵커 ETF 등은 자기 자신을 단일 후보로 둔다).

    weighting: 'equal'(균등) | 'view'(관점 conviction 가중).
    반환:
      {
        ok, account_index, variant, source,
        holdings: [{kind, ref, ticker, weight_pct, bucket, bucket_weight_pct, basis}],
        bucket_summary: [{key, kind, weight_pct, allocated_pct, picks, headroom_pct}],
        defensive: {...},
        total_pct(=100 검증), total_is_100,
        over_limit_warnings: [...],   # 한도 초과 시 차단/축소 제안
        auto_order_created: False, requires_user_approval: True, db_write: False,
      }
    """
    cb = confirmed_buckets(account_index)
    if not cb.get("ok"):
        return cb
    picks = picks or {}
    limits = _limits(account_index)
    single_max = limits["single_name_max_pct"]
    sector_max = limits["sector_max_pct"]
    inverse_max = limits["inverse_max_pct"]

    conn = sec_mod.store_db.connect()
    try:
        holdings: list[dict] = []
        bucket_summary: list[dict] = []
        warnings: list[dict] = []

        for b in cb["buckets"]:
            key = b["key"]
            w = b["weight_pct"]
            kind = b["kind"]
            raw_picks = list(dict.fromkeys(picks.get(key, [])))  # 중복 제거, 순서 보존

            valid, unknown = ([], [])
            if raw_picks:
                valid, unknown = _valid_tickers(account_index, key, raw_picks, conn)
                for tk in unknown:
                    warnings.append({"level": "warn", "bucket": key, "ticker": tk,
                                     "msg": f"'{tk}' 는 bucket '{key}' 후보에 없음 — 배분 제외(검증 실패)."})

            if not valid:
                # 선택 없음 → 확정안 weight 를 bucket 대표(ref/ETF)로 보존(미반영분 아님).
                holdings.append({"kind": kind, "ref": b["ref"], "ticker": None,
                                 "weight_pct": w, "bucket": key, "bucket_weight_pct": w,
                                 "basis": "미선택 — 확정안 weight 보존(대표 ETF/미배정)"})
                bucket_summary.append({"key": key, "kind": kind, "weight_pct": w,
                                       "allocated_pct": w, "picks": [], "headroom_pct": 0.0})
                continue

            vw = _view_weights(account_index, valid) if weighting == "view" else None
            dist = _distribute(w, valid, vw)

            # 단일종목 상한(hard) — 인버스는 hedge 한도로 별도 처리(아래 총합).
            for tk, ww in dist.items():
                if kind != "hedge" and ww > single_max:
                    warnings.append({"level": "block", "bucket": key, "ticker": tk,
                                     "msg": f"'{tk}' {ww}% > 단일종목 한도 {single_max}% — 축소/분산 필요.",
                                     "suggest_max_pct": single_max})

            allocated = round(sum(dist.values()), ROUND)
            for tk, ww in dist.items():
                holdings.append({"kind": kind, "ref": b["ref"], "ticker": tk,
                                 "weight_pct": ww, "bucket": key, "bucket_weight_pct": w,
                                 "basis": (f"관점 conviction 가중 (bucket {key} {w}% 내)" if vw else
                                           f"균등 분배 (bucket {key} {w}% 내)")})
            bucket_summary.append({"key": key, "kind": kind, "weight_pct": w,
                                   "allocated_pct": allocated, "picks": valid,
                                   "headroom_pct": round(w - allocated, ROUND)})
            # bucket 초과 자가검증(불변): 분배합은 weight 와 정확히 같아야 함.
            if allocated > w + 0.05:
                warnings.append({"level": "block", "bucket": key,
                                 "msg": f"bucket '{key}' 배분합 {allocated}% > 확정안 {w}% (초과 금지 위반)."})

        # 방어자산(현금/국채)은 equity 배분 대상 아님 — 확정안 그대로 holdings 에 보존.
        dfn = cb["defensive"]
        if dfn["cash_pct"] > 0:
            holdings.append({"kind": "cash", "ref": None, "ticker": None,
                             "weight_pct": dfn["cash_pct"], "bucket": "cash",
                             "bucket_weight_pct": dfn["cash_pct"], "basis": "순현금(방어) — 확정안 보존"})
        if dfn["govbond_pct"] > 0:
            holdings.append({"kind": "bond", "ref": "국채", "ticker": None,
                             "weight_pct": dfn["govbond_pct"], "bucket": "treasury",
                             "bucket_weight_pct": dfn["govbond_pct"], "basis": "국채(방어, 현금의 일부) — 확정안 보존"})

        # --- 횡단 한도 검사 (섹터/테마 과집중, 헤지 총합) ---
        # 같은 bucket key(테마)로 묶인 holdings 합 = 섹터 노출. 확정안 bucket weight 가 이미
        # precheck 에서 섹터한도 검증을 통과했으나, 배분 후에도 재확인(방어).
        sector_expo: dict[str, float] = {}
        for h in holdings:
            if h["kind"] == "tilt":
                sector_expo[h["bucket"]] = round(sector_expo.get(h["bucket"], 0.0) + h["weight_pct"], ROUND)
        for sec, expo in sector_expo.items():
            if expo > sector_max:
                warnings.append({"level": "block", "bucket": sec,
                                 "msg": f"테마/섹터 '{sec}' 노출 {expo}% > 섹터 한도 {sector_max}% — 축소 필요.",
                                 "suggest_max_pct": sector_max})

        hedge_total = round(sum(h["weight_pct"] for h in holdings if h["kind"] == "hedge"), ROUND)
        if hedge_total > inverse_max:
            warnings.append({"level": "block", "bucket": "hedge",
                             "msg": f"헤지/인버스 총합 {hedge_total}% > 숏/인버스 한도 {inverse_max}% — 축소 필요.",
                             "suggest_max_pct": inverse_max})
    finally:
        conn.close()

    # 합계 100 불변 보정 — 확정안이 100을 미세하게 벗어나도(반올림/stale drift) 순현금이 잔차 흡수.
    # (allocation._variant 와 동일 패턴.) 큰 차이(>1%p)는 보정 않고 보존 + total_is_100=False 로 정직 경고.
    pre_total = round(sum(h["weight_pct"] for h in holdings), ROUND)
    if pre_total != 100.0 and abs(pre_total - 100.0) <= 1.0:
        cash_h = next((h for h in holdings if h["kind"] == "cash"), None)
        if cash_h is not None:
            cash_h["weight_pct"] = round(cash_h["weight_pct"] + (100.0 - pre_total), ROUND)
    total = round(sum(h["weight_pct"] for h in holdings), ROUND)
    blocked = [w for w in warnings if w["level"] == "block"]
    return {
        "ok": True,
        "account_index": account_index,
        "variant": cb["variant"],
        "source": cb["source"],
        "weighting": weighting,
        "holdings": holdings,
        "bucket_summary": bucket_summary,
        "defensive": cb["defensive"],
        "total_pct": total,
        "total_is_100": total == 100.0,
        "limits": limits,
        "over_limit_warnings": warnings,
        "blocked": bool(blocked),
        "auto_order_created": False,
        "requires_user_approval": True,
        "db_write": False,
        "note": ("draft target holdings (미반영). 확정안 bucket 한도 안에서 배분 — "
                 "총합 100 불변, bucket 초과 0. 자동주문/policy 변경 없음. "
                 "각 비중은 제안일 뿐 — 사람 승인 필요(단정 아님)."),
    }


# ---------------------------------------------------------------------------
# 개별주 bucket 옵션 (A/B/C) — 위험자산 안에서 배분(추가 비중 아님)
# ---------------------------------------------------------------------------
def individual_bucket_options(account_index: int) -> dict:
    """개별주 bucket 옵션 A/B/C 제안 — **위험자산 안에서** carve(확정안 100 불변).

    개별주 bucket 은 확정안의 위험자산(anchor+tilt+hedge, = 100 − 방어) **안에서** 떼어내는
    것이지 추가 비중이 아니다. 선택은 사용자(제안만). DB write 0.

    옵션:
      A: 없음 — ETF 만.
      B: 5% — 5종 내외, 종목당 ~1% (단일 2% 상한).
      C: 10% — 10종 내외, 종목당 0.5~1.5% (단일 2% 상한).
    위험자산이 부족하면(< 옵션 carve) 정직하게 cap 을 위험자산으로 낮추고 표기.
    """
    cb = confirmed_buckets(account_index)
    if not cb.get("ok"):
        return cb
    risk_pct = round(100.0 - cb["defensive"]["defensive_pct"], ROUND)
    limits = _limits(account_index)
    single_max = limits["single_name_max_pct"]

    def _opt(label, cap, count, per_lo, per_hi):
        eff_cap = round(min(cap, risk_pct), ROUND)
        capped = eff_cap < cap
        per_cap = min(2.0, single_max)  # 개별주 단일 상한(CEO: 1~2%) — 단일종목 한도와 더 작은 값
        return {
            "option": label,
            "individual_cap_pct": eff_cap,
            "requested_cap_pct": cap,
            "capped_to_risk": capped,
            "suggested_count": count,
            "per_name_pct_range": [per_lo, per_hi],
            "per_name_max_pct": per_cap,
            "carve_from": "위험자산(anchor+tilt) — 확정안 100 불변, 추가 비중 아님",
            "note": (f"개별주 {label}: 위험자산 {risk_pct}% 안에서 {eff_cap}% carve, "
                     f"{count}종 내외, 종목당 {per_lo}~{per_hi}% (단일 {per_cap}% 상한)."
                     + (" (위험자산 부족 → cap 축소, 정직 표기.)" if capped else "")),
        }

    return {
        "ok": True,
        "account_index": account_index,
        "variant": cb["variant"],
        "risk_asset_pct": risk_pct,
        "defensive": cb["defensive"],
        "single_name_max_pct": single_max,
        "options": {
            "A": {"option": "A", "individual_cap_pct": 0.0, "suggested_count": 0,
                  "carve_from": "없음 — ETF 만",
                  "note": "개별주 없음(ETF 만). 가장 단순/분산."},
            "B": _opt("B", 5.0, 5, 0.5, 1.5),
            "C": _opt("C", 10.0, 10, 0.5, 1.5),
        },
        "auto_order_created": False,
        "requires_user_approval": True,
        "db_write": False,
        "note": "개별주 옵션은 **제안**이다. 위험자산 안에서 carve(확정안 100 불변). 선택은 사용자.",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="비중 조절 엔진 (확정안 bucket 한도 내 배분, 읽기 전용)")
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--confirmed", action="store_true", help="확정안 bucket 정리")
    ap.add_argument("--picks", help='JSON {"bucket":["ticker",...]} — bucket 내 배분')
    ap.add_argument("--weighting", default="equal", choices=("equal", "view"))
    ap.add_argument("--individual-options", action="store_true", help="개별주 A/B/C 옵션")
    args = ap.parse_args()
    try:
        if args.confirmed:
            out = confirmed_buckets(args.account)
        elif args.picks is not None:
            picks = json.loads(args.picks)
            out = allocate(args.account, picks, weighting=args.weighting)
        elif args.individual_options:
            out = individual_bucket_options(args.account)
        else:
            out = {"ok": False, "error": "--confirmed | --picks JSON | --individual-options"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
