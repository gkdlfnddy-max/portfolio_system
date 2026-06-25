"""투자전제 → **정책 객체(policy object)** 컴파일 + 버전 저장.

investor_profile(자연어+추출변수) 를 decision engine 이 그대로 쓰는 **정책 객체**로 승격한다.
모든 한도(단일/섹터/국가/통화/개별/인버스/레버리지/1주문)·현금밴드·pace·금지자산을 명시.
버전 관리(portfolio_policies) → decision provenance 가 policy_version 을 남길 수 있게.

  python -m main_mission.portfolio_os.policy --account 1 --compile
  python -m main_mission.portfolio_os.policy --account 1 --get
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .store import db as store_db
from .risk.gate import RiskLimits


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _g(obj, name, default):
    v = getattr(obj, name, default)
    return default if v is None else v


def compile_policy(account_index: int) -> dict:
    limits = RiskLimits()
    conn = store_db.connect()
    try:
        prof = conn.execute("SELECT * FROM investor_profile WHERE account_index=?", (account_index,)).fetchone()
        adv = conn.execute(
            "SELECT title, detail FROM advice_items WHERE account_index=? AND status='accepted' ORDER BY id",
            (account_index,),
        ).fetchall()
    finally:
        conn.close()
    p = dict(prof) if prof else {}
    accepted_advice = [{"title": a["title"], "detail": a["detail"]} for a in adv]

    risk = p.get("risk_tolerance") or "neutral"
    cmin = p.get("cash_min_pct")
    cmax = p.get("cash_max_pct")
    # 현금 목표: 밴드 안에서 성향으로 — 공격=하한, 방어=상한, 중립=중간
    if cmin is not None and cmax is not None:
        cash_target = {"aggressive": cmin, "defensive": cmax}.get(risk, round((cmin + cmax) / 2, 1))
    else:
        cash_target = float(_g(limits, "cash_min_pct", 10.0))

    # 금지자산: 숏 정책이 none 이면 인버스/숏 금지
    forbidden = []
    if (p.get("short_policy") or "") == "none":
        forbidden.append("inverse")

    try:
        region_targets = json.loads(p["region_targets"]) if p.get("region_targets") else {}
    except Exception:
        region_targets = {}

    policy = {
        "account_index": account_index,
        "risk_tolerance": risk,
        "horizon": p.get("horizon"),
        "region_pref": p.get("region_pref"),
        "region_targets": region_targets,
        "bond": {"target_pct": p.get("bond_target_pct"), "duration_pref": p.get("bond_duration_pref")},
        "pace": p.get("rebalance_pace") or "normal",
        "cash_band": {"min": cmin, "max": cmax, "target": cash_target},
        "limits": {
            "single_name_max_pct": float(_g(limits, "single_name_max_pct", 20.0)),
            "sector_max_pct": 30.0,
            "country_max_pct": 70.0,        # 해외 도입 전 기본(국내 100% 허용에 가까움)
            "max_single_country_pct": 70.0, # 단일 국가 집중 한도
            "emerging_market_max_pct": 20.0,# 신흥국 비중 한도
            "currency_max_pct": 80.0,       # KRW 외 노출 상한 (해외 도입 시 의미)
            "individual_cap_pct": p.get("individual_cap_pct"),
            "individual_count": p.get("individual_count"),
            "inverse_max_pct": float(_g(limits, "short_max_pct", 10.0)),
            "leverage_max_pct": float(_g(limits, "leverage_max_pct", 15.0)),
            "one_order_cap_pct": float(_g(limits, "single_order_max_pct", 5.0)),
            "cash_min_pct": float(_g(limits, "cash_min_pct", 10.0)),
        },
        "forbidden_assets": forbidden,
        "accepted_advice": accepted_advice,   # 사람이 반영한 조언 → 정책의 일부(provenance 로 하위 전파)
        "compiled_at": _now(),
    }
    return policy


def save(account_index: int, policy: dict, source: str = "user") -> dict:
    conn = store_db.connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(version),0) AS v FROM portfolio_policies WHERE account_index=?",
            (account_index,),
        ).fetchone()
        version = int(row["v"]) + 1
        conn.execute(
            "INSERT INTO portfolio_policies(account_index, version, policy, source, created_at) VALUES(?,?,?,?,?)",
            (account_index, version, json.dumps(policy, ensure_ascii=False), source, _now()),
        )
        conn.commit()
        return {"ok": True, "account_index": account_index, "version": version, "policy": policy}
    finally:
        conn.close()


def latest(account_index: int) -> dict | None:
    conn = store_db.connect()
    try:
        row = conn.execute(
            "SELECT version, policy, source, created_at FROM portfolio_policies "
            "WHERE account_index=? ORDER BY version DESC LIMIT 1", (account_index,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["policy"] = json.loads(d["policy"])
        return d
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--compile", action="store_true")
    ap.add_argument("--get", action="store_true")
    args = ap.parse_args()
    try:
        if args.compile:
            out = save(args.account, compile_policy(args.account), source="user")
        elif args.get:
            out = {"ok": True, "policy": latest(args.account)}
        else:
            out = {"ok": False, "error": "--compile 또는 --get"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
