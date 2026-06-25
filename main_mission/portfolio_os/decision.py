"""계좌별 의사결정 — **selected allocation 기반** Portfolio Balance(단기 트레이딩 아님).

핵심 규칙(CEO):
  - 사람이 **선택한 목표 포트폴리오(selected allocation)** 만 사용한다.
  - selected allocation 이 없으면 decision 생성 **hard-block**.
  - 선택되지 않은 3안은 참고용 — decision 에 사용하지 않는다.
  - drift·rebalance plan·주문 후보는 selected allocation 기준으로만.
  - 롱 테마(tilt)와 **헤지(인버스, kind=hedge)** 를 분리. 헤지는 인버스 한도로 검사.
  - stale snapshot 이면 decision 생성 차단.
  - provenance: selected_allocation_id, policy_version, account_snapshot_id, price_snapshot_id 기록.

  python -m main_mission.portfolio_os.decision --account 1
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone

from .store import db as store_db
from .store.constants import STALE_HOURS
from .risk.gate import RiskLimits
from . import policy as policy_mod
from . import selection as selection_mod
from . import regionbond
from . import policy_rules
from .growth import middleware as growth_mw

PACE_CAP = {"slow": 3.0, "normal": 5.0, "fast": 5.0}
SECTOR_MAX_PCT = 30.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _r1(x: float) -> float:
    return round(x, 2)


def _block(reason_code: str, msg: str) -> dict:
    return {"ok": False, "blocked": True, "block_code": reason_code, "error": msg}


def _blocked_shape(reasons: list[str]) -> dict:
    """prehook gate=block → compute() 가 precondition 실패 때 돌려주던 _block shape 로 매핑.

    호출측(daily_review·web·테스트)은 {ok:False, blocked:True, block_code, error} 만 본다.
    prehook reason 텍스트로 가장 근접한 block_code 를 고른다(이중 차단 방지: 본문 가드와 동일 의미)."""
    joined = "; ".join(reasons) if reasons else "prehook gate=block"
    code = "precondition"
    if any("selected allocation" in r or "확정" in r for r in reasons):
        code = "no_selection"
    elif any("스냅샷" in r or "snapshot" in r or "stale" in r for r in reasons):
        code = "stale_snapshot"
    elif any("정책" in r or "policy" in r for r in reasons):
        code = "no_policy"
    elif any("account_id" in r for r in reasons):
        code = "no_account"
    return {"ok": False, "blocked": True, "block_code": code, "error": joined}


def compute(account_index: int) -> dict:
    """선택된 목표 포트폴리오 기준 의사결정. Growth Middleware(run_task) 강제 통과.

    prehook(decision_compute) 가 account_id/policy/selected_allocation/fresh_snapshot 를 게이트하고,
    통과 시에만 본문(_impl)을 실행한다. block 이면 본문 미실행 + 기존 _block shape 반환."""
    def _impl(_inp, _ctx):
        return _compute_impl(account_index)

    out = growth_mw.run_task("decision_compute", "broker-chief", _impl,
                             account_index=account_index, input={"account_index": account_index})
    if out["blocked"]:
        return _blocked_shape(out["reasons"])
    if not out["ok"]:
        # 본문 실행 예외 등 — 기존 main() 의 내부오류 shape 와 동일 계열.
        return {"ok": False, "error": "; ".join(out.get("reasons") or ["내부 오류"])}
    return out["result"]


def _compute_impl(account_index: int) -> dict:
    limits = RiskLimits()
    one_order_cap = float(limits.single_order_max_pct)
    cash_min = float(limits.cash_min_pct)

    # 1) 확정된 목표 포트폴리오(selected allocation) 필수 — 없으면 차단
    sel = selection_mod.current(account_index)
    if not sel:
        return _block("no_selection", "확정된 목표 포트폴리오가 없습니다 — 3안 중 하나를 먼저 선택하세요.")
    if int(sel["account_index"]) != int(account_index):
        return _block("account_mismatch", "선택된 allocation 의 계좌가 일치하지 않습니다.")
    try:
        alloc = json.loads(sel["allocation"])
    except Exception:
        return _block("bad_allocation", "선택된 allocation 데이터가 손상되었습니다.")

    conn = store_db.connect()
    try:
        snap = conn.execute(
            "SELECT id, cash_krw, total_value_krw, captured_at FROM account_snapshots "
            "WHERE account_index=? ORDER BY id DESC LIMIT 1", (account_index,),
        ).fetchone()
        if not snap:
            return _block("no_snapshot", "잔고 스냅샷이 없습니다 — 동기화 먼저.")
        # stale 이면 차단 (애매하면 차단: 파싱 실패도 stale 취급 — fail-closed)
        try:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(snap["captured_at"])).total_seconds() / 3600
            stale = age_h > STALE_HOURS
        except Exception:
            age_h, stale = None, True
        if stale:
            return _block("stale_snapshot", f"스냅샷이 오래됨({round(age_h,1) if age_h else '?'}h) — 동기화 후 재계산.")

        total = float(snap["total_value_krw"] or 0)
        cash = float(snap["cash_krw"] or 0)

        prof = conn.execute("SELECT rebalance_pace FROM investor_profile WHERE account_index=?",
                            (account_index,)).fetchone()
        holdings = conn.execute("SELECT ticker, market_value FROM holdings WHERE snapshot_id=?", (snap["id"],)).fetchall()
        # 가격 스냅샷 id (있으면 — 최신 quotes). 없으면 None(정직).
        prow = conn.execute("SELECT MAX(id) AS pid FROM quotes").fetchone()
        price_snapshot_id = prow["pid"] if prow else None

        pace = (prof["rebalance_pace"] if prof else None) or "normal"
        cycle_cap = min(one_order_cap, PACE_CAP.get(pace, 5.0))

        pol = policy_mod.latest(account_index)
        policy = pol["policy"] if pol else policy_mod.compile_policy(account_index)
        # 계좌별 실효 정책(policy_type 별 default + override) 한도를 우선, 없으면 policy.limits.
        plimits = dict(policy.get("limits", {}))
        try:
            eff = policy_rules.effective_policy(account_index)
            for k, v in (eff.get("limits") or {}).items():
                if v is not None:
                    plimits[k] = v
        except Exception:  # noqa: BLE001 — 실효정책 조회 실패는 back-compat 흡수
            pass
        sector_max = plimits.get("sector_max_pct", SECTOR_MAX_PCT)
        inverse_max = plimits.get("inverse_max_pct", 10.0)
        # one_order_cap 도 실효 정책 우선(pace 분할 cap 과 결합)
        eff_one_order = plimits.get("one_order_cap_pct")
        if eff_one_order is not None:
            one_order_cap = float(eff_one_order)
            cycle_cap = min(one_order_cap, PACE_CAP.get(pace, 5.0))
        band = policy.get("cash_band", {})

        cash_cur = (cash / total * 100) if total else 0.0
        # 현재 보유의 테마/헤지 노출 매핑은 소전제(universe) theme 태깅 후 — 현재 미태깅이면 0.
        held_total = sum(float(h["market_value"] or 0) for h in holdings)
        cur_invested_pct = round((held_total / total * 100) if total else 0.0, 1)

        # 위험#5: 목표비중 사용 전 합계 ≈ 100 검증. 벗어나면 violation 기록(차단 신호).
        alloc_sum = round(sum(float(el["weight_pct"]) for el in alloc), 1)
        alloc_sum_ok = abs(alloc_sum - 100.0) <= 0.5

        # 2) 목표 = selected allocation 의 각 요소. drift = 현재 - 목표.
        lines = []
        for el in alloc:
            kind = el["kind"]
            ref = el.get("ref") or ("현금" if kind == "cash" else kind)
            tgt = float(el["weight_pct"])
            if kind == "cash":
                cur = round(cash_cur, 1)
            elif kind == "anchor":
                cur = cur_invested_pct  # 미태깅 시 보유 전체를 광범위로 근사
            else:
                cur = 0.0  # 테마/헤지 현재 노출(미태깅) — 소전제 매핑 후 정밀화
            drift = _r1(cur - tgt)
            band_w = _r1(min(cycle_cap, tgt * 0.25)) if tgt else 0.0
            needs = kind != "cash" and abs(drift) > band_w and abs(drift) > 0.1
            line = {"kind": kind, "ref": ref, "role": ("hedge" if kind == "hedge" else
                    "bond" if kind == "bond" else
                    "long" if kind in ("tilt", "anchor") else "cash"),
                    "current_pct": round(cur, 1), "target_pct": round(tgt, 1), "drift": drift, "band": band_w,
                    "needs_adjust": needs}
            if needs:
                direction = "매도" if drift > 0 else "매수"
                total_pct = _r1(abs(drift))
                cyc = _r1(min(total_pct, cycle_cap))
                line.update({
                    "direction": direction, "total_adjust_pct": total_pct,
                    "total_adjust_krw": round(total_pct / 100 * total),
                    "this_cycle_pct": cyc, "this_cycle_krw": round(cyc / 100 * total),
                    "remaining_pct": _r1(total_pct - cyc), "split_rounds": max(1, math.ceil(total_pct / cycle_cap)),
                    "hold_note": "지정가보다 불리하면 이번 회차 보류 → 다음 사이클 재평가",
                })
            lines.append(line)

        # 3) 리스크 게이트 — selected allocation 기준 (잘못된 이동 방지)
        violations = []
        cash_tgt = round(sum(float(el["weight_pct"]) for el in alloc if el["kind"] == "cash"), 1)
        bond_tgt = round(sum(float(el["weight_pct"]) for el in alloc if el["kind"] == "bond"), 1)
        # 국채는 현금의 일부(방어) — 현금밴드 검사는 방어 총량(현금+국채) 기준.
        defensive_tgt = round(cash_tgt + bond_tgt, 1)
        if band.get("min") is not None and defensive_tgt < float(band["min"]):
            violations.append({"limit": "cash_band_min", "observed": defensive_tgt, "threshold": float(band["min"]),
                               "detail": "방어(현금+국채)가 대전제 하한 미만"})
        if band.get("max") is not None and defensive_tgt > float(band["max"]):
            violations.append({"limit": "cash_band_max", "observed": defensive_tgt, "threshold": float(band["max"]),
                               "detail": "방어(현금+국채)가 대전제 상한 초과 — 투자 여력 남음"})
        # 섹터/테마 집중 (롱 tilt 만)
        for el in alloc:
            if el["kind"] == "tilt" and float(el["weight_pct"]) > sector_max:
                violations.append({"limit": "sector_max_pct", "observed": float(el["weight_pct"]),
                                   "threshold": sector_max, "detail": f"테마 '{el.get('ref')}' 집중 과도"})
        # 위험#5: 목표비중 합계가 100(±0.5)이 아니면 violation (잘못된 목표로 주문 생성 방지)
        if not alloc_sum_ok:
            violations.append({"limit": "alloc_sum_100", "observed": alloc_sum, "threshold": 100.0,
                               "detail": "selected allocation 목표비중 합계가 100%(±0.5) 아님"})
        # 인버스/헤지 총합 한도 — kind=hedge 합계
        hedge_total = round(sum(float(el["weight_pct"]) for el in alloc if el["kind"] == "hedge"), 1)
        if hedge_total > inverse_max:
            violations.append({"limit": "inverse_max_pct", "observed": hedge_total, "threshold": inverse_max,
                               "detail": "헤지(인버스) 총합이 숏/인버스 한도 초과"})

        # F#6: net/gross/hedge_ratio 노출 (포트폴리오 합계 기준).
        #   방어(현금+국채)는 노출 계산에서 제외.
        #   long = anchor + tilt(롱 tilt), short = hedge(인버스/숏).
        long_total = round(sum(float(el["weight_pct"]) for el in alloc if el["kind"] in ("anchor", "tilt")), 1)
        short_total = round(hedge_total, 1)
        net_exposure_pct = round(long_total - short_total, 1)            # 순노출 = 롱 − 숏
        gross_exposure_pct = round(long_total + short_total, 1)          # 총노출 = |롱| + |숏|
        hedge_ratio_pct = round((short_total / long_total * 100), 1) if long_total > 0 else 0.0
        # 지역/채권 구조 검증 (regionbond.validate — 포트폴리오 이동 방향 검증 기준)
        region_targets = policy.get("region_targets") or {}
        bond_pol = policy.get("bond") or {}
        violations.extend(regionbond.validate(
            region_targets, bond_pol.get("target_pct"), band,
            max_single_country=plimits.get("max_single_country_pct", 70.0),
            emerging_max=plimits.get("emerging_market_max_pct", 20.0),
        ))

        result = {
            "ok": True,
            "account_index": account_index,
            "selected_variant": sel["variant"],
            "total_value_krw": total,
            "cash_current_pct": _r1(cash_cur),
            "cash_target_pct": _r1(cash_tgt),
            "lines": lines,
            "long_count": sum(1 for l in lines if l["role"] == "long" and l["needs_adjust"]),
            "hedge_count": sum(1 for l in lines if l["role"] == "hedge"),
            "hedge_total_pct": hedge_total,
            "alloc_sum_pct": alloc_sum,
            "net_exposure_pct": net_exposure_pct,
            "gross_exposure_pct": gross_exposure_pct,
            "hedge_ratio_pct": hedge_ratio_pct,
            "risk": {"passed": len(violations) == 0, "violations": violations},
            "provenance": {
                "selected_allocation_id": sel["id"],
                "policy_version": (pol["version"] if pol else None),
                "account_snapshot_id": snap["id"],
                "price_snapshot_id": price_snapshot_id,
                "pace": pace, "cycle_cap_pct": cycle_cap,
                "risk_policy": {"sector_max_pct": sector_max, "inverse_max_pct": inverse_max,
                                "cash_band": band, "one_order_cap_pct": one_order_cap},
            },
            "snapshot_at": snap["captured_at"],
            "note": "확정 목표(selected allocation) 기준. 한 번에 다 맞추지 않고 이번 회차(분할)만. 종목 단위 실행·qty=0 차단은 주문 단계.",
            "computed_at": _now(),
        }
        dcur = conn.execute("INSERT INTO decisions(account_index, payload, created_at) VALUES(?,?,?)",
                            (account_index, json.dumps(result, ensure_ascii=False), _now()))
        decision_id = dcur.lastrowid
        result["decision_id"] = decision_id

        # 4) 회차 plan — selected allocation 기준
        pcur = conn.execute(
            "INSERT INTO rebalance_plans(account_index, decision_id, pace, summary, created_at) VALUES(?,?,?,?,?)",
            (account_index, decision_id, pace,
             json.dumps({"from": "selected_allocation", "selected_allocation_id": sel["id"],
                         "cycle_cap_pct": cycle_cap}, ensure_ascii=False), _now()))
        plan_id = pcur.lastrowid
        for ln in lines:
            if not ln.get("needs_adjust"):
                continue
            conn.execute(
                "INSERT INTO rebalance_plan_steps(plan_id, ticker, direction, total_pct, total_krw, cycle_pct, "
                "cycle_krw, cycle_qty, remaining_pct, round_no, total_rounds, limit_price, status, reason, role, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, ln["ref"], ln.get("direction"), ln.get("total_adjust_pct"), ln.get("total_adjust_krw"),
                 ln.get("this_cycle_pct"), ln.get("this_cycle_krw"), None, ln.get("remaining_pct"), 1,
                 ln.get("split_rounds"), None, "candidate", "구조적 전개(종목 매핑은 소전제)", ln["role"], _now()),
            )
        result["plan_id"] = plan_id
        conn.commit()
        return result
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    args = ap.parse_args()
    try:
        out = compute(args.account)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
