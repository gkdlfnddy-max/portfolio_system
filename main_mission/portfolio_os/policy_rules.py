"""유연 투자기준 (Dynamic Policy) — default rule vs **hard rule** 분리.

CEO 원칙: 시스템은 하나의 투자 철학을 강요하지 않는다. 투자 스타일 값은 유동적(default+override),
그러나 **안전·정합성·승인·보안 규칙(hard rule)은 절대 override/disable 불가**.

  - DEFAULT_RULES : 사용자가 바꿀 수 있는 기본값(현금밴드/한도/pace 등)
  - HARD_RULES    : disabled_rules_json/user_overrides_json 으로도 끌 수 없는 불변 규칙
  - TEMPLATES     : 투자 스타일 시작점(개별주집중/ETF분산/현금방어/성장테마/배당인컴/자유형) — 시작점일 뿐 수정 가능
"""
from __future__ import annotations

# 절대 불변 (override/disable 시도 무시) — safety_rules + 시스템 정합성.
# 불변 규칙(hard rule) — disabled_rules/user_overrides 로도 끌 수 없음.
# CEO 지시(2026-06-21): 필요한 것은 추가하고 무효가 된 것은 빼며 계속 발전시킨다.
#   변경이력: pin_required_for_accounts 제거(계좌 PIN 전면 폐기 → 로그인+RBAC),
#             no_auto_policy_change·account_memory_isolation·evidence_for_strong_advice·
#             no_anthropic_api·no_fake_data 추가.
HARD_RULES: dict[str, str] = {
    "human_approval_required": "사람 승인 없이 주문 금지",
    "no_auto_order": "자동 주문 생성 금지 — 주문은 사용자 승인 후에만",
    "no_placeholder_as_real": "placeholder/미연동 데이터를 실데이터처럼 노출 금지",
    "live_order_blocked_by_default": "live 주문 기본 차단(KIS_LIVE_CONFIRM 별도)",
    "selected_allocation_required": "selected allocation 없이 주문 후보 금지",
    "no_stale_snapshot_decision": "stale snapshot 으로 decision 금지",
    "no_zero_qty_order": "qty=0 주문 후보 금지",
    "no_market_buy": "시장가 매수 영구 금지(지정가 예측진입)",
    "kis_secret_protected": "KIS secret 노출/DB저장 금지",
    "login_and_rbac_required": "계좌 접근은 로그인+RBAC(계좌별 권한) 필수",
    "web_no_direct_kis": "웹에서 KIS 직접 호출 금지",
    "db_truth_only": "DB 없는 데이터를 운영 화면에 표시 금지",
    "no_auto_policy_change": "사용자 승인 전 policy/목표비중 자동 변경 금지(draft만)",
    "account_memory_isolation": "계좌/사용자 memory 교차적용 금지",
    "evidence_for_strong_advice": "근거(evidence) 없는 강한 조언 금지",
    "no_anthropic_api": "Anthropic API 미사용 — 지능은 Claude+메모리",
    "no_fake_data": "가짜/mock 데이터를 실데이터처럼 표시·완료보고 금지",
}

# 사용자가 override 가능한 기본값(없을 때 제안값). 고정 진리가 아니다.
DEFAULT_RULES: dict[str, object] = {
    "cash_min_pct": 10.0,
    "cash_max_pct": 40.0,
    "single_name_max_pct": 20.0,
    "sector_max_pct": 30.0,
    "inverse_max_pct": 10.0,
    "leverage_max_pct": 15.0,
    "one_order_cap_pct": 5.0,
    "rebalance_rounds_min": 3,
    "rebalance_rounds_max": 5,
    "pace": "normal",
    "use_etf": True,
    "use_individual_stocks": True,
    "use_bond": True,
    "allow_inverse": True,
    "allow_themes": True,
}

# 투자 스타일 템플릿(시작점) — 사용자가 언제든 수정. 강요 아님.
TEMPLATES: dict[str, dict] = {
    "single_stock_focus": {"label": "개별주 집중형", "single_name_max_pct": 35.0, "use_etf": False,
                            "individual_count": 8, "allow_themes": True, "note": "종목수·단일한도·섹터쏠림·분할기준 설정 필요"},
    "etf_diversified": {"label": "ETF 분산형", "use_individual_stocks": False, "single_name_max_pct": 15.0,
                        "sector_max_pct": 25.0, "note": "ETF 중복노출·국가/통화/섹터 분산 점검 강화"},
    "cash_defensive": {"label": "현금/방어형", "cash_min_pct": 30.0, "cash_max_pct": 60.0, "use_bond": True,
                       "allow_themes": False, "note": "현금밴드·국채 duration·안정자산 비중 강화"},
    "growth_theme": {"labels": "성장 테마형", "label": "성장 테마형", "sector_max_pct": 35.0, "pace": "slow",
                     "note": "테마별 상한·변동성·분할진입 필수"},
    "dividend_income": {"label": "배당/인컴형", "use_bond": True, "allow_themes": False,
                        "note": "배당주/배당ETF/리츠/채권 중심, 배당일정·현금흐름 관리"},
    "custom": {"label": "사용자 자유형", "note": "모든 한도/비중 사용자 설정, 시스템은 조언·위험경고만"},
}


