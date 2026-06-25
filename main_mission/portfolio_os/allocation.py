"""목표비중 생성 — **anchor + tilt** (보수/기준/공격 3안).

- **대전제(policy)** → **anchor**: 현금 목표 + 글로벌 코어 ETF(광범위 분산 anchor)(테마 외 envelope).
- **중전제(관심 테마)** → **tilt**: 테마별 비중. 단, 섹터/테마 상한·총 tilt 상한으로
  *한 테마가 포트폴리오를 뒤집지 못하게* 제한.
- 보수/기준/공격 3안을 만들어 `target_allocations` 에 저장(draft) → **사람이 선택**.

핵심: 자연어("공격적/글로벌/관심=로봇·바이오·양자")가 **추적 가능한 allocation policy**로 변환됨.

  python -m main_mission.portfolio_os.allocation --account 1 --generate
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from .store import db as store_db
from . import policy as policy_mod
from .growth import middleware as growth_mw


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _themes(interests_text: str | None) -> list[str]:
    if not interests_text:
        return []
    parts = re.split(r"[,/·]| 및 |\s{2,}", interests_text)
    return [s.strip() for s in parts if s.strip()][:8]


# 변이별 tilt 적극성 (invested 중 롱 테마에 싣는 비율) / 헤지(인버스) 비율
TILT_SHARE = {"conservative": 0.3, "base": 0.5, "aggressive": 0.7}
HEDGE_SHARE = {"conservative": 0.03, "base": 0.05, "aggressive": 0.08}


def _split_region(broad: float, region_targets: dict | None) -> list[dict]:
    """광범위 anchor 를 지역 비중으로 분해. region_targets 없으면 단일 '글로벌 코어 ETF(광범위 분산 anchor)'.
    합계가 100이 아니어도 *상대 비율*로 분배(합은 broad 유지) — 합계 오류는 validate가 경고."""
    rows: list[dict] = []
    rt = {k: float(v) for k, v in (region_targets or {}).items() if v and float(v) > 0}
    tot = sum(rt.values())
    if rt and tot > 0:
        acc = 0.0
        items = list(rt.items())
        for i, (reg, w) in enumerate(items):
            wpct = round(broad - acc, 1) if i == len(items) - 1 else round(broad * w / tot, 1)
            acc = round(acc + wpct, 1)
            if wpct > 0:
                rows.append({"kind": "anchor", "ref": f"{reg} 코어 ETF", "weight_pct": wpct})
    elif broad > 0:
        rows.append({"kind": "anchor", "ref": "글로벌 코어 ETF", "weight_pct": broad})
    return rows


def _variant(name: str, cash: float, long_themes: list[str], hedge_list: list[str],
             sector_max: float, inverse_max: float, *, bond_pct: float = 0.0,
             region_targets: dict | None = None, duration: str | None = None) -> list[dict]:
    # CEO 방침: 채권은 **국채(govbond) 위주**이고 **현금의 일부로 취급**(방어자산 family).
    # bond_pct 는 **방어자산(현금밴드)의 비율(0~100)** 이다 — 전체 대비 %가 아니다.
    #   예: 방어 40, bond_pct=25 → 국채 = 40×0.25 = 10(전체%), 순현금 = 30.
    # 방어 총량(=현금밴드)은 유지하되, 그 안에서 ratio 만큼 국채로 배분: defensive = pure_cash + govbond.
    # 국채는 invested(위험자산)에서 빼지 않고 cash 에서 carve 한다(현금 침범 아님 — 현금의 구성).
    cash_total = round(max(0.0, min(100.0, cash)), 1)            # 방어 총량(현금밴드)
    ratio = max(0.0, min(100.0, float(bond_pct or 0.0)))         # 방어자산 중 국채 비율(%)
    govbond = round(cash_total * ratio / 100.0, 1)               # 국채(전체% 절대값) = 방어×비율
    pure_cash = round(cash_total - govbond, 1)                   # 즉시 매수여력(순현금)
    invested = round(100.0 - cash_total, 1)                      # 위험자산 (국채 안 뺌)

    # 헤지(인버스) — 인버스 한도 내 소액 보험 포지션 (롱과 분리)
    hedge_total = round(min(inverse_max, invested * HEDGE_SHARE[name]), 1) if hedge_list else 0.0
    rest = round(invested - hedge_total, 1)
    tilt_total = round(rest * TILT_SHARE[name], 1)
    broad = round(rest - tilt_total, 1)

    rows = [{"kind": "cash", "ref": None, "weight_pct": pure_cash}]
    if govbond > 0:
        rows.append({"kind": "bond", "ref": ("국채·" + duration) if duration else "국채", "weight_pct": govbond})
    if hedge_list:
        hper = round(hedge_total / len(hedge_list), 1)
        for h in hedge_list:
            rows.append({"kind": "hedge", "ref": h + " 인버스", "weight_pct": hper})
    rows.extend(_split_region(broad, region_targets))  # 광범위 anchor → 지역별 anchor
    if long_themes:
        per = min(round(tilt_total / len(long_themes), 1), sector_max)  # 섹터 상한
        for th in long_themes:
            rows.append({"kind": "tilt", "ref": th, "weight_pct": per})

    total = round(sum(r["weight_pct"] for r in rows), 1)
    if total != 100.0:  # 상한으로 깎인 잔여(또는 초과분)는 순현금으로 흡수 (over/under-invest 방지)
        rows[0]["weight_pct"] = round(rows[0]["weight_pct"] + (100 - total), 1)
    # 위험#5: 보정 후에도 합계 100이 아니면(반올림 누적·음수현금 등) hard-fail.
    final = round(sum(r["weight_pct"] for r in rows), 1)
    if final != 100.0:
        raise ValueError(f"allocation 합계가 100%가 아닙니다: {final} (변이={name})")
    return rows


def generate(account_index: int) -> dict:
    """3안(보수/기준/공격) 목표비중 생성. Growth Middleware(run_task) 강제 통과.

    prehook(allocation_generation) 은 account_id 귀속만 게이트한다(정책 미존재 시 본문이
    compile_policy 로 폴백하므로 policy 요구 금지 — 기존 동작 보존). block 이면 본문 미실행."""
    def _impl(_inp, _ctx):
        return _generate_impl(account_index)

    out = growth_mw.run_task("allocation_generation", "broker-chief", _impl,
                             account_index=account_index, input={"account_index": account_index})
    if out["blocked"]:
        return {"ok": False, "error": "; ".join(out["reasons"]) or "prehook gate=block"}
    if not out["ok"]:
        # 본문 예외(예: 합계 100 아님 ValueError) — 기존 main() 의 내부오류 shape 와 동일 계열.
        return {"ok": False, "error": "; ".join(out.get("reasons") or ["내부 오류"])}
    return out["result"]


def _generate_impl(account_index: int) -> dict:
    pol = policy_mod.latest(account_index)
    policy = pol["policy"] if pol else policy_mod.compile_policy(account_index)
    band = policy.get("cash_band", {})
    cmin = band.get("min") if band.get("min") is not None else 10.0
    cmax = band.get("max") if band.get("max") is not None else 40.0
    target = band.get("target") if band.get("target") is not None else round((cmin + cmax) / 2, 1)
    limits = policy.get("limits", {})
    sector_max = limits.get("sector_max_pct", 30.0)
    inverse_max = limits.get("inverse_max_pct", 10.0)

    conn = store_db.connect()
    try:
        prof = conn.execute(
            "SELECT interests_text, hedge_themes, region_targets, bond_target_pct, bond_duration_pref "
            "FROM investor_profile WHERE account_index=?", (account_index,)).fetchone()
    finally:
        conn.close()
    all_themes = _themes(prof["interests_text"] if prof else None)
    hedge_col = _themes(prof["hedge_themes"] if prof else None)
    # 방향성 게이트(CEO): 관심 테마는 neutral. 롱 후보만 tilt, 숏/헤지는 hedge,
    # 관망/방향미정/제외는 allocation 미반영. 자동 long 금지.
    from .field_advisors import resolve_theme_directions  # 지역 import(순환/로드비용 회피)
    dirs = resolve_theme_directions(account_index, all_themes)
    # 롱→tilt, 숏→hedge, 혼재(mixed_swing)→**둘 다**(코어 롱 + 전술 인버스 = 스윙 페어).
    long_themes = [t for t in all_themes if dirs.get(t) in ("long_candidate", "mixed_swing")]
    dir_hedge = [t for t in all_themes if dirs.get(t) in ("short_or_hedge_candidate", "mixed_swing")]
    hedge_list = list(dict.fromkeys(hedge_col + dir_hedge))  # 명시 헤지컬럼 + 방향=숏/헤지/혼재

    # 지역/채권 구조 — investor_profile(source of truth). policy 에도 반영돼 흐른다.
    region_targets: dict = {}
    if prof and prof["region_targets"]:
        try:
            region_targets = json.loads(prof["region_targets"]) or {}
        except (ValueError, TypeError):
            region_targets = {}
    bond_pct = float(prof["bond_target_pct"]) if (prof and prof["bond_target_pct"] is not None) else 0.0
    duration = (prof["bond_duration_pref"] if prof else None) or None

    def mk(name, c):
        return _variant(name, c, long_themes, hedge_list, sector_max, inverse_max,
                        bond_pct=bond_pct, region_targets=region_targets, duration=duration)

    # 변이별 현금: 공격=하한, 기준=목표, 보수=상한
    variants = {
        "conservative": mk("conservative", cmax),
        "base": mk("base", target),
        "aggressive": mk("aggressive", cmin),
    }

    proposal_id = f"alloc-{account_index}-{_now()}"
    conn = store_db.connect()
    try:
        for variant, rows in variants.items():
            for r in rows:
                conn.execute(
                    "INSERT INTO target_allocations(account_index, proposal_id, variant, kind, ref, weight_pct, status, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (account_index, proposal_id, variant, r["kind"], r["ref"], r["weight_pct"], "draft", _now()),
                )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "account_index": account_index,
        "proposal_id": proposal_id,
        "themes": long_themes,
        "hedge_themes": hedge_list,
        "sector_max_pct": sector_max,
        "inverse_max_pct": inverse_max,
        "cash_band": {"min": cmin, "max": cmax, "target": target},
        "region_targets": region_targets,
        "bond": {"target_pct": bond_pct, "duration_pref": duration},
        "variants": variants,
        "note": "테마는 tilt 상한(섹터 한도)으로 제한됩니다. 채권은 현금과 별도 bucket이며 지역 비중은 anchor를 분해합니다. 사람이 한 안을 선택해 목표비중으로 확정하세요.",
    }


# ============================================================
# 관점-aware 비중 (perspective_variants 용 — base 로직 보존, 재프레이밍 전용)
# ============================================================
# CEO: 하나의 정답 금지. 같은 정책/견해라도 *관점*에 따라 현금/위험 강도가 달라진다.
#   A(현재 관점), B(방어적) = 현금/채권↑·위험↓, C(공격적) = 위험/테마↑(단 한도·게이트 준수).
# 핵심: 새 수치 로직을 만들지 않고 기존 _variant(검증된 sum100·섹터상한·국채 carve)을 재사용한다.
#   관점 강도는 **현금 수준(cash_pct)** 으로만 단조롭게 준다 — C 는 현금↓ → invested↑ → 테마/앵커↑,
#   B 는 현금↑ → 방어↑. (tilt-share 를 관점마다 다르게 두면 테마가 없을 때 잔여 tilt 가 현금으로
#   흡수되며 방어 순서가 역전되므로, tilt-share 는 'base' 로 고정해 단조성을 보장한다.)
PERSPECTIVES = ("A", "B", "C")


def variant_for_perspective(account_index: int, perspective: str, *, cash_pct: float,
                            sector_max: float, inverse_max: float, long_themes: list[str],
                            hedge_list: list[str], bond_pct: float = 0.0,
                            region_targets: dict | None = None,
                            duration: str | None = None) -> list[dict]:
    """관점(A/B/C) 1개에 대한 비중 rows 생성. 검증(sum100·한도)은 _variant 가 보장.

    관점 차이는 현금 수준(cash_pct, 호출부의 관점별 현금밴드 매핑)으로만 단조롭게 준다.
    tilt-share 는 'base' 로 고정 — 그래야 테마 유무와 무관하게
    'B 방어↑ ≥ A ≥ C 방어↓' / 'C 위험↑ ≥ A ≥ B' 단조성이 깨지지 않는다.
    base 함수(_variant)·검증을 그대로 통과하므로 한도/합계는 동일 규칙으로 보호된다."""
    if (perspective or "").upper() not in PERSPECTIVES:
        raise ValueError(f"perspective 는 A|B|C 중 하나여야 합니다 (받음: {perspective!r})")
    return _variant("base", cash_pct, long_themes, hedge_list, sector_max, inverse_max,
                    bond_pct=bond_pct, region_targets=region_targets, duration=duration)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--generate", action="store_true")
    args = ap.parse_args()
    try:
        out = generate(args.account) if args.generate else {"ok": False, "error": "--generate"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
