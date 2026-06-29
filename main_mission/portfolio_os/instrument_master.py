"""종목/ETF 공통 지식 계층 (계좌 무관) — instrument_master + theme/sector 매핑.

CEO 지시(2026-06-29): 종목/ETF DB·Memory 계층 명확화.
  - 이 계층은 "투자 가능 후보군 + 분류 체계"를 관리한다(특정 계좌 판단 아님).
  - 테마/섹터/시장/자산군(개별주 vs ETF) 분류를 정규화해 추천 후보군을 확장한다.

정직 원칙(불변, CEO 금지조항):
  - 가짜 티커 임의 생성 금지 — 시드는 **실재 종목만**. KRX 는 DART corp_map 으로 검증,
    검증 실패 종목은 정직하게 스킵(가짜 corp_code 추측 금지). US 는 큐레이트한 실재 심볼.
  - 섹터/테마 불명확한 종목 억지 분류 금지 — 시드에 명시된 매핑만.
  - 계좌 성향 결합은 이 계층이 아니라 stock_reco(추천)에서 — 여기는 공통 사실만.

기존 구조와 관계:
  - asset_memory(scope=stock/etf/sector/theme, account NULL=공통)와 보완 — master 는 정규화된
    분류 사실, asset_memory 는 누적 해석/메모리. 중복 아님.
  - security_selection._TICKER_META(하드코딩 14개)를 흡수·확장(시드에 포함).

  python -m main_mission.portfolio_os.instrument_master --seed       # 검증 후 적재
  python -m main_mission.portfolio_os.instrument_master --theme 반도체 --kind stock
  python -m main_mission.portfolio_os.instrument_master --list-themes
"""
from __future__ import annotations

import argparse
import json
import sys

from .store import db as store_db

# ETF 자산군 집합(개별주 vs ETF 구분의 SSOT).
_ETF_CLASSES = ("equity_etf", "inverse_etf", "leveraged_etf", "bond_etf", "dividend_etf")


def _flags(asset_class: str) -> tuple[int, int, int]:
    """asset_class → (is_etf, is_inverse, is_leveraged)."""
    is_etf = 1 if asset_class in _ETF_CLASSES else 0
    is_inverse = 1 if asset_class == "inverse_etf" else 0
    is_leveraged = 1 if asset_class == "leveraged_etf" else 0
    return is_etf, is_inverse, is_leveraged


# ─────────────────────────────────────────────────────────────────────────────
# 단일 원본(설정 파일) — config/portfolio/instruments.json.
#   코드에 종목/ETF/테마/섹터/bucket 을 하드코딩하지 않는다(하드코딩 다 제거 대상, CEO).
#   이 파일만 수정하면 master DB·bucket 시드·테마/섹터·_TICKER_META 가 모두 갱신된다.
#   실재 종목만(KRX 는 seed 시 DART corp_map 검증).
# ─────────────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    """instruments.json(단일 원본) 로드 — 중앙 로더 configs 경유(코드 하드코딩 금지)."""
    from . import configs
    return configs.load("instruments")


def _seed_records() -> list[dict]:
    """config instruments → seed() 내부 스키마."""
    out: list[dict] = []
    for i in load_config().get("instruments", []):
        out.append({"t": i["ticker"], "m": i["market"], "n": i["name"], "ac": i["asset_class"],
                    "sec": i.get("sector"), "ps": i.get("parent_sector"),
                    "th": [(t["theme"], t.get("relation", "core")) for t in i.get("themes", [])]})
    return out


def bucket_specs() -> dict:
    """security_selection 용 — {bucket:{label,kind,note,seed:[tickers]}} (config 단일 원본).

    하드코딩 BUCKETS 대체. seed 는 instruments 의 bucket 필드에서 모은다.
    """
    cfg = load_config()
    specs = {k: dict(v) for k, v in cfg.get("buckets", {}).items()}
    for b in specs.values():
        b.setdefault("seed", [])
    for i in cfg.get("instruments", []):
        bk = i.get("bucket")
        if bk and bk in specs:
            specs[bk]["seed"].append(i["ticker"])
    return specs


