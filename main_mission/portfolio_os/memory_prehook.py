"""memory pre-hook — 자산/시장 판단 **전에** 관련 기억을 모아 요약 컨텍스트를 만든다.

목적(성장형 시스템): "무작정 처음부터 판단" 금지. 작업 전에 과거를 읽는다.
  최신 / 장기 thesis / 시장반응(lesson) / 사용자반응 / stale 을 **분리**해 retrieval +
  판단용 요약을 제공한다. 자동 적용 금지 — 후보순위·confidence·주의문구·질문·draft 후보까지만.

우선순위(prehook_context output 순서/가중) — **계좌가 공통보다 먼저**(CEO 지시):
  ① account_id (계좌 식별 — 모든 판단의 기준)
  ② selected_allocation (계좌 확정 배분 = truth — 최우선 맥락)
  ③ objective (계좌 목적 + "최선" 기준)
  ④ policy/risk (계좌 한도/스타일 + hard rule)
  ⑤ 계좌 과거 결정 (이 계좌의 lesson_run 이력)
  ⑥ lesson reliability (scope 시장 노하우 누적)
  ⑦ user_views (계좌 견해 — 1급 입력, 계좌 격리)
  ⑧ asset_memory (자산 지식: **공통**은 계좌 관점 뒤 — 공통이 계좌 목적을 덮지 않음)
  ⑨ 최신 evidence (evidence_items)
  ⑩ 최신 가격/수급/거시 (price_history / investor_flows / macro_indicators)
  추가: 장기 thesis(time_horizon=long / layer=long) · stale(표시만)

  핵심 원칙: **공통 자산 memory 보다 계좌 목적/정책이 먼저** 온다. 공통 지식은 계좌
  정책을 override 하지 못하며, 계좌 정책이 공통 후보를 제약한다.

**상충 정보도 포함**(장기 긍정 vs 단기 수급 악화)해 conflicts 로 노출 — 한쪽으로 숨기지 않음.
계좌 격리: 사용자 관점·user_views 는 그 account_index 만. 타 계좌 혼입 금지.
지능 = Claude + 메모리 (Anthropic API 미사용 — import 없음). 주문/policy 변경 0.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import asset_memory as am
from . import lesson_runs as lr
from .store import db as store_db

# scope_type → 검색 키 매핑(자산종류별 무엇으로 찾을지)
_SCOPE_TO_TICKERLIKE = {"stock": "ticker", "etf": "ticker"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# ============================================================
# 읽기 헬퍼 (read-only — 절대 쓰지 않음)
# ============================================================
def _latest_price(conn, code: str, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT trade_date, close, volume FROM price_history WHERE instrument_code=? "
        "ORDER BY trade_date DESC LIMIT ?",
        (code, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_flows(conn, code: str, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT trade_date, foreign_net, institution_net, retail_net FROM investor_flows "
        "WHERE instrument_code=? ORDER BY trade_date DESC LIMIT ?",
        (code, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_macro(conn, factors: list[str], limit: int = 1) -> dict:
    out: dict[str, dict] = {}
    for f in factors:
        r = conn.execute(
            "SELECT obs_date, value FROM macro_indicators WHERE indicator=? "
            "ORDER BY obs_date DESC LIMIT ?",
            (f, limit),
        ).fetchone()
        if r:
            out[f] = dict(r)
    return out


def _latest_evidence(conn, *, ticker=None, etf=None, theme=None, limit: int = 5) -> list[dict]:
    where, params = [], []
    if ticker:
        where.append("related_ticker=?")
        params.append(ticker)
    if etf:
        where.append("related_etf=?")
        params.append(etf)
    if theme:
        where.append("related_theme=?")
        params.append(theme)
    if not where:
        return []
    rows = conn.execute(
        "SELECT id, source, source_date, summary, positive_factors, negative_factors, "
        "uncertainties, freshness, confidence, stale FROM evidence_items WHERE ("
        + " OR ".join(where)
        + ") ORDER BY datetime(created_at) DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["stale"] = bool(d["stale"])
        out.append(d)
    return out


def _user_views(conn, account_index: int, *, ticker=None, theme=None) -> list[dict]:
    where = ["account_index=?", "status='active'"]
    params: list = [int(account_index)]
    sub = []
    if ticker:
        sub.append("ticker=?")
        params.append(ticker)
    if theme:
        sub.append("theme=?")
        params.append(theme)
    if sub:
        where.append("(" + " OR ".join(sub) + ")")
    rows = conn.execute(
        "SELECT id, layer, theme, ticker, etf, stance, conviction, horizon, note "
        "FROM user_views WHERE " + " AND ".join(where) + " ORDER BY datetime(updated_at) DESC",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _selected_allocation(conn, account_index: int) -> dict | None:
    """계좌 선택 배분(있으면). 테이블 없으면 None — 결정적으로 graceful."""
    try:
        r = conn.execute(
            "SELECT * FROM selected_allocation WHERE account_index=? "
            "ORDER BY datetime(created_at) DESC LIMIT 1",
            (int(account_index),),
        ).fetchone()
        return dict(r) if r else None
    except Exception:
        return None


# ============================================================
# 상충 탐지 — 장기 긍정 vs 단기 수급/가격 악화 등
# ============================================================
def _detect_conflicts(long_thesis: list, short_signals: dict) -> list[dict]:
    conflicts = []
    # 장기 긍정 메모리/견해 존재?
    long_positive = any(
        (m.get("stance") == "positive") or ("positive" in (m.get("title") or "").lower())
        or (m.get("memory_type") in ("interpretation", "lesson") and (m.get("positive_factors")))
        for m in long_thesis
    )
    # 단기 수급 악화? (최근 외국인+기관 순매도)
    flows = short_signals.get("flows") or []
    recent_outflow = False
    if flows:
        f = flows[0]
        net = (f.get("foreign_net") or 0) + (f.get("institution_net") or 0)
        recent_outflow = net < 0
    if long_positive and recent_outflow:
        conflicts.append({
            "type": "long_positive_vs_short_outflow",
            "note": "장기 긍정 thesis 와 단기 수급 악화(외국인/기관 순매도)가 상충 — 무릎 진입 타이밍 재검토.",
        })
    # 단기 가격 급락?
    prices = short_signals.get("prices") or []
    if long_positive and len(prices) >= 2:
        last, prev = prices[0].get("close"), prices[1].get("close")
        if last and prev and last < prev * 0.97:
            conflicts.append({
                "type": "long_positive_vs_price_drop",
                "note": "장기 긍정 thesis 와 최근 가격 하락이 상충 — 지정가 예측진입(무릎) 후보로만.",
            })
    return conflicts


# ============================================================
# prehook_context — 메인
# ============================================================
def prehook_context(
    account: int | None,
    scope_type: str,
    scope_key: str,
    *,
    ticker=None,
    theme=None,
    macro_factors=None,
    conn=None,
) -> dict:
    """판단 전 컨텍스트 묶음 — 최신/장기/시장반응/사용자반응/stale 분리 + 요약.

    account: 계좌 판단이면 account_index(격리). 시장 공통 조회면 None.
    scope_type/scope_key: 무엇에 대한 판단인지(예: stock/005930).
    ticker/theme: 추가 검색키(없으면 scope 로 추론).
    macro_factors: 거시 지표명 리스트(예: ['yield_10y','fx_usdkrw']).
    자동 적용 없음 — 요약·후보·주의문구·질문까지만.
    """
    st = am._norm_enum(scope_type, am.SCOPE_TYPES, "scope_type")
    skey = _clean(scope_key)
    if skey is None:
        raise ValueError("scope_key 는 필수입니다")
    # ticker 추론: stock/etf scope 면 scope_key 가 ticker.
    if ticker is None and st in _SCOPE_TO_TICKERLIKE:
        ticker = skey
    acct = int(account) if account not in (None, "") else None

    # ★ 계좌 최우선: 계좌 목적/정책/확정배분/과거결정을 *먼저* 모은다(공통 자산 memory 보다 앞).
    #   계좌 격리 — account_index 한정. 시장 공통 조회(account None)면 빈 맥락.
    account_block = None
    if acct is not None:
        try:
            from . import account_memory as _acct_mem  # 지역 import(순환 방지)
            account_block = _acct_mem.account_context(acct)
        except Exception:  # noqa: BLE001 — 계좌 맥락 조회 실패는 graceful(미설정 계좌)
            account_block = None

    own = conn is None
    conn = conn or store_db.connect()
    try:
        # ① selected_allocation
        allocation = _selected_allocation(conn, acct) if acct is not None else None

        # ② user_views (계좌 격리)
        views = _user_views(conn, acct, ticker=ticker, theme=theme) if acct is not None else []

        # ③ asset_memory — 공통 + 그 계좌 사용자 관점(분리해서 둘 다)
        shared_mem = am.search(
            scope_type=st, scope_key=skey, account_index="__shared__", conn=conn
        )
        user_mem = (
            am.search(scope_type=st, scope_key=skey, account_index=acct, conn=conn)
            if acct is not None
            else []
        )

        # ④ 최신 evidence
        evidence = _latest_evidence(conn, ticker=ticker, theme=theme)

        # ⑤ 최신 가격/수급/거시
        prices = _latest_price(conn, ticker) if ticker else []
        flows = _latest_flows(conn, ticker) if ticker else []
        macro = _latest_macro(conn, list(macro_factors)) if macro_factors else {}

        # ⑥ 과거 lesson_runs + reliability
        runs = lr.recent_runs(st, skey, conn=conn)
        rel = lr.reliability(st, skey, conn=conn)

    finally:
        if own:
            conn.close()

    # ── 분리: 최신 vs 장기 thesis vs stale ──
    all_mem = shared_mem + user_mem
    stale_mem = [m for m in all_mem if m["stale"]]
    fresh_mem = [m for m in all_mem if not m["stale"]]
    long_thesis = [
        m for m in fresh_mem
        if (m.get("time_horizon") == "long") or (m.get("memory_type") == "lesson")
    ]
    long_views = [v for v in views if v.get("layer") == "long" or v.get("horizon") == "long"]
    short_views = [v for v in views if v.get("layer") == "short" or v.get("horizon") == "short"]

    # 출처 없는 강한 기억은 신뢰 불가로 분리(강한 조언 사용 금지).
    weak_mem = [m for m in fresh_mem if m.get("weak")]

    short_signals = {"prices": prices, "flows": flows, "macro": macro}
    conflicts = _detect_conflicts(long_thesis + long_views, short_signals)

    summary = _summarize(
        scope_type=st, scope_key=skey, allocation=allocation, views=views,
        shared_mem=shared_mem, user_mem=user_mem, evidence=evidence,
        prices=prices, flows=flows, macro=macro, reliability=rel,
        conflicts=conflicts, stale_mem=stale_mem, weak_mem=weak_mem,
        account_block=account_block,
    )

    return {
        "scope_type": st,
        "scope_key": skey,
        "account_index": acct,                      # ① 계좌 식별 — 모든 판단의 기준
        # ★ 계좌 최우선 맥락(공통 자산 memory 보다 앞) — 자동 적용 아님, 계좌 격리.
        "account_context": account_block,           # 목적/정책/확정배분/과거결정 통합
        "account_objective": (account_block or {}).get("objective"),   # ③ 계좌 목적
        "account_policy": (account_block or {}).get("policy"),         # ④ 계좌 한도/스타일
        "account_lessons": (account_block or {}).get("lessons"),       # ⑤ 이 계좌 과거결정
        # 우선순위 순서대로 (자동 적용 아님)
        "selected_allocation": allocation,          # ② 확정 배분 = truth
        "user_views": views,                        # ⑦ (계좌 격리)
        "asset_memory_shared": shared_mem,          # ⑧ 공통 자산지식(계좌 관점 뒤)
        "asset_memory_user": user_mem,              # ⑦ 그 계좌 관점(격리)
        "evidence": evidence,                       # ⑨
        "latest_price": prices,                     # ⑤
        "latest_flows": flows,                      # ⑤
        "latest_macro": macro,                      # ⑤
        "lesson_runs": runs,                        # ⑥
        "reliability": rel,                         # ⑥
        # 분리 뷰
        "long_thesis": long_thesis,                 # ⑦ 장기
        "long_views": long_views,
        "short_views": short_views,
        "stale": stale_mem,                         # ⑧ 표시만
        "weak_unsourced": weak_mem,                 # 출처없는 강한기억(신뢰 불가)
        "conflicts": conflicts,                     # 상충 정보 노출
        "summary": summary,
        # 안전 단언(자동 적용 아님)
        "advisory_only": True,
        "applied": False,
        "generated_at": _now(),
    }


def _summarize(**kw) -> dict:
    """무작정 나열 금지 — 판단용 짧은 요약. 출처/주의문구 포함. **계좌 맥락이 먼저.**"""
    notes: list[str] = []
    questions: list[str] = []

    # ★ 계좌 최우선 — 목적/정책이 공통 자산지식 앞에 온다.
    ab = kw.get("account_block")
    if ab:
        obj = ab.get("objective") or {}
        if obj.get("is_set"):
            g = (obj.get("objective") or {}).get("investment_goal")
            notes.append(f"[계좌 최우선] 목적={g} → 같은 자산도 이 목적 기준으로 해석.")
        else:
            notes.append("[계좌 최우선] 목적 미설정 — 기준을 가정하지 않음(먼저 입력 권장).")
        if ab.get("lessons"):
            notes.append(f"[계좌] 이 계좌 과거 판단 {len(ab['lessons'])}건(격리).")

    if kw["allocation"]:
        notes.append("선택된 계좌 배분 존재 — 이 맥락에서 판단.")
    if kw["views"]:
        notes.append(f"사용자 견해 {len(kw['views'])}건(1급 입력) — 데이터와 일치/충돌 설명 필요.")
    if kw["shared_mem"]:
        notes.append(f"공통 자산지식 {len(kw['shared_mem'])}건.")
    if kw["user_mem"]:
        notes.append(f"이 계좌 관점 메모리 {len(kw['user_mem'])}건(격리).")
    if kw["flows"]:
        f = kw["flows"][0]
        net = (f.get("foreign_net") or 0) + (f.get("institution_net") or 0)
        notes.append(f"최근 수급 외국인+기관 순매수={net:+.0f}.")
    if kw["reliability"]["evaluated"]:
        notes.append(
            f"이 scope reliability={kw['reliability']['reliability']} "
            f"(평가 {kw['reliability']['evaluated']}건)."
        )

    cautions: list[str] = []
    if kw["stale_mem"]:
        cautions.append(f"stale 메모리 {len(kw['stale_mem'])}건 — 최신처럼 사용 금지(재확인 필요).")
    if kw["weak_mem"]:
        cautions.append(f"출처 없는 강한 기억 {len(kw['weak_mem'])}건 — 강한 조언에 사용 금지.")
    for c in kw["conflicts"]:
        cautions.append("상충: " + c["note"])

    if kw["conflicts"]:
        questions.append("장기 긍정 thesis 가 유지되는가, 아니면 단기 악화가 추세 전환인가?")
    if kw["views"] and not kw["evidence"]:
        questions.append("사용자 견해를 뒷받침/반박할 evidence 가 부족 — 자료 조사 필요?")

    return {
        "notes": notes,
        "cautions": cautions,       # 주의문구(stale/출처없음/상충)
        "questions": questions,     # 사람에게 던질 질문(자동 결정 아님)
        "decision": None,           # 자동 결정 안 함 — 사람 승인 경로
    }


# ============================================================
# CLI
# ============================================================
def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="memory_prehook — 판단 전 컨텍스트")
    p.add_argument("--account", type=int)
    p.add_argument("--scope-type", required=True)
    p.add_argument("--scope-key", required=True)
    p.add_argument("--ticker")
    p.add_argument("--theme")
    p.add_argument("--macro", nargs="*")
    a = p.parse_args(argv)
    out = prehook_context(
        a.account, a.scope_type, a.scope_key,
        ticker=a.ticker, theme=a.theme, macro_factors=a.macro,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
