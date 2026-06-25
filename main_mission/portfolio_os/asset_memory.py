"""자산별/시장별 누적 메모리 — 종목/ETF/섹터/테마/거시/이벤트/정책 지식이 시간이 지날수록 축적.

핵심 원칙(불변):
- **공통 자산지식(account_index NULL)** 과 **사용자 관점(account_index/user_id 지정)** 을 **분리**한다.
  교차 덮어쓰기 금지: record() 가 공통/사용자를 섞지 않으며, search() 는 scope filter 로 격리.
- **출처 없는 강한 메모리 금지**: 강한 confidence(>= STRONG_CONFIDENCE) 를 주장하려면
  evidence_id 또는 source(+source_date) + freshness 가 있어야 한다. 없으면 자동으로 약한
  confidence(WEAK_CONFIDENCE_CAP)로 강등하고 weak=True 로 표시한다.
- **자동 적용/주문/policy 변경 0**: 이 모듈은 지식 저장/조회만. 판단·주문은 사람 승인 경로.
- **stale 표시**: stale_at 경과 또는 stale=1 이면 stale 로 표시(최신처럼 사용 금지).
- 지능 = Claude + 메모리 (Anthropic API 미사용 — import 없음).

테이블(이미 생성됨, 스키마 편집 금지): asset_memory
  (id, scope_type, scope_key, memory_type, account_index, user_id,
   ticker, market, sector, theme, asset_class, bucket, related_etf, related_stock,
   macro_factor, event_type, time_horizon, title, body,
   positive_factors, negative_factors, uncertainties, evidence_id,
   source, source_date, freshness, confidence, reliability,
   stale, stale_at, last_verified_at, last_used_at, created_at)

CLI:
  python -m main_mission.portfolio_os.asset_memory --record --scope-type stock --scope-key 005930 \
      --memory-type fact --ticker 005930 --title "외국인 순매수 전환" --source kis_investor
  python -m main_mission.portfolio_os.asset_memory --search --scope-type stock --scope-key 005930
  python -m main_mission.portfolio_os.asset_memory --growth --scope-type stock --scope-key 005930
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .store import db as store_db

# ── enum (스키마 주석 SSOT) ──
SCOPE_TYPES = ("stock", "etf", "sector", "theme", "macro", "event", "policy")
MEMORY_TYPES = ("fact", "interpretation", "user_view", "outcome", "lesson")

# 강한 메모리(출처 필수) 임계 / 출처 없을 때 강등 상한
STRONG_CONFIDENCE = 0.6
WEAK_CONFIDENCE_CAP = 0.35


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _norm_enum(value, allowed, field) -> str:
    v = _clean(value)
    if v is None:
        raise ValueError(f"{field} 는 필수입니다")
    v = v.lower()
    if v not in allowed:
        raise ValueError(f"{field} 는 {allowed} 중 하나여야 합니다 (받음: {value!r})")
    return v


def _norm_unit(value, field) -> float | None:
    if value is None or value == "":
        return None
    f = float(value)
    if not (0.0 <= f <= 1.0):
        raise ValueError(f"{field} 는 0~1 범위여야 합니다 (받음: {value!r})")
    return f


def _has_source(evidence_id, source, source_date, freshness) -> bool:
    """강한 메모리 자격: evidence 연결 OR (source + source_date + freshness 존재)."""
    if evidence_id is not None:
        return True
    return bool(_clean(source)) and bool(_clean(source_date)) and freshness is not None


def _is_stale(row) -> bool:
    if row["stale"]:
        return True
    sa = row["stale_at"]
    if not sa:
        return False
    try:
        return _parse(sa) <= datetime.now(timezone.utc)
    except Exception:
        return False


def _parse(ts: str) -> datetime:
    s = str(ts).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _factors_to_text(v) -> str | None:
    """list/dict 면 JSON 직렬화, 문자열이면 그대로."""
    if v is None:
        return None
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return _clean(v)


# ============================================================
# record — 공통 vs 사용자 관점 분리 저장
# ============================================================
def record(
    scope_type: str,
    scope_key: str,
    memory_type: str,
    *,
    account_index: int | None = None,
    user_id: int | None = None,
    ticker=None,
    market=None,
    sector=None,
    theme=None,
    asset_class=None,
    bucket=None,
    related_etf=None,
    related_stock=None,
    macro_factor=None,
    event_type=None,
    time_horizon=None,
    title=None,
    body=None,
    positive_factors=None,
    negative_factors=None,
    uncertainties=None,
    evidence_id: int | None = None,
    source=None,
    source_date=None,
    freshness: float | None = None,
    confidence: float | None = None,
    reliability: float | None = None,
    stale_at=None,
    conn=None,
) -> dict:
    """asset_memory 한 행 적재.

    분리 규칙:
      - account_index/user_id 둘 다 None → **공통 자산지식**(시장 공통 노하우).
      - account_index 지정 → **그 계좌 사용자 관점**(격리). memory_type=user_view 권장.
    출처 게이트:
      - confidence 가 STRONG_CONFIDENCE 이상인데 출처(evidence/source)가 없으면
        WEAK_CONFIDENCE_CAP 로 강등하고 결과에 downgraded=True 표시.
    자동 적용/주문/policy 변경 없음 — 저장만.
    """
    scope_type = _norm_enum(scope_type, SCOPE_TYPES, "scope_type")
    memory_type = _norm_enum(memory_type, MEMORY_TYPES, "memory_type")
    skey = _clean(scope_key)
    if skey is None:
        raise ValueError("scope_key 는 필수입니다")

    freshness = _norm_unit(freshness, "freshness")
    confidence = _norm_unit(confidence, "confidence")
    reliability = _norm_unit(reliability, "reliability")

    acct = int(account_index) if account_index not in (None, "") else None
    uid = int(user_id) if user_id not in (None, "") else None

    # 출처 게이트 — 출처 없는 강한 메모리 차단(약하게 강등).
    downgraded = False
    has_src = _has_source(evidence_id, source, source_date, freshness)
    if confidence is not None and confidence >= STRONG_CONFIDENCE and not has_src:
        confidence = WEAK_CONFIDENCE_CAP
        downgraded = True

    own = conn is None
    conn = conn or store_db.connect()
    now = _now()
    try:
        cur = conn.execute(
            "INSERT INTO asset_memory("
            "scope_type, scope_key, memory_type, account_index, user_id, "
            "ticker, market, sector, theme, asset_class, bucket, related_etf, related_stock, "
            "macro_factor, event_type, time_horizon, title, body, "
            "positive_factors, negative_factors, uncertainties, evidence_id, "
            "source, source_date, freshness, confidence, reliability, stale, stale_at, "
            "last_verified_at, last_used_at, created_at) "
            "VALUES(?,?,?,?,?, ?,?,?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?,?,?,?, ?,?,?)",
            (
                scope_type, skey, memory_type, acct, uid,
                _clean(ticker), _clean(market), _clean(sector), _clean(theme),
                _clean(asset_class), _clean(bucket), _clean(related_etf), _clean(related_stock),
                _clean(macro_factor), _clean(event_type), _clean(time_horizon),
                _clean(title), _factors_to_text(body),
                _factors_to_text(positive_factors), _factors_to_text(negative_factors),
                _factors_to_text(uncertainties), evidence_id,
                _clean(source), _clean(source_date), freshness, confidence, reliability,
                0, _clean(stale_at),
                now, None, now,
            ),
        )
        conn.commit()
        mem_id = cur.lastrowid
        return {
            "ok": True,
            "id": mem_id,
            "scope_type": scope_type,
            "scope_key": skey,
            "memory_type": memory_type,
            "account_index": acct,
            "user_id": uid,
            "shared": acct is None and uid is None,
            "confidence": confidence,
            "has_source": has_src,
            "downgraded": downgraded,  # 출처 없어 강등됨
        }
    finally:
        if own:
            conn.close()


# ============================================================
# get / search
# ============================================================
def get(memory_id: int, *, conn=None) -> dict | None:
    own = conn is None
    conn = conn or store_db.connect()
    try:
        r = conn.execute("SELECT * FROM asset_memory WHERE id=?", (int(memory_id),)).fetchone()
        return _row(r) if r else None
    finally:
        if own:
            conn.close()


def search(
    *,
    scope_type=None,
    scope_key=None,
    memory_type=None,
    ticker=None,
    sector=None,
    theme=None,
    bucket=None,
    account_index="__shared__",
    user_id=None,
    min_freshness: float | None = None,
    min_confidence: float | None = None,
    include_stale: bool = True,
    limit: int = 50,
    conn=None,
) -> list[dict]:
    """검색키 매칭 + freshness/confidence filter + scope filter.

    account_index 의미(격리):
      - "__shared__"(기본) → 공통 자산지식만(account_index IS NULL).
      - 정수 N → 그 계좌 사용자 관점만(account_index=N). **타 계좌 메모리 혼입 금지**.
      - None → 격리 해제(공통+모든 계좌) — 일반적으로 prehook 내부에서만 사용.
    ticker 는 exact, sector/theme/bucket 은 정확 매칭.
    """
    where = []
    params: list = []
    if scope_type is not None:
        where.append("scope_type=?")
        params.append(_norm_enum(scope_type, SCOPE_TYPES, "scope_type"))
    if _clean(scope_key):
        where.append("scope_key=?")
        params.append(_clean(scope_key))
    if memory_type is not None:
        where.append("memory_type=?")
        params.append(_norm_enum(memory_type, MEMORY_TYPES, "memory_type"))
    if _clean(ticker):
        where.append("ticker=?")
        params.append(_clean(ticker))
    if _clean(sector):
        where.append("sector=?")
        params.append(_clean(sector))
    if _clean(theme):
        where.append("theme=?")
        params.append(_clean(theme))
    if _clean(bucket):
        where.append("bucket=?")
        params.append(_clean(bucket))

    # ── scope 격리 ──
    if account_index == "__shared__":
        where.append("account_index IS NULL")
    elif account_index is not None:
        where.append("account_index=?")
        params.append(int(account_index))
    if user_id is not None:
        where.append("user_id=?")
        params.append(int(user_id))

    if min_freshness is not None:
        where.append("(freshness IS NOT NULL AND freshness >= ?)")
        params.append(float(min_freshness))
    if min_confidence is not None:
        where.append("(confidence IS NOT NULL AND confidence >= ?)")
        params.append(float(min_confidence))

    sql = "SELECT * FROM asset_memory"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY datetime(created_at) DESC LIMIT ?"
    params.append(int(limit))

    own = conn is None
    conn = conn or store_db.connect()
    try:
        rows = [_row(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        if own:
            conn.close()

    if not include_stale:
        rows = [r for r in rows if not r["stale"]]
    return rows


def mark_used(memory_id: int, *, conn=None) -> None:
    own = conn is None
    conn = conn or store_db.connect()
    try:
        conn.execute("UPDATE asset_memory SET last_used_at=? WHERE id=?", (_now(), int(memory_id)))
        conn.commit()
    finally:
        if own:
            conn.close()


def mark_stale(memory_id: int, *, reason: str | None = None, conn=None) -> None:
    own = conn is None
    conn = conn or store_db.connect()
    try:
        conn.execute("UPDATE asset_memory SET stale=1 WHERE id=?", (int(memory_id),))
        conn.commit()
    finally:
        if own:
            conn.close()


def _row(r) -> dict:
    d = dict(r)
    d["stale"] = bool(_is_stale(r))
    d["has_source"] = _has_source(r["evidence_id"], r["source"], r["source_date"], r["freshness"])
    # 출처 없는데 비-사소한 주장(confidence 가 약상한 이상이거나 미상)이면 신뢰 불가 표시.
    # record() 가 강등하면 confidence==WEAK_CONFIDENCE_CAP 로 앉으므로 그 경계도 weak 로 잡는다.
    conf = r["confidence"]
    d["weak"] = bool(
        not d["has_source"]
        and (conf is None or conf >= WEAK_CONFIDENCE_CAP)
    )
    return d


# ============================================================
# growth_report — 이 scope 의 성장 현황(새 evidence·stale·view 변경·reliability)
# ============================================================
def growth_report(scope_type: str, scope_key: str, *, account_index=None, conn=None) -> dict:
    """한 자산/시장 scope 의 메모리 성장 스냅샷.

    - 새/최근 메모리, stale 목록, 사용자 관점(view) 변경, reliability 분포.
    자동 적용 없음 — 보고만.
    """
    scope_type = _norm_enum(scope_type, SCOPE_TYPES, "scope_type")
    skey = _clean(scope_key)
    own = conn is None
    conn = conn or store_db.connect()
    try:
        rows = [
            _row(r)
            for r in conn.execute(
                "SELECT * FROM asset_memory WHERE scope_type=? AND scope_key=? "
                "ORDER BY datetime(created_at) DESC",
                (scope_type, skey),
            ).fetchall()
        ]
    finally:
        if own:
            conn.close()

    shared = [r for r in rows if r["account_index"] is None]
    user_scoped = [r for r in rows if r["account_index"] is not None]
    stale = [r for r in rows if r["stale"]]
    weak = [r for r in rows if r["weak"]]
    with_evidence = [r for r in rows if r["evidence_id"] is not None]
    rels = [r["reliability"] for r in rows if r["reliability"] is not None]
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["memory_type"]] = by_type.get(r["memory_type"], 0) + 1

    return {
        "scope_type": scope_type,
        "scope_key": skey,
        "total": len(rows),
        "shared_count": len(shared),
        "user_scoped_count": len(user_scoped),
        "by_memory_type": by_type,
        "stale_count": len(stale),
        "weak_count": len(weak),  # 출처 없는 강한 주장(신뢰 불가)
        "with_evidence_count": len(with_evidence),
        "reliability_avg": round(sum(rels) / len(rels), 3) if rels else None,
        "latest": rows[0] if rows else None,
        "stale_items": [{"id": r["id"], "title": r["title"]} for r in stale],
        "user_views": [
            {"id": r["id"], "account_index": r["account_index"], "title": r["title"], "stale": r["stale"]}
            for r in user_scoped
            if r["memory_type"] == "user_view"
        ],
    }


# ============================================================
# CLI
# ============================================================
def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="asset_memory — 자산별 누적 메모리")
    p.add_argument("--record", action="store_true")
    p.add_argument("--search", action="store_true")
    p.add_argument("--growth", action="store_true")
    p.add_argument("--scope-type")
    p.add_argument("--scope-key")
    p.add_argument("--memory-type", default="fact")
    p.add_argument("--account", type=int)
    p.add_argument("--ticker")
    p.add_argument("--sector")
    p.add_argument("--theme")
    p.add_argument("--title")
    p.add_argument("--body")
    p.add_argument("--source")
    p.add_argument("--source-date")
    p.add_argument("--freshness", type=float)
    p.add_argument("--confidence", type=float)
    p.add_argument("--evidence-id", type=int)
    a = p.parse_args(argv)

    if a.record:
        out = record(
            a.scope_type, a.scope_key, a.memory_type,
            account_index=a.account, ticker=a.ticker, sector=a.sector, theme=a.theme,
            title=a.title, body=a.body, source=a.source, source_date=a.source_date,
            freshness=a.freshness, confidence=a.confidence, evidence_id=a.evidence_id,
        )
    elif a.search:
        out = search(
            scope_type=a.scope_type, scope_key=a.scope_key,
            ticker=a.ticker, sector=a.sector, theme=a.theme,
            account_index=a.account if a.account is not None else "__shared__",
        )
    elif a.growth:
        out = growth_report(a.scope_type, a.scope_key)
    else:
        p.print_help()
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