def ticker_meta_map() -> dict:
    """ticker → {name, market, asset_class} (config 단일 원본). 하드코딩 _TICKER_META 대체."""
    return {i["ticker"]: {"name": i["name"], "market": i["market"], "asset_class": i["asset_class"]}
            for i in load_config().get("instruments", [])}


def _verify(ticker: str, market: str) -> str | None:
    """실재 검증 → verified_source('dart'|'curated') 또는 None(검증 실패=스킵).

    KRX 는 DART corp_map 으로 실재 확인(가짜 티커 차단). US 는 큐레이트한 실재 심볼(curated).
    """
    if market == "KRX":
        try:
            from . import financials_connect as fc
            return "dart" if fc.resolve_corp_code(ticker) else None
        except Exception:  # noqa: BLE001 — 미연동/미존재면 검증 실패(가짜 corp_code 추측 금지)
            return None
    # US: 시드는 큐레이트한 실재 심볼만(임의 생성 아님).
    return "curated"


def seed(*, conn=None) -> dict:
    """큐레이션 시드를 검증 후 적재(멱등 upsert). 검증 실패는 정직하게 스킵."""
    own = conn is None
    conn = conn or store_db.connect()
    loaded, skipped = [], []
    records = _seed_records()
    try:
        for rec in records:
            tk, mkt = rec["t"], rec["m"]
            vsrc = _verify(tk, mkt)
            if vsrc is None:
                skipped.append({"ticker": tk, "reason": "실재 검증 실패(DART corp_map 미존재) — 가짜 금지 스킵"})
                continue
            ac = rec["ac"]
            is_etf, is_inv, is_lev = _flags(ac)
            ccy = "KRW" if mkt == "KRX" else "USD"
            country = "KR" if mkt == "KRX" else "US"
            conn.execute(
                "INSERT INTO instrument_master(ticker, market, name, asset_class, is_etf, is_inverse, "
                "is_leveraged, country, currency, exchange, verified, verified_source, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,1,?,datetime('now')) "
                "ON CONFLICT(ticker) DO UPDATE SET market=excluded.market, name=excluded.name, "
                "asset_class=excluded.asset_class, is_etf=excluded.is_etf, is_inverse=excluded.is_inverse, "
                "is_leveraged=excluded.is_leveraged, country=excluded.country, currency=excluded.currency, "
                "verified=1, verified_source=excluded.verified_source, updated_at=datetime('now')",
                (tk, mkt, rec["n"], ac, is_etf, is_inv, is_lev, country, ccy, mkt, vsrc),
            )
            # sector
            if rec.get("sec"):
                conn.execute(
                    "INSERT OR IGNORE INTO instrument_sector_map(ticker, sector_key, parent_sector, source) "
                    "VALUES(?,?,?,'curated')", (tk, rec["sec"], rec.get("ps")))
            # themes
            for theme, relation in rec.get("th", []):
                conn.execute(
                    "INSERT OR IGNORE INTO instrument_theme_map(ticker, theme_key, relation, source) "
                    "VALUES(?,?,?,'curated')", (tk, theme, relation))
            loaded.append(tk)
        conn.commit()
    finally:
        if own:
            conn.close()
    return {"ok": True, "loaded": len(loaded), "skipped": len(skipped),
            "skipped_detail": skipped, "total_seed": len(records)}


def _kind_clause(kind: str) -> str:
    """kind 필터 → SQL where 조각. stock=개별주만, etf=ETF만, all=전체."""
    if kind == "stock":
        return " AND m.is_etf=0"
    if kind == "etf":
        return " AND m.is_etf=1"
    return ""


def by_theme(theme: str, *, kind: str = "all", conn=None) -> list[dict]:
    """테마에 속한 종목/ETF(검증된 것만). kind: stock|etf|all."""
    own = conn is None
    conn = conn or store_db.connect()
    try:
        rows = conn.execute(
            "SELECT m.ticker, m.market, m.name, m.asset_class, m.is_etf, m.is_inverse, m.is_leveraged, "
            "tm.relation FROM instrument_master m JOIN instrument_theme_map tm ON tm.ticker=m.ticker "
            "WHERE tm.theme_key=? AND m.verified=1" + _kind_clause(kind) +
            " ORDER BY (tm.relation='core') DESC, m.is_etf, m.ticker",
            (theme,)).fetchall()
    finally:
        if own:
            conn.close()
    return [dict(r) for r in rows]


