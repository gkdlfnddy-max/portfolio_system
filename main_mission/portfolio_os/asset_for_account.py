"""asset_for_account — **같은 자산을 계좌 목적에 따라 다르게 해석**.

CEO 지시: 동일 종목(예: 005930 삼성전자)이라도 계좌가 성장형이면 "후보 유지·분할·hedge",
방어형이면 "직접 편입 보류·글로벌 ETF/국채 우선" 으로 *판단이 달라야* 한다.

결합식(계좌 최우선):
    공통 자산 사실(asset_memory shared + evidence/가격/수급) +
    계좌 목적(objective/criteria) + 계좌 확정 배분(selected_allocation) +
    계좌 risk rule(effective_policy limits/forbidden) + 계좌 견해/lesson
  → **그 계좌에 맞는 판단 후보(여러 개)**.

설계 제약(불변):
- **자동 적용 0.** 후보·confidence·주의문구·근거 출처만. 주문/policy/배분 변경 없음.
- **계좌 격리.** 다른 계좌의 견해/배분/lesson 은 보이지 않는다(account_index 한정).
- **공통 지식이 계좌 정책을 덮어쓰지 않는다.** 계좌 forbidden/한도가 공통 후보를 *제약*한다.
- **confidence 낮으면 단정 회피.** 근거(공통 사실/evidence) 부족 → 질문·"확인 필요"로.
- **지능 = Claude + 메모리 (Anthropic API 미사용 — import 없음).** 매핑은 순수 규칙.

  python -m main_mission.portfolio_os.asset_for_account --account 1 --scope-type stock --asset 005930
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import account_memory as acct_mem
from . import memory_prehook as prehook
from . import investor_objective as objective

# 계좌 목적(goal) → 그 목적에서 "위험 자산 직접 편입"을 어떻게 다루는지(규칙).
# stance: 그 자산을 어떤 태도로 볼지 / actions: 후보 행동 / prefer: 대체 우선.
# 수익률 최대화 단일 기준이 아님 — investor_objective._CRITERIA 와 정합.
_GOAL_DISPOSITION: dict[str, dict] = {
    "aggressive_growth": {
        "stance": "growth_candidate",
        "headline": "공격적 성장 — 위험자산 직접 편입에 적극(분할 진입).",
        "actions": ["후보 유지", "지정가 분할 진입(무릎)", "비중 상향 검토"],
        "prefer": [],
        "direct_equity_ok": True,
    },
    "growth": {
        "stance": "growth_candidate",
        "headline": "성장 — 위험자산 직접 편입 가능하되 분할·hedge 동반.",
        "actions": ["후보 유지", "지정가 분할 진입(무릎)", "변동성 구간 hedge 검토"],
        "prefer": [],
        "direct_equity_ok": True,
    },
    "thesis_hold": {
        "stance": "thesis_aligned",
        "headline": "thesis 유지 — 견해 정합 시 보유/유지, 회전 최소화.",
        "actions": ["thesis 정합 점검", "회전 최소화", "견해 충돌 시 비중 점검"],
        "prefer": [],
        "direct_equity_ok": True,
    },
    "stable_operation": {
        "stance": "balanced",
        "headline": "안정 운용 — 분산 안에서만 소량, 집중 회피.",
        "actions": ["소량·분산 내 편입 검토", "단일 한도 준수", "ETF 대체 비교"],
        "prefer": ["etf"],
        "direct_equity_ok": True,
    },
    "dividend": {
        "stance": "income_first",
        "headline": "배당 인컴 — 배당 기여 낮으면 직접 편입 후순위.",
        "actions": ["배당 기여 점검", "배당주/배당ETF 우선 비교"],
        "prefer": ["dividend", "etf"],
        "direct_equity_ok": False,
    },
    "volatility_reduction": {
        "stance": "defensive",
        "headline": "변동성 축소 — 개별주 변동성 부담, ETF/분산 우선.",
        "actions": ["직접 편입 보류 검토", "글로벌/분산 ETF 우선", "상관 낮은 자산 비교"],
        "prefer": ["etf", "bond"],
        "direct_equity_ok": False,
    },
    "loss_reduction": {
        "stance": "defensive",
        "headline": "손실 축소 — 하락 방어 우선, 개별주 직접 편입 보류.",
        "actions": ["직접 편입 보류", "글로벌 ETF/국채 우선", "현금 여력 유지"],
        "prefer": ["etf", "bond", "cash"],
        "direct_equity_ok": False,
    },
    "cash_preservation": {
        "stance": "defensive",
        "headline": "자본 보존 — 원금 우선, 위험자산 직접 편입 보류.",
        "actions": ["직접 편입 보류", "국채/현금 우선", "필요 시 최소 분산만"],
        "prefer": ["cash", "bond"],
        "direct_equity_ok": False,
    },
}

# 위험 자산(개별주)으로 보는 scope — 직접 편입 판단이 목적에 민감.
_RISKY_SCOPES = ("stock", "theme", "sector")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _shared_facts_strength(ctx: dict) -> tuple[int, list[str]]:
    """공통 자산 사실(근거) 강도 — 후보 confidence 산정용.

    근거 = 출처 있는 공통 메모리 + evidence + 최근 가격/수급. weak/stale 은 제외.
    """
    n = 0
    why: list[str] = []
    sourced_shared = [
        m for m in (ctx.get("asset_memory_shared") or [])
        if not m.get("stale") and not m.get("weak")
    ]
    if sourced_shared:
        n += len(sourced_shared)
        why.append(f"공통 자산지식(출처O) {len(sourced_shared)}건")
    ev = [e for e in (ctx.get("evidence") or []) if not e.get("stale")]
    if ev:
        n += len(ev)
        why.append(f"evidence {len(ev)}건")
    if ctx.get("latest_price"):
        n += 1
        why.append("최근 가격")
    if ctx.get("latest_flows"):
        n += 1
        why.append("최근 수급")
    return n, why


def _confidence(fact_n: int, goal_set: bool) -> tuple[float, str]:
    """근거 강도 + 목적 설정 여부로 confidence. 낮으면 단정 회피."""
    if not goal_set:
        return 0.2, "계좌 목적 미설정 — 단정 회피(먼저 목적 입력)."
    if fact_n == 0:
        return 0.25, "공통 근거(사실/evidence) 부족 — 단정 회피, 확인 필요."
    if fact_n <= 2:
        return 0.5, "근거 보통 — 후보 제시(확정 아님)."
    return 0.7, "근거 충분 — 후보 신뢰도 보통 이상(여전히 사람 승인)."


def _risk_constraints(asset: str, scope_type: str, policy: dict | None) -> list[str]:
    """계좌 risk rule 이 이 자산에 거는 제약 — 공통 후보를 *덮어쓰지 않고 제한*."""
    out: list[str] = []
    if not policy:
        return out
    # policy["effective"] 는 effective_policy() 반환 dict — 실효값은 그 안의 'effective'.
    eff_pol = policy.get("effective") or {}
    eff = (eff_pol.get("effective") or {}) if isinstance(eff_pol, dict) else {}
    compiled = policy.get("compiled") or {}
    # 인버스 금지(숏 정책 none) — hedge 후보 차단.
    forbidden = (compiled.get("forbidden_assets") or [])
    if "inverse" in forbidden:
        out.append("계좌 정책: 인버스/숏 금지 → hedge(인버스) 후보 제외.")
    single = eff.get("single_name_max_pct")
    if single is not None:
        out.append(f"계좌 단일 종목 한도 {single}% — 직접 편입 시 초과 금지.")
    if eff.get("allow_themes") is False and scope_type in ("theme", "sector"):
        out.append("계좌 정책: 테마 비허용 → 테마 직접 베팅 보류.")
    if eff.get("use_individual_stocks") is False and scope_type == "stock":
        out.append("계좌 정책: 개별주 미사용 → ETF 대체 우선.")
    return out


def interpret(asset: str, account_index: int, *, scope_type: str = "stock",
              ticker: str | None = None, theme: str | None = None) -> dict:
    """같은 자산을 **이 계좌 목적/배분/risk** 로 해석한 판단 후보(자동 적용 아님).

    asset: 자산 키(예: '005930' 또는 테마명). scope_type: stock/etf/theme/sector/...
    반환: 계좌별 stance·후보 행동·제약·confidence·근거 출처·질문(낮은 신뢰 시).
    """
    asset = str(asset).strip()
    if not asset:
        raise ValueError("asset 은 필수입니다")
    acct = acct_mem._acct(account_index)

    # ① 공통 자산 사실 + 그 계좌 관점/견해 (prehook — 계좌 격리)
    ctx = prehook.prehook_context(
        acct, scope_type, asset, ticker=ticker or (asset if scope_type in ("stock", "etf") else None),
        theme=theme,
    )
    # ② 계좌 목적/정책/확정배분 (계좌 최우선)
    ac = acct_mem.account_context(acct)

    obj = ac["objective"]
    goal = (obj["objective"] or {}).get("investment_goal") if obj["objective"] else None
    goal_set = obj["is_set"]
    disp = _GOAL_DISPOSITION.get(goal or "", None)

    fact_n, fact_why = _shared_facts_strength(ctx)
    confidence, conf_note = _confidence(fact_n, goal_set)

    # ③ 후보 합성 — 공통 사실은 같아도 *계좌 목적이 행동을 가른다*.
    actions: list[str] = []
    stance = "unknown"
    headline = "계좌 목적 미설정 — 단정하지 않고 목적 확인을 권합니다."
    if disp is not None:
        stance = disp["stance"]
        headline = disp["headline"]
        actions = list(disp["actions"])
        # 위험자산인데 방어형이면 대체 우선을 명시
        if scope_type in _RISKY_SCOPES and not disp["direct_equity_ok"]:
            prefs = disp.get("prefer") or []
            if prefs:
                actions.append("대체 우선: " + "/".join(prefs))

    # ④ 계좌 risk rule 제약 (공통 후보를 제한 — override 아님)
    constraints = _risk_constraints(asset, scope_type, ac["policy"])

    # ⑤ 확정 배분 맥락 (있으면 최우선 — 후보는 이 배분 안에서)
    alloc_note = None
    if ac["selected_allocation"]:
        alloc_note = "확정 배분 존재 — 후보는 이 배분/리밸런싱 한도 안에서만 의미."

    # ⑥ 출처(근거) 표기 — 어디서 왔는지 불분명하면 신뢰 불가.
    sources = {
        "shared_facts": fact_why,
        "account_objective": goal,
        "account_policy": "effective_policy" if ac["policy"]["effective"] else None,
        "account_views": len(ctx.get("user_views") or []),
        "account_lessons": len(ac["lessons"] or []),
        "selected_allocation": bool(ac["selected_allocation"]),
    }

    questions: list[str] = []
    cautions: list[str] = list(ctx["summary"].get("cautions", []))
    if not goal_set:
        questions.append("이 계좌의 투자 목적을 먼저 알려주세요(목적에 따라 같은 종목 판단이 달라집니다).")
    if confidence < 0.5:
        questions.append("공통 근거(사실/evidence)가 부족합니다 — 자료 조사 후 재판단할까요?")

    return {
        "asset": asset,
        "scope_type": scope_type,
        "account_index": acct,
        "account_goal": goal,
        "account_goal_label": (objective.GOALS.get(goal) if goal else None),
        "stance": stance,                 # 계좌 목적에 따른 태도(같은 자산도 다름)
        "headline": headline,
        "candidate_actions": actions,     # 후보 행동(자동 적용 아님)
        "risk_constraints": constraints,  # 계좌 risk rule 제약(공통 후보 제한)
        "allocation_note": alloc_note,
        "confidence": confidence,         # 낮으면 단정 회피
        "confidence_note": conf_note,
        "sources": sources,               # 근거 출처(불분명=신뢰 불가)
        "cautions": cautions,             # stale/출처없음/상충(prehook)
        "questions": questions,           # 사람에게 — 자동 결정 아님
        "conflicts": ctx.get("conflicts", []),
        # 안전 단언
        "isolated": True,                 # 계좌 격리(타 계좌 견해/배분/lesson 안 보임)
        "advisory_only": True,
        "applied": False,
        "generated_at": _now(),
    }


# ============================================================
# CLI
# ============================================================
def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="asset_for_account — 같은 자산을 계좌 목적으로 다르게 해석")
    p.add_argument("--account", type=int, required=True)
    p.add_argument("--asset", required=True)
    p.add_argument("--scope-type", default="stock")
    p.add_argument("--ticker")
    p.add_argument("--theme")
    a = p.parse_args(argv)
    try:
        out = interpret(a.asset, a.account, scope_type=a.scope_type, ticker=a.ticker, theme=a.theme)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