def apply_overrides(user_overrides: dict | None = None, disabled_rules: list | None = None,
                    template: str | None = None) -> dict:
    """기본값 + 템플릿 + 사용자 override 를 병합한 effective 정책. **hard rule 은 절대 변경 안 됨.**

    반환: {effective, hard_rules, ignored_overrides(hard 시도), blocked_disables(hard 끄기 시도), template_applied}
    """
    effective = dict(DEFAULT_RULES)
    template_applied = None
    if template and template in TEMPLATES:
        for k, v in TEMPLATES[template].items():
            if k in ("label", "labels", "note"):
                continue
            if k not in HARD_RULES:
                effective[k] = v
        template_applied = template

    ignored = []
    for k, v in (user_overrides or {}).items():
        if k in HARD_RULES:
            ignored.append(k)          # hard rule override 시도 — 무시
            continue
        effective[k] = v

    # disabled_rules 로 hard rule 을 끄려는 시도는 차단(유지).
    blocked = [r for r in (disabled_rules or []) if r in HARD_RULES]
    soft_disabled = [r for r in (disabled_rules or []) if r not in HARD_RULES]

    return {
        "effective": effective,
        "hard_rules": list(HARD_RULES),
        "ignored_overrides": ignored,
        "blocked_disables": blocked,
        "soft_disabled": soft_disabled,
        "template_applied": template_applied,
    }


def is_hard_rule(name: str) -> bool:
    return name in HARD_RULES


# 투자 스타일별 **게이트 강조점**(default — 사용자 override 로 덮어쓸 수 있음).
# 템플릿(TEMPLATES)이 스타일 시작값이라면, RISK_PROFILES 는 그 스타일에서 리스크 게이트가
# 어디를 더(또는 덜) 조이는지의 기본값이다. hard rule 은 여기 절대 들어가지 않는다.
RISK_PROFILES: dict[str, dict] = {
    # 개별주 집중형: 단일 한도를 높이되 집중·이벤트·손실 경고를 강조.
    "single_stock_focus": {
        "single_name_max_pct": 35.0,
        "sector_max_pct": 40.0,
        "emphasis": ["concentration", "event_risk", "loss_warning"],
    },
    # ETF 분산형: 중복노출/국가/통화/섹터 분산을 더 엄격히.
    "etf_diversified": {
        "single_name_max_pct": 15.0,
        "sector_max_pct": 25.0,
        "max_single_country_pct": 60.0,
        "currency_max_pct": 70.0,
        "emphasis": ["overlap", "country_diversification", "currency_diversification"],
    },
    # 현금/방어형: 현금밴드/국채/듀레이션을 강조, 테마 차단.
    "cash_defensive": {
        "cash_min_pct": 30.0,
        "cash_max_pct": 60.0,
        "allow_themes": False,
        "emphasis": ["cash_band", "bond_duration", "stability"],
    },
    # 성장 테마형: 테마 상한을 조이고 분할진입(pace slow) 강조.
    "growth_theme": {
        "sector_max_pct": 35.0,
        "pace": "slow",
        "emphasis": ["theme_cap", "volatility", "split_entry"],
    },
    # 배당/인컴형: 채권/인컴 강조.
    "dividend_income": {
        "allow_themes": False,
        "emphasis": ["bond", "income", "cashflow"],
    },
    # 자유형: 프로파일 강조 없음 — 사용자가 모두 설정.
    "custom": {
        "emphasis": [],
    },
}

# effective limits 로 노출되는 한도 키(나머지는 flags). emphasis 등 메타는 제외.
_LIMIT_KEYS = (
    "cash_min_pct", "cash_max_pct", "single_name_max_pct", "sector_max_pct",
    "inverse_max_pct", "leverage_max_pct", "one_order_cap_pct",
    "max_single_country_pct", "currency_max_pct", "emerging_market_max_pct",
)


def risk_profile(policy_type: str | None) -> dict:
    """policy_type 의 게이트 강조 기본값(default). 없으면 빈 dict."""
    return dict(RISK_PROFILES.get(policy_type or "", {}))


