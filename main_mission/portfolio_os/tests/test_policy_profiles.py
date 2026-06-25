"""Effective policy per policy_type (Track A) — 유연 한도 + hard rule 불가침 검증.

키 없이 임시 SQLite로 전 경로 검증. (Anthropic API 미사용 — 순수 메모리/안전/추적 토대)
임시 SQLITE_PATH 를 import 전에 주입 → setup() 에서 store_db.init().
"""
from __future__ import annotations

import json
import os
import tempfile

_TMP = os.path.join(tempfile.gettempdir(), "portfolio_test_policy_profiles.sqlite3")
if os.path.exists(_TMP):
    os.remove(_TMP)
os.environ["SQLITE_PATH"] = _TMP

from main_mission.portfolio_os.store import db as store_db
from main_mission.portfolio_os import policy_rules as pr
from main_mission.portfolio_os import selection as selection_mod


def setup():
    store_db.init()


def _seed_policy(account_index, *, policy_type, template=None, user_overrides=None, disabled_rules=None):
    """portfolio_policies 에 동적 컬럼 포함한 최신 버전 1건 시드."""
    conn = store_db.connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(version),0) AS v FROM portfolio_policies WHERE account_index=?",
            (account_index,),
        ).fetchone()
        version = int(row["v"]) + 1
        conn.execute(
            "INSERT INTO portfolio_policies(account_index, version, policy, source, policy_type, "
            "policy_template, user_overrides_json, disabled_rules_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?, datetime('now'))",
            (account_index, version, json.dumps({"account_index": account_index}), "test",
             policy_type, template,
             json.dumps(user_overrides or {}), json.dumps(disabled_rules or [])),
        )
        conn.commit()
    finally:
        conn.close()


# ---- effective single_name differs per policy_type ----
def test_effective_single_name_differs_by_type():
    _seed_policy(11, policy_type="single_stock_focus")
    _seed_policy(12, policy_type="cash_defensive")
    a = pr.effective_policy(11)
    b = pr.effective_policy(12)
    # 개별주 집중형은 단일 한도 default 가 더 높다(프로파일 35), 방어형은 기본 20.
    assert a["limits"]["single_name_max_pct"] == 35.0, a["limits"]
    assert b["limits"]["single_name_max_pct"] < a["limits"]["single_name_max_pct"], (a["limits"], b["limits"])
    assert a["policy_type"] == "single_stock_focus"
    assert a["sources"]["single_name_max_pct"] == "profile"


# ---- cash_defensive has stricter cash_min ----
def test_cash_defensive_stricter_cash_min():
    _seed_policy(13, policy_type="cash_defensive")
    eff = pr.effective_policy(13)
    assert eff["limits"]["cash_min_pct"] == 30.0, eff["limits"]
    assert eff["limits"]["cash_min_pct"] > pr.DEFAULT_RULES["cash_min_pct"], eff["limits"]
    assert eff["flags"]["allow_themes"] is False, eff["flags"]
    assert "cash_band" in eff["emphasis"], eff["emphasis"]


# ---- user_override on a SOFT rule applies (and is sourced 'user') ----
def test_user_override_soft_rule_applies():
    _seed_policy(14, policy_type="etf_diversified",
                 user_overrides={"single_name_max_pct": 12.0, "sector_max_pct": 22.0})
    eff = pr.effective_policy(14)
    assert eff["limits"]["single_name_max_pct"] == 12.0, eff["limits"]
    assert eff["limits"]["sector_max_pct"] == 22.0, eff["limits"]
    assert eff["sources"]["single_name_max_pct"] == "user"
    assert eff["sources"]["sector_max_pct"] == "user"


# ---- user_override / disable on a HARD rule is ignored/blocked ----
def test_hard_rule_override_and_disable_blocked():
    _seed_policy(15, policy_type="custom",
                 user_overrides={"no_market_buy": False, "human_approval_required": False,
                                 "single_name_max_pct": 50.0},
                 disabled_rules=["no_market_buy", "sector_max_pct"])
    eff = pr.effective_policy(15)
    # hard override 시도는 무시되고 effective 에 새지 않음
    assert "no_market_buy" in eff["ignored_overrides"], eff
    assert "human_approval_required" in eff["ignored_overrides"], eff
    assert "no_market_buy" not in eff["effective"], eff["effective"]
    # hard rule 끄기 시도는 차단, soft 규칙은 허용
    assert "no_market_buy" in eff["blocked_disables"], eff
    assert "sector_max_pct" in eff["soft_disabled"], eff
    # soft override 는 정상 적용(자유형이라도)
    assert eff["limits"]["single_name_max_pct"] == 50.0, eff["limits"]
    # hard rule 은 항상 enforce 목록에 존재
    assert "no_market_buy" in eff["hard_rules"]
    assert eff["sources"]["no_market_buy"] == "hard"


# ---- precheck uses effective single_name/sector for crafted rows+policy ----
def test_precheck_uses_effective_limits():
    # ETF 분산형: sector_max default 25. 28% 테마는 block 되어야 한다(policy.limits 가 느슨해도).
    _seed_policy(16, policy_type="etf_diversified")
    policy = {"limits": {"sector_max_pct": 40.0, "single_name_max_pct": 30.0},  # 일부러 느슨
              "cash_band": {"min": 5.0, "max": 60.0}, "region_targets": {}, "bond": {}}
    rows = [
        {"kind": "cash", "ref": "현금", "weight_pct": 30.0},
        {"kind": "tilt", "ref": "반도체", "weight_pct": 28.0},
    ]
    pc = selection_mod.precheck(rows, policy, stale=False, account_index=16)
    # effective sector_max=25 < 28 → block
    assert pc["status"] == "block", pc
    assert any("섹터 한도 25" in r["msg"] for r in pc["reasons"] if r["level"] == "block"), pc["reasons"]

    # account_index 없으면 back-compat: policy.limits(40) 기준 → 28% 는 통과(block 아님)
    pc2 = selection_mod.precheck(rows, policy, stale=False)
    assert pc2["status"] != "block", pc2


# ---- from_effective constructor maps limits onto order-time gate ----
def test_risk_limits_from_effective():
    from main_mission.portfolio_os.risk.gate import RiskLimits
    from decimal import Decimal
    _seed_policy(17, policy_type="single_stock_focus")
    eff = pr.effective_policy(17)
    rl = RiskLimits.from_effective(eff)
    assert rl.single_name_max_pct == Decimal("35.0"), rl
    # 빈/None 은 기본값 유지
    base = RiskLimits()
    assert RiskLimits.from_effective(None) == base


if __name__ == "__main__":
    setup()
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
        print(f"  PASS {f.__name__}")
    print(f"ALL {len(fns)} POLICY-PROFILE TESTS PASSED")
