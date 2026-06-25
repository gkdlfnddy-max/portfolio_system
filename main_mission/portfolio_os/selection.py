"""target allocation **선택 확정** 서비스.

3안(보수/기준/공격) 중 사람이 1안을 선택 → 그 계좌의 공식 target allocation 으로 확정.
- 선택 전 **risk pre-check**(현금밴드/섹터/인버스·레버리지/1회 조정량/stale 등)
- **예상 drift / 리밸런싱 총량 / 분할 회차** 추정
- append-only 이력(allocation_selections) + 회차 plan(rebalance_plans/steps)
- 재선택·취소 가능, 단 이력 삭제 금지(status 만 변경)

선택되지 않은 안은 참고안으로만, decision 은 선택된 안 기준.

  python -m main_mission.portfolio_os.selection --account 1 --options
  python -m main_mission.portfolio_os.selection --account 1 --select <proposal_id> <variant>
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone

from .store import db as store_db
from .store.constants import STALE_HOURS
from . import policy as policy_mod
from . import allocation as alloc_mod
from . import regionbond
from . import policy_rules

PACE_CAP = {"slow": 3.0, "normal": 5.0, "fast": 5.0}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot(conn, account_index):
    return conn.execute(
        "SELECT id, cash_krw, total_value_krw, captured_at FROM account_snapshots "
        "WHERE account_index=? ORDER BY id DESC LIMIT 1", (account_index,),
    ).fetchone()


def _is_stale(captured_at) -> bool:
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(captured_at)).total_seconds() / 3600
        return age > STALE_HOURS
    except Exception:
        return False


def _effective_limits(account_index, policy_limits: dict) -> dict:
    """계좌별 실효 정책 limits 를 우선 사용하고, 없으면 policy.limits 로 back-compat.

    account_index 가 None 이거나 조회 실패 시 기존 policy.limits 를 그대로 쓴다.
    """
    merged = dict(policy_limits or {})
    if account_index is None:
        return merged
    try:
        eff = policy_rules.effective_policy(account_index)
        for k, v in (eff.get("limits") or {}).items():
            if v is not None:
                merged[k] = v
    except Exception:  # noqa: BLE001 — 실효정책 조회 실패는 back-compat 로 흡수
        pass
    return merged


def precheck(rows: list[dict], policy: dict, stale: bool, account_index: int | None = None) -> dict:
    reasons = []
    status = "pass"
    limits = _effective_limits(account_index, policy.get("limits", {}))
    band = policy.get("cash_band", {})
    cash = next((r["weight_pct"] for r in rows if r["kind"] == "cash"), 0.0)
    tilts = [r for r in rows if r["kind"] == "tilt"]
    sector_max = limits.get("sector_max_pct", 30.0)
    single_max = limits.get("single_name_max_pct", 20.0)
    one_order = limits.get("one_order_cap_pct", 5.0)
    inverse_max = limits.get("inverse_max_pct", 10.0)

    def block(m):
        nonlocal status
        status = "block"
        reasons.append({"level": "block", "msg": m})

    def warn(m):
        nonlocal status
        if status != "block":
            status = "warn"
        reasons.append({"level": "warn", "msg": m})

    # 국채는 현금의 일부(방어) — 현금밴드 검사는 방어 총량(현금+국채) 기준.
    bond_w = round(sum(r["weight_pct"] for r in rows if r["kind"] == "bond"), 1)
    defensive = round(cash + bond_w, 1)
    if band.get("min") is not None and defensive < band["min"]:
        block(f"방어(현금+국채) {defensive}% < 대전제 하한 {band['min']}% (방어 부족)")
    if band.get("max") is not None and defensive > band["max"]:
        warn(f"방어(현금+국채) {defensive}% > 대전제 상한 {band['max']}% (투자 여력 남음)")
    for t in tilts:
        if t["weight_pct"] > sector_max:
            block(f"테마 '{t['ref']}' {t['weight_pct']}% > 섹터 한도 {sector_max}%")
        elif t["weight_pct"] > single_max:
            warn(f"테마 '{t['ref']}' {t['weight_pct']}% > 단일 한도 {single_max}% (개별 매핑 시 주의)")
    # 인버스/헤지 총합 한도 (kind=hedge) — 롱과 분리해 검사
    hedge_total = round(sum(r["weight_pct"] for r in rows if r["kind"] == "hedge"), 1)
    if hedge_total > inverse_max:
        block(f"헤지(인버스) 총합 {hedge_total}% > 숏/인버스 한도 {inverse_max}%")
    # 지역/채권 구조 검증 (regionbond.validate) — 국가집중/신흥국/현금-채권충돌=block, 합계오류=warn
    bond = policy.get("bond") or {}
    rb = regionbond.validate(policy.get("region_targets") or {}, bond.get("target_pct"), band,
                             max_single_country=limits.get("max_single_country_pct", 70.0),
                             emerging_max=limits.get("emerging_market_max_pct", 20.0))
    for x in rb:
        (warn if x["limit"] == "region_sum" else block)(x["detail"])
    # 1회 조정량: 회차 분할(cycle_cap)이 one_order 이하로 보장 → pass.
    invested = round(sum(r["weight_pct"] for r in rows if r["kind"] != "cash"), 1)
    if invested > 100:
        block(f"투자비중 합 {invested}% > 100%")
    if stale:
        block("스냅샷이 오래됨 — 동기화 후 재선택")
    reasons.append({"level": "info", "msg": "종목 단위 qty=0·가격 이상치는 확정 후 의사결정 단계에서 차단(현재 000660 유지)"})
    return {"status": status, "reasons": reasons, "one_order_cap_pct": one_order}


def estimate(rows: list[dict], snapshot, policy: dict) -> dict:
    total = float(snapshot["total_value_krw"] or 0)
    cash = float(snapshot["cash_krw"] or 0)
    cur_cash_pct = (cash / total * 100) if total else 0.0
    cur_invested_pct = round(100 - cur_cash_pct, 1)

    cash_target = next((r["weight_pct"] for r in rows if r["kind"] == "cash"), 0.0)
    invested_target = round(100 - cash_target, 1)
    # 보유 종목의 테마 매핑은 소전제(universe)에서 — 현재는 미보유면 0. 구조적 일방 전개량.
    deploy_pct = round(max(0.0, invested_target - cur_invested_pct), 1)
    total_krw = round(deploy_pct / 100 * total)

    pace = policy.get("pace", "normal")
    cycle_cap = min(policy.get("limits", {}).get("one_order_cap_pct", 5.0), PACE_CAP.get(pace, 5.0))
    positions = [r for r in rows if r["kind"] != "cash" and r["weight_pct"] > 0]
    max_pos = max((r["weight_pct"] for r in positions), default=0.0)
    rounds = max(1, math.ceil(max_pos / cycle_cap)) if max_pos > 0 else 0

    return {
        "expected_drift_pct": deploy_pct,
        "expected_rebalance_total_krw": total_krw,
        "expected_rebalance_rounds": rounds,
        "cycle_cap_pct": cycle_cap,
        "current_cash_pct": round(cur_cash_pct, 1),
        "target_cash_pct": cash_target,
    }


def _variant_rows(conn, account_index, proposal_id, variant):
    rows = conn.execute(
        "SELECT kind, ref, weight_pct FROM target_allocations "
        "WHERE account_index=? AND proposal_id=? AND variant=? ORDER BY id",
        (account_index, proposal_id, variant),
    ).fetchall()
    return [dict(r) for r in rows]


def options(account_index: int) -> dict:
    """현재(또는 새로 생성한) 3안 + 각 안의 precheck/estimate + 현재 선택."""
    conn = store_db.connect()
    try:
        row = conn.execute(
            "SELECT proposal_id FROM target_allocations WHERE account_index=? ORDER BY id DESC LIMIT 1",
            (account_index,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        alloc_mod.generate(account_index)  # 없으면 생성
        conn = store_db.connect()
        try:
            row = conn.execute(
                "SELECT proposal_id FROM target_allocations WHERE account_index=? ORDER BY id DESC LIMIT 1",
                (account_index,),
            ).fetchone()
        finally:
            conn.close()
    proposal_id = row["proposal_id"]

    pol = policy_mod.latest(account_index)
    policy = pol["policy"] if pol else policy_mod.compile_policy(account_index)

    conn = store_db.connect()
    try:
        snap = _snapshot(conn, account_index)
        stale = _is_stale(snap["captured_at"]) if snap else True
        variants = {}
        for v in ("conservative", "base", "aggressive"):
            rows = _variant_rows(conn, account_index, proposal_id, v)
            pc = precheck(rows, policy, stale, account_index)
            est = estimate(rows, snap, policy) if snap else {}
            variants[v] = {"rows": rows, "precheck": pc, "estimate": est}
    finally:
        conn.close()

    return {
        "ok": True, "account_index": account_index, "proposal_id": proposal_id,
        "policy_version": pol["version"] if pol else None,
        "variants": variants, "selected": current(account_index),
    }


def _diff(prev_alloc, new_rows):
    if not prev_alloc:
        return {"first_selection": True}
    prev = {(r["kind"], r.get("ref")): r["weight_pct"] for r in prev_alloc}
    new = {(r["kind"], r.get("ref")): r["weight_pct"] for r in new_rows}
    changes = []
    for k in set(prev) | set(new):
        a, b = prev.get(k, 0), new.get(k, 0)
        if a != b:
            changes.append({"kind": k[0], "ref": k[1], "from": a, "to": b})
    return {"changes": changes}


def select(account_index: int, proposal_id: str, variant: str, *, selected_by: str = "user",
           user_override: int = 0) -> dict:
    conn = store_db.connect()
    try:
        rows = _variant_rows(conn, account_index, proposal_id, variant)
        if not rows:
            return {"ok": False, "error": "해당 proposal/variant 없음"}
        snap = _snapshot(conn, account_index)
        if not snap:
            return {"ok": False, "error": "잔고 스냅샷 없음 — 동기화 필요"}
        stale = _is_stale(snap["captured_at"])
    finally:
        conn.close()

    pol = policy_mod.latest(account_index)
    policy = pol["policy"] if pol else policy_mod.compile_policy(account_index)
    pc = precheck(rows, policy, stale, account_index)
    est = estimate(rows, snap, policy)
    prev = current(account_index)
    prev_alloc = json.loads(prev["allocation"]) if prev else None
    diff = _diff(prev_alloc, rows)

    conn = store_db.connect()
    try:
        # 이전 active → superseded (이력 보존, 삭제 안 함)
        conn.execute("UPDATE allocation_selections SET status='superseded' WHERE account_index=? AND status='active'",
                     (account_index,))
        cur = conn.execute(
            "INSERT INTO allocation_selections(account_index, proposal_id, variant, allocation, policy_version, "
            "account_snapshot_id, expected_drift_pct, expected_rebalance_total_krw, expected_rebalance_rounds, "
            "precheck_status, precheck_reasons, selected_by, user_override, diff, status, selected_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_index, proposal_id, variant, json.dumps(rows, ensure_ascii=False),
             (pol["version"] if pol else None), snap["id"], est["expected_drift_pct"],
             est["expected_rebalance_total_krw"], est["expected_rebalance_rounds"],
             pc["status"], json.dumps(pc["reasons"], ensure_ascii=False), selected_by, user_override,
             json.dumps(diff, ensure_ascii=False), "active", _now()),
        )
        sel_id = cur.lastrowid
        # 선택된 variant 표시
        conn.execute("UPDATE target_allocations SET status='chosen' WHERE account_index=? AND proposal_id=? AND variant=?",
                     (account_index, proposal_id, variant))
        conn.execute("UPDATE target_allocations SET status='archived' WHERE account_index=? AND proposal_id=? AND variant!=?",
                     (account_index, proposal_id, variant))
        conn.commit()
        # 회차 plan/steps 생성은 decision.compute(selected allocation 기준)가 단일 출처로 담당.
        return {"ok": True, "selection_id": sel_id, "precheck": pc, "estimate": est, "diff": diff}
    finally:
        conn.close()


def current(account_index: int, *, conn=None) -> dict | None:
    """확정 배분(truth)의 **단일 SSOT 로더** — allocation_selections(status='active') 최신 1건.

    conn 을 넘기면 그 연결을 재사용한다(prehook 등 동일 트랜잭션). 미지정 시 자체 연결.
    """
    own = conn is None
    conn = conn or store_db.connect()
    try:
        r = conn.execute(
            "SELECT * FROM allocation_selections WHERE account_index=? AND status='active' ORDER BY id DESC LIMIT 1",
            (account_index,),
        ).fetchone()
        return dict(r) if r else None
    finally:
        if own:
            conn.close()


def history(account_index: int, limit: int = 30) -> list:
    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT id, variant, precheck_status, expected_drift_pct, expected_rebalance_rounds, status, selected_by, selected_at "
            "FROM allocation_selections WHERE account_index=? ORDER BY id DESC LIMIT ?", (account_index, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def cancel(account_index: int) -> dict:
    conn = store_db.connect()
    try:
        conn.execute("UPDATE allocation_selections SET status='cancelled' WHERE account_index=? AND status='active'",
                     (account_index,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--options", action="store_true")
    ap.add_argument("--select", nargs=2, metavar=("PROPOSAL", "VARIANT"))
    ap.add_argument("--cancel", action="store_true")
    args = ap.parse_args()
    try:
        if args.options:
            out = options(args.account)
        elif args.select:
            out = select(args.account, args.select[0], args.select[1])
        elif args.cancel:
            out = cancel(args.account)
        else:
            out = {"ok": False, "error": "--options | --select P V | --cancel"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
