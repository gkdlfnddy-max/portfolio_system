"""투자 목적/성향 프로파일 — 계좌별. "최선"의 *기준*을 정하는 토대.

CEO 원칙: **"최선" ≠ 수익률 최대화.** 사람마다 최선이 다르다(손실 줄이기·잠 잘자기·
배당·변동성↓·thesis 유지·현금 확보·공격적 성장…). 그래서 *목적을 먼저 확인*해야
그 관점에서의 최선을 계산할 수 있다. 이 모듈은 (1) 계좌별 목적/성향을 저장·조회하고,
(2) 목적 → "최선 기준"(평가지표 우선순위)을 **규칙 기반**으로 매핑한다.

설계 제약(불변):
- **스키마 무변경.** 기존 `user_views` 테이블에 `layer='objective'`, `note=JSON` 1행으로 저장.
- **계좌 격리.** 다른 계좌 목적은 조회/수정 불가(교차적용 금지).
- **자동 적용 0.** 저장만 한다. allocation/관점별 후보(B 에이전트)가 *읽어* 쓸 뿐,
  여기서 포트폴리오/주문을 바꾸지 않는다.
- **정직.** 목적 미설정이면 기본값을 *가정하지 않고* "미설정 — 먼저 입력 권장"을 알린다.
- **지능 = Claude+메모리 (Anthropic API 미사용).** 매핑은 순수 규칙.

  python -m main_mission.portfolio_os.investor_objective --account 1 --get
  python -m main_mission.portfolio_os.investor_objective --account 1 --set --json '{"investment_goal":"loss_reduction","risk_tolerance":"low"}'
  python -m main_mission.portfolio_os.investor_objective --account 1 --criteria
  python -m main_mission.portfolio_os.investor_objective --goals
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .store import db as store_db

# user_views 의 layer 값으로 목적 행을 식별(스키마 무변경 — 기존 TEXT 컬럼 재사용).
OBJECTIVE_LAYER = "objective"

# ── 허용 enum (이 모듈이 SSOT) ──────────────────────────────
# 투자 목적: "최선"의 관점. 사람마다 다르다(수익률 최대화만이 아님).
GOALS: dict[str, str] = {
    "loss_reduction": "손실 축소(손실을 줄이는 것이 최우선)",
    "dividend": "배당 수입(꾸준한 현금 흐름)",
    "growth": "성장(장기 자본 성장)",
    "aggressive_growth": "공격적 성장(높은 위험 감수)",
    "volatility_reduction": "변동성 축소(흔들림을 줄여 잘 자기)",
    "thesis_hold": "thesis 유지(견해/논리를 지키며 보유)",
    "cash_preservation": "현금 확보/자본 보존(원금 우선)",
    "stable_operation": "안정 운용(균형 잡힌 무난한 운용)",
}

RISK_LEVELS = ("low", "mid", "high")
REGIONS = ("kr", "us", "global")
MARKET_VIEWS = ("short", "long")          # 단기/장기 시장관(보는 기간)
PREFERS = ("cash", "bond", "dividend", "growth", "etf")  # 다중 선택
ALLOWS = ("inverse", "leverage")          # bool 토글(다중)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _acct(account_index) -> int:
    n = int(account_index)
    if n < 1:
        raise ValueError("account_index 는 1 이상이어야 합니다")
    return n


def _clean(value) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    return v or None


def _norm_enum(value, allowed, field) -> str | None:
    v = _clean(value)
    if v is None:
        return None
    v = v.lower()
    if v not in allowed:
        raise ValueError(f"{field} 는 {tuple(allowed)} 중 하나여야 합니다 (받음: {value!r})")
    return v


def _norm_loss_aversion(value) -> float | None:
    if value is None or value == "":
        return None
    f = float(value)
    if not (0.0 <= f <= 1.0):
        raise ValueError("loss_aversion 은 0~1 범위여야 합니다")
    return f


def _norm_prefers(value) -> list[str]:
    """선호(다중)를 정규화 — 허용 목록 교집합, 중복 제거, 순서 보존."""
    if value is None or value == "":
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).replace("/", ",").split(",")
    out: list[str] = []
    for it in items:
        v = _clean(it)
        if v is None:
            continue
        v = v.lower()
        if v not in PREFERS:
            raise ValueError(f"prefers 항목은 {PREFERS} 중에서만 (받음: {it!r})")
        if v not in out:
            out.append(v)
    return out


def _norm_allows(value) -> dict[str, bool]:
    """허용(inverse/leverage)을 bool dict 로 정규화. 리스트/딕트 모두 허용."""
    out = {k: False for k in ALLOWS}
    if not value:
        return out
    if isinstance(value, dict):
        for k, v in value.items():
            kk = _clean(k)
            if kk and kk.lower() in ALLOWS:
                out[kk.lower()] = bool(v) and str(v).lower() not in ("false", "0", "no")
        return out
    items = value if isinstance(value, (list, tuple)) else str(value).replace("/", ",").split(",")
    for it in items:
        v = _clean(it)
        if v and v.lower() in ALLOWS:
            out[v.lower()] = True
    return out


def normalize(data: dict) -> dict:
    """입력(자유로운 dict)을 구조화된 목적/성향 객체로 정규화·검증."""
    goal = _clean(data.get("investment_goal"))
    if goal is not None:
        goal = goal.lower()
        if goal not in GOALS:
            raise ValueError(f"investment_goal 은 {tuple(GOALS)} 중 하나여야 합니다 (받음: {data.get('investment_goal')!r})")
    return {
        "investment_goal": goal,
        "horizon": _clean(data.get("horizon")),
        "risk_tolerance": _norm_enum(data.get("risk_tolerance"), RISK_LEVELS, "risk_tolerance"),
        "loss_aversion": _norm_loss_aversion(data.get("loss_aversion")),
        "prefers": _norm_prefers(data.get("prefers")),
        "allows": _norm_allows(data.get("allows")),
        "region_pref": _norm_enum(data.get("region_pref"), REGIONS, "region_pref"),
        "market_view": _norm_enum(data.get("market_view"), MARKET_VIEWS, "market_view"),
        "note": _clean(data.get("note")),
    }


# ───────────────────────── 저장/조회 (계좌 격리) ─────────────────────────

def get(account_index: int) -> dict | None:
    """계좌의 *현재(active)* 목적/성향. 없으면 None (목적 미설정).

    user_views(layer='objective', status='active') 1행을 읽어 note(JSON)를 풀어 반환.
    계좌 격리: account_index 로만 조회한다.
    """
    acct = _acct(account_index)
    conn = store_db.connect()
    try:
        r = conn.execute(
            "SELECT id, note, created_at, updated_at FROM user_views "
            "WHERE account_index=? AND layer=? AND status='active' "
            "ORDER BY id DESC LIMIT 1",
            (acct, OBJECTIVE_LAYER),
        ).fetchone()
        if not r:
            return None
        try:
            obj = json.loads(r["note"]) if r["note"] else {}
        except (ValueError, TypeError):
            obj = {}
        obj = normalize(obj)
        obj.update({
            "account_index": acct,
            "view_id": r["id"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
        return obj
    finally:
        conn.close()


def is_set(account_index: int) -> bool:
    """목적이 *의미있게* 설정됐는지 — 최소 investment_goal 존재 여부."""
    cur = get(account_index)
    return bool(cur and cur.get("investment_goal"))


def set_objective(account_index: int, data: dict, *, source: str = "user") -> dict:
    """목적/성향 저장 — **이력 보존 supersede**(덮어쓰기 아님).

    기존 active objective 행은 status='superseded' + superseded_by 로 두고
    새 active 행을 만든다. 자동 적용 없음 — 저장만 한다.
    """
    acct = _acct(account_index)
    obj = normalize(data)
    if not any(obj.get(k) not in (None, [], {}, {"inverse": False, "leverage": False})
               for k in ("investment_goal", "horizon", "risk_tolerance", "loss_aversion",
                         "prefers", "region_pref", "market_view", "note")):
        return {"ok": False, "error": "저장할 목적/성향 값이 없습니다(빈 입력)."}

    payload = json.dumps({**obj, "source": _clean(source) or "user"}, ensure_ascii=False)
    now = _now()
    conn = store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO user_views(account_index, layer, note, status, created_at, updated_at) "
            "VALUES(?,?,?, 'active', ?, ?)",
            (acct, OBJECTIVE_LAYER, payload, now, now),
        )
        new_id = int(cur.lastrowid)
        # 같은 계좌의 이전 active objective 들을 supersede (계좌 격리: account_index 한정).
        conn.execute(
            "UPDATE user_views SET status='superseded', superseded_by=?, updated_at=? "
            "WHERE account_index=? AND layer=? AND status='active' AND id<>?",
            (new_id, now, acct, OBJECTIVE_LAYER, new_id),
        )
        conn.commit()
        saved = get(acct)
        return {"ok": True, "account_index": acct, "view_id": new_id, "objective": saved}
    finally:
        conn.close()


# ───────────────────────── "최선 기준" 매핑 ─────────────────────────
# 목적 → 평가지표 우선순위(criteria). 규칙 기반(Anthropic API 미사용).
# 각 항목: metric(평가지표), direction(min=낮을수록 좋음 / max=높을수록 좋음), weight(상대 가중 0~1).
# "최선"은 이 우선순위로 정의된다 — 수익률 최대화 단일 기준이 아님.

_CRITERIA: dict[str, dict] = {
    "loss_reduction": {
        "headline": "손실을 줄이는 것이 최선 — 하락 방어·현금 여력 우선",
        "criteria": [
            {"metric": "max_drawdown", "direction": "min", "weight": 1.0},
            {"metric": "downside_deviation", "direction": "min", "weight": 0.8},
            {"metric": "cash_band", "direction": "max", "weight": 0.6},
            {"metric": "volatility", "direction": "min", "weight": 0.5},
        ],
        "deprioritize": ["max_return", "cagr"],
    },
    "dividend": {
        "headline": "꾸준한 현금 흐름이 최선 — 배당수익률·방어 우선",
        "criteria": [
            {"metric": "dividend_yield", "direction": "max", "weight": 1.0},
            {"metric": "dividend_stability", "direction": "max", "weight": 0.8},
            {"metric": "max_drawdown", "direction": "min", "weight": 0.5},
            {"metric": "defensiveness", "direction": "max", "weight": 0.5},
        ],
        "deprioritize": ["growth_tilt"],
    },
    "growth": {
        "headline": "장기 자본 성장이 최선 — 장기 CAGR·성장 tilt 우선",
        "criteria": [
            {"metric": "cagr", "direction": "max", "weight": 1.0},
            {"metric": "growth_tilt", "direction": "max", "weight": 0.8},
            {"metric": "long_term_return", "direction": "max", "weight": 0.7},
            {"metric": "max_drawdown", "direction": "min", "weight": 0.3},
        ],
        "deprioritize": [],
    },
    "aggressive_growth": {
        "headline": "공격적 성장이 최선 — 기대수익 극대화(높은 변동성 감수)",
        "criteria": [
            {"metric": "expected_return", "direction": "max", "weight": 1.0},
            {"metric": "cagr", "direction": "max", "weight": 0.9},
            {"metric": "growth_tilt", "direction": "max", "weight": 0.8},
        ],
        "deprioritize": ["max_drawdown", "volatility"],
    },
    "volatility_reduction": {
        "headline": "흔들림을 줄이는 것이 최선 — 변동성↓·분산 우선",
        "criteria": [
            {"metric": "volatility", "direction": "min", "weight": 1.0},
            {"metric": "diversification", "direction": "max", "weight": 0.8},
            {"metric": "correlation", "direction": "min", "weight": 0.6},
            {"metric": "max_drawdown", "direction": "min", "weight": 0.5},
        ],
        "deprioritize": ["max_return"],
    },
    "thesis_hold": {
        "headline": "견해/논리 유지가 최선 — thesis 정합·견해 일관성 우선",
        "criteria": [
            {"metric": "thesis_alignment", "direction": "max", "weight": 1.0},
            {"metric": "turnover", "direction": "min", "weight": 0.7},
            {"metric": "conviction_weighting", "direction": "max", "weight": 0.6},
        ],
        "deprioritize": ["short_term_return"],
    },
    "cash_preservation": {
        "headline": "원금 보존이 최선 — 자본 보존·현금 우선",
        "criteria": [
            {"metric": "capital_preservation", "direction": "max", "weight": 1.0},
            {"metric": "max_drawdown", "direction": "min", "weight": 0.9},
            {"metric": "cash_band", "direction": "max", "weight": 0.8},
            {"metric": "volatility", "direction": "min", "weight": 0.5},
        ],
        "deprioritize": ["max_return", "growth_tilt"],
    },
    "stable_operation": {
        "headline": "균형 잡힌 안정 운용이 최선 — 위험조정수익·분산",
        "criteria": [
            {"metric": "sharpe_ratio", "direction": "max", "weight": 1.0},
            {"metric": "diversification", "direction": "max", "weight": 0.7},
            {"metric": "max_drawdown", "direction": "min", "weight": 0.6},
            {"metric": "volatility", "direction": "min", "weight": 0.5},
        ],
        "deprioritize": [],
    },
}


def objective_to_criteria(goal: str | None) -> dict:
    """투자 목적 → "최선 기준"(평가지표 우선순위). 규칙 기반.

    목적 미설정/미인식이면 기본값을 *가정하지 않고* unset 을 명시한다(정직 원칙).
    """
    g = _clean(goal)
    if g is None:
        return {
            "ok": True, "goal": None, "is_set": False,
            "headline": "투자 목적 미설정 — '최선'의 기준을 정할 수 없습니다.",
            "criteria": [],
            "note": "목적을 먼저 입력하세요. 기본값을 가정하지 않습니다(없는 걸 가정 금지).",
        }
    g = g.lower()
    spec = _CRITERIA.get(g)
    if spec is None:
        return {
            "ok": False, "goal": g, "is_set": False,
            "error": f"알 수 없는 목적: {goal!r} (허용: {tuple(GOALS)})",
            "criteria": [],
        }
    return {
        "ok": True, "goal": g, "is_set": True, "label": GOALS[g],
        "headline": spec["headline"],
        "criteria": spec["criteria"],
        "deprioritize": spec.get("deprioritize", []),
        "note": "규칙 기반 매핑입니다(Anthropic API 미사용). '최선'은 이 우선순위로 정의됩니다.",
    }


def criteria_for_account(account_index: int) -> dict:
    """계좌의 저장된 목적으로 "최선 기준" 산출. 미설정이면 정직하게 알림(가정 금지)."""
    cur = get(account_index)
    if not cur or not cur.get("investment_goal"):
        out = objective_to_criteria(None)
        out["account_index"] = _acct(account_index)
        out["objective"] = cur  # None 또는 goal 없는 부분 입력
        return out
    out = objective_to_criteria(cur["investment_goal"])
    out["account_index"] = _acct(account_index)
    out["objective"] = cur
    return out


def goals_catalog() -> dict:
    """선택지 카탈로그(웹 UI 용) — 목적/성향 enum 라벨."""
    return {
        "goals": GOALS,
        "risk_levels": list(RISK_LEVELS),
        "regions": list(REGIONS),
        "market_views": list(MARKET_VIEWS),
        "prefers": list(PREFERS),
        "allows": list(ALLOWS),
    }


# ──────────────────────────── CLI ────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int)
    ap.add_argument("--get", action="store_true")
    ap.add_argument("--set", action="store_true")
    ap.add_argument("--criteria", action="store_true")
    ap.add_argument("--goals", action="store_true", help="선택지 카탈로그(계좌 불필요)")
    ap.add_argument("--json", metavar="PAYLOAD")
    ap.add_argument("--source", default="user")
    args = ap.parse_args()

    try:
        if args.goals:
            out = {"ok": True, "catalog": goals_catalog()}
        elif args.criteria and args.account:
            out = criteria_for_account(args.account)
        elif args.get and args.account:
            out = {"ok": True, "is_set": is_set(args.account), "objective": get(args.account)}
        elif args.set and args.account and args.json:
            out = set_objective(args.account, json.loads(args.json), source=args.source)
        else:
            out = {"ok": False, "error": "--goals | --account 와 함께 --get/--set(--json)/--criteria"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