def effective_policy(account_index: int) -> dict:
    """계좌별 **실효 정책** — default + template + RISK_PROFILE + 사용자 override 병합.

    우선순위(낮음→높음): DEFAULT_RULES → TEMPLATE → RISK_PROFILE(policy_type) → user_overrides.
    hard rule 은 어떤 경로로도 변경되지 않으며, hard rule 을 끄려는 disabled 시도는 차단된다.

    반환:
      {
        account_index, policy_type, policy_template,
        effective: {limits + flags 병합 결과},
        limits:    {게이트가 쓰는 한도만 추린 dict},
        flags:     {use_etf/allow_themes/pace 등 비한도 값},
        hard_rules, ignored_overrides, blocked_disables, soft_disabled,
        emphasis, sources: {field: 'default'|'template'|'profile'|'user'|'hard'},
      }
    """
    from .store import db as store_db  # 지역 import: CLI 외 경로에서 DB 의존 최소화

    policy_type = None
    policy_template = None
    user_overrides: dict = {}
    disabled_rules: list = []
    conn = store_db.connect()
    try:
        # 진리 우선순위: investor_profile(UI가 profile.save 로 저장) → portfolio_policies(버전/레거시) fallback.
        ip = conn.execute(
            "SELECT policy_type, user_overrides_json, disabled_rules_json "
            "FROM investor_profile WHERE account_index=?",
            (account_index,),
        ).fetchone()
        pp = conn.execute(
            "SELECT policy_type, policy_template, user_overrides_json, disabled_rules_json "
            "FROM portfolio_policies WHERE account_index=? ORDER BY version DESC LIMIT 1",
            (account_index,),
        ).fetchone()
    finally:
        conn.close()
    ipd = dict(ip) if ip is not None else {}
    ppd = dict(pp) if pp is not None else {}

    def _pick(key):
        for src in (ipd, ppd):
            v = src.get(key)
            if v not in (None, ""):
                return v
        return None

    policy_type = _pick("policy_type")
    policy_template = _pick("policy_template") or policy_type  # policy_type 을 시작 템플릿으로
    user_overrides = _safe_json(_pick("user_overrides_json"), {})
    disabled_rules = _safe_json(_pick("disabled_rules_json"), [])
    # 템플릿 미지정 시 policy_type 을 시작 템플릿으로 사용(스타일=시작점).
    template = policy_template or policy_type

    # 1) sources 추적을 위해 단계별로 적용.
    sources: dict[str, str] = {}
    effective = dict(DEFAULT_RULES)
    for k in effective:
        sources[k] = "default"

    if template and template in TEMPLATES:
        for k, v in TEMPLATES[template].items():
            if k in ("label", "labels", "note"):
                continue
            if k in HARD_RULES:
                continue
            effective[k] = v
            sources[k] = "template"

    profile = risk_profile(policy_type)
    for k, v in profile.items():
        if k == "emphasis":
            continue
        if k in HARD_RULES:
            continue
        effective[k] = v
        sources[k] = "profile"

    ignored_overrides: list = []
    for k, v in (user_overrides or {}).items():
        if k in HARD_RULES:
            ignored_overrides.append(k)  # hard rule override 시도 — 무시
            continue
        effective[k] = v
        sources[k] = "user"

    blocked_disables = [r for r in (disabled_rules or []) if r in HARD_RULES]
    soft_disabled = [r for r in (disabled_rules or []) if r not in HARD_RULES]
    for r in blocked_disables:
        sources[r] = "hard"  # 끄려 했으나 hard 라 유지됨을 명시

    limits = {k: effective[k] for k in _LIMIT_KEYS if k in effective}
    flags = {k: v for k, v in effective.items() if k not in _LIMIT_KEYS}

    return {
        "account_index": account_index,
        "policy_type": policy_type,
        "policy_template": policy_template,
        "effective": effective,
        "limits": limits,
        "flags": flags,
        "hard_rules": list(HARD_RULES),
        "ignored_overrides": ignored_overrides,
        "blocked_disables": blocked_disables,
        "soft_disabled": soft_disabled,
        "emphasis": list(profile.get("emphasis", [])),
        "sources": sources,
    }


def _safe_json(raw, default):
    import json
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return default


def main() -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="계좌별 실효 정책(effective_policy) JSON 출력 — 웹 API 소비용")
    ap.add_argument("--account", type=int, required=True, help="계좌 인덱스 (필수)")
    args = ap.parse_args()
    try:
        out = {"ok": True, "effective_policy": effective_policy(args.account)}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