def by_bucket(bucket: str, *, kind: str = "all", conn=None) -> list[dict]:
    """선정 위저드 bucket(global_core/robotics/semiconductor/...)의 후보 — config seed → master.

    bucket 시드(config instruments 의 bucket 필드)에 속한 검증된 종목/ETF. kind: stock|etf|all.
    """
    seed = bucket_specs().get(bucket, {}).get("seed", [])
    if not seed:
        return []
    own = conn is None
    conn = conn or store_db.connect()
    try:
        out: list[dict] = []
        for tk in seed:
            r = conn.execute(
                "SELECT ticker, market, name, asset_class, is_etf, is_inverse, is_leveraged "
                "FROM instrument_master WHERE ticker=? AND verified=1", (tk,)).fetchone()
            if not r:
                continue
            if kind == "stock" and r["is_etf"]:
                continue
            if kind == "etf" and not r["is_etf"]:
                continue
            out.append(dict(r))
        return out
    finally:
        if own:
            conn.close()


def by_sector(sector: str, *, kind: str = "all", conn=None) -> list[dict]:
    own = conn is None
    conn = conn or store_db.connect()
    try:
        rows = conn.execute(
            "SELECT m.ticker, m.market, m.name, m.asset_class, m.is_etf, m.is_inverse, m.is_leveraged "
            "FROM instrument_master m JOIN instrument_sector_map sm ON sm.ticker=m.ticker "
            "WHERE sm.sector_key=? AND m.verified=1" + _kind_clause(kind) +
            " ORDER BY m.is_etf, m.ticker", (sector,)).fetchall()
    finally:
        if own:
            conn.close()
    return [dict(r) for r in rows]


def get(ticker: str, *, conn=None) -> dict | None:
    own = conn is None
    conn = conn or store_db.connect()
    try:
        r = conn.execute("SELECT * FROM instrument_master WHERE ticker=?", (ticker,)).fetchone()
    finally:
        if own:
            conn.close()
    return dict(r) if r else None


def list_themes(*, conn=None) -> list[dict]:
    own = conn is None
    conn = conn or store_db.connect()
    try:
        rows = conn.execute(
            "SELECT theme_key, COUNT(*) n, SUM(CASE WHEN m.is_etf=1 THEN 1 ELSE 0 END) etf_n "
            "FROM instrument_theme_map tm JOIN instrument_master m ON m.ticker=tm.ticker "
            "WHERE m.verified=1 GROUP BY theme_key ORDER BY n DESC").fetchall()
    finally:
        if own:
            conn.close()
    return [{"theme": r["theme_key"], "count": r["n"], "etf_count": r["etf_n"]} for r in rows]


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="종목/ETF 공통 지식 계층(instrument_master)")
    ap.add_argument("--seed", action="store_true", help="큐레이션 시드 적재(검증 후)")
    ap.add_argument("--theme", help="테마별 후보 조회")
    ap.add_argument("--sector", help="섹터별 후보 조회")
    ap.add_argument("--kind", default="all", choices=["stock", "etf", "all"])
    ap.add_argument("--list-themes", action="store_true")
    args = ap.parse_args(argv)

    if args.seed:
        _emit(seed())
    elif args.theme:
        _emit({"ok": True, "theme": args.theme, "kind": args.kind, "candidates": by_theme(args.theme, kind=args.kind)})
    elif args.sector:
        _emit({"ok": True, "sector": args.sector, "kind": args.kind, "candidates": by_sector(args.sector, kind=args.kind)})
    elif args.list_themes:
        _emit({"ok": True, "themes": list_themes()})
    else:
        _emit({"ok": False, "error": "--seed | --theme T | --sector S | --list-themes"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
