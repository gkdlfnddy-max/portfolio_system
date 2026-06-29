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
# 큐레이션 시드 — 실재 종목/ETF 만. (ticker, market, name, asset_class, sector,
#   parent_sector, [(theme, relation), ...])  KRX 는 DART 검증, US 는 curated.
#   섹터/테마가 명확한 것만. 불명확하면 넣지 않음(정직).
# ─────────────────────────────────────────────────────────────────────────────
_SEED: list[dict] = [
    # ── 글로벌 코어 ETF (테마 무관 — 분류만) ──
    {"t": "SPY", "m": "US", "n": "SPDR S&P 500 ETF", "ac": "equity_etf", "sec": "광범위지수", "ps": "지수", "th": []},
    {"t": "VOO", "m": "US", "n": "Vanguard S&P 500 ETF", "ac": "equity_etf", "sec": "광범위지수", "ps": "지수", "th": []},
    {"t": "QQQ", "m": "US", "n": "Invesco QQQ (Nasdaq-100)", "ac": "equity_etf", "sec": "기술지수", "ps": "지수", "th": [("AI", "adjacent")]},
    {"t": "VT", "m": "US", "n": "Vanguard Total World Stock ETF", "ac": "equity_etf", "sec": "글로벌지수", "ps": "지수", "th": []},
    {"t": "VTI", "m": "US", "n": "Vanguard Total US Stock Market ETF", "ac": "equity_etf", "sec": "미국전체", "ps": "지수", "th": []},

    # ── 반도체 (개별주 확장 — 검증 기준) ──
    {"t": "005930", "m": "KRX", "n": "삼성전자", "ac": "stock", "sec": "반도체", "ps": "IT", "th": [("반도체", "core"), ("AI", "adjacent")]},
    {"t": "000660", "m": "KRX", "n": "SK하이닉스", "ac": "stock", "sec": "반도체", "ps": "IT", "th": [("반도체", "core"), ("AI", "adjacent")]},
    {"t": "042700", "m": "KRX", "n": "한미반도체", "ac": "stock", "sec": "반도체장비", "ps": "IT", "th": [("반도체", "core")]},
    {"t": "240810", "m": "KRX", "n": "원익IPS", "ac": "stock", "sec": "반도체장비", "ps": "IT", "th": [("반도체", "core")]},
    {"t": "058470", "m": "KRX", "n": "리노공업", "ac": "stock", "sec": "반도체부품", "ps": "IT", "th": [("반도체", "core")]},
    {"t": "039030", "m": "KRX", "n": "이오테크닉스", "ac": "stock", "sec": "반도체장비", "ps": "IT", "th": [("반도체", "core")]},
    {"t": "000990", "m": "KRX", "n": "DB하이텍", "ac": "stock", "sec": "반도체", "ps": "IT", "th": [("반도체", "core")]},
    {"t": "036930", "m": "KRX", "n": "주성엔지니어링", "ac": "stock", "sec": "반도체장비", "ps": "IT", "th": [("반도체", "core")]},
    {"t": "NVDA", "m": "US", "n": "NVIDIA", "ac": "stock", "sec": "반도체", "ps": "IT", "th": [("반도체", "core"), ("AI", "core")]},
    {"t": "AMD", "m": "US", "n": "Advanced Micro Devices", "ac": "stock", "sec": "반도체", "ps": "IT", "th": [("반도체", "core"), ("AI", "adjacent")]},
    {"t": "AVGO", "m": "US", "n": "Broadcom", "ac": "stock", "sec": "반도체", "ps": "IT", "th": [("반도체", "core"), ("AI", "adjacent")]},
    {"t": "TSM", "m": "US", "n": "Taiwan Semiconductor", "ac": "stock", "sec": "파운드리", "ps": "IT", "th": [("반도체", "core")]},
    {"t": "ASML", "m": "US", "n": "ASML Holding", "ac": "stock", "sec": "반도체장비", "ps": "IT", "th": [("반도체", "core")]},
    {"t": "MU", "m": "US", "n": "Micron Technology", "ac": "stock", "sec": "메모리", "ps": "IT", "th": [("반도체", "core")]},
    {"t": "SOXX", "m": "US", "n": "iShares Semiconductor ETF", "ac": "equity_etf", "sec": "반도체", "ps": "IT", "th": [("반도체", "core")]},
    {"t": "SMH", "m": "US", "n": "VanEck Semiconductor ETF", "ac": "equity_etf", "sec": "반도체", "ps": "IT", "th": [("반도체", "core")]},
    {"t": "SOXS", "m": "US", "n": "Direxion Daily Semiconductor Bear 3X", "ac": "inverse_etf", "sec": "반도체", "ps": "IT", "th": [("반도체", "hedge")]},

    # ── 2차전지 ──
    {"t": "373220", "m": "KRX", "n": "LG에너지솔루션", "ac": "stock", "sec": "배터리", "ps": "소재", "th": [("2차전지", "core")]},
    {"t": "006400", "m": "KRX", "n": "삼성SDI", "ac": "stock", "sec": "배터리", "ps": "소재", "th": [("2차전지", "core")]},
    {"t": "247540", "m": "KRX", "n": "에코프로비엠", "ac": "stock", "sec": "배터리소재", "ps": "소재", "th": [("2차전지", "core")]},
    {"t": "086520", "m": "KRX", "n": "에코프로", "ac": "stock", "sec": "배터리소재", "ps": "소재", "th": [("2차전지", "core")]},
    {"t": "066970", "m": "KRX", "n": "엘앤에프", "ac": "stock", "sec": "배터리소재", "ps": "소재", "th": [("2차전지", "core")]},
    {"t": "003670", "m": "KRX", "n": "포스코퓨처엠", "ac": "stock", "sec": "배터리소재", "ps": "소재", "th": [("2차전지", "core")]},
    {"t": "LIT", "m": "US", "n": "Global X Lithium & Battery Tech ETF", "ac": "equity_etf", "sec": "배터리", "ps": "소재", "th": [("2차전지", "core")]},

    # ── 바이오/제약 ──
    {"t": "207940", "m": "KRX", "n": "삼성바이오로직스", "ac": "stock", "sec": "제약바이오", "ps": "헬스케어", "th": [("바이오", "core")]},
    {"t": "068270", "m": "KRX", "n": "셀트리온", "ac": "stock", "sec": "제약바이오", "ps": "헬스케어", "th": [("바이오", "core")]},
    {"t": "196170", "m": "KRX", "n": "알테오젠", "ac": "stock", "sec": "제약바이오", "ps": "헬스케어", "th": [("바이오", "core")]},
    {"t": "326030", "m": "KRX", "n": "SK바이오팜", "ac": "stock", "sec": "제약바이오", "ps": "헬스케어", "th": [("바이오", "core")]},
    {"t": "LLY", "m": "US", "n": "Eli Lilly", "ac": "stock", "sec": "제약", "ps": "헬스케어", "th": [("바이오", "core")]},
    {"t": "NVO", "m": "US", "n": "Novo Nordisk", "ac": "stock", "sec": "제약", "ps": "헬스케어", "th": [("바이오", "core")]},
    {"t": "XBI", "m": "US", "n": "SPDR S&P Biotech ETF", "ac": "equity_etf", "sec": "제약바이오", "ps": "헬스케어", "th": [("바이오", "core")]},
    {"t": "IBB", "m": "US", "n": "iShares Biotechnology ETF", "ac": "equity_etf", "sec": "제약바이오", "ps": "헬스케어", "th": [("바이오", "core")]},

    # ── 로봇 ──
    {"t": "454910", "m": "KRX", "n": "두산로보틱스", "ac": "stock", "sec": "로봇", "ps": "산업재", "th": [("로봇", "core")]},
    {"t": "056080", "m": "KRX", "n": "유진로봇", "ac": "stock", "sec": "로봇", "ps": "산업재", "th": [("로봇", "core")]},
    {"t": "108490", "m": "KRX", "n": "로보티즈", "ac": "stock", "sec": "로봇", "ps": "산업재", "th": [("로봇", "core")]},
    {"t": "ISRG", "m": "US", "n": "Intuitive Surgical", "ac": "stock", "sec": "의료로봇", "ps": "헬스케어", "th": [("로봇", "core")]},
    {"t": "BOTZ", "m": "US", "n": "Global X Robotics & AI ETF", "ac": "equity_etf", "sec": "로봇", "ps": "산업재", "th": [("로봇", "core"), ("AI", "adjacent")]},
    {"t": "ROBO", "m": "US", "n": "ROBO Global Robotics & Automation ETF", "ac": "equity_etf", "sec": "로봇", "ps": "산업재", "th": [("로봇", "core")]},
    {"t": "ARKQ", "m": "US", "n": "ARK Autonomous Tech & Robotics ETF", "ac": "equity_etf", "sec": "로봇", "ps": "산업재", "th": [("로봇", "core"), ("AI", "adjacent")]},

    # ── AI ──
    {"t": "035420", "m": "KRX", "n": "NAVER", "ac": "stock", "sec": "인터넷", "ps": "IT", "th": [("AI", "core")]},
    {"t": "035720", "m": "KRX", "n": "카카오", "ac": "stock", "sec": "인터넷", "ps": "IT", "th": [("AI", "core")]},
    {"t": "MSFT", "m": "US", "n": "Microsoft", "ac": "stock", "sec": "소프트웨어", "ps": "IT", "th": [("AI", "core")]},
    {"t": "GOOGL", "m": "US", "n": "Alphabet", "ac": "stock", "sec": "인터넷", "ps": "IT", "th": [("AI", "core")]},
    {"t": "PLTR", "m": "US", "n": "Palantir Technologies", "ac": "stock", "sec": "소프트웨어", "ps": "IT", "th": [("AI", "core")]},

    # ── 방산 ──
    {"t": "012450", "m": "KRX", "n": "한화에어로스페이스", "ac": "stock", "sec": "방위산업", "ps": "산업재", "th": [("방산", "core")]},
    {"t": "047810", "m": "KRX", "n": "한국항공우주", "ac": "stock", "sec": "방위산업", "ps": "산업재", "th": [("방산", "core")]},
    {"t": "064350", "m": "KRX", "n": "현대로템", "ac": "stock", "sec": "방위산업", "ps": "산업재", "th": [("방산", "core")]},
    {"t": "079550", "m": "KRX", "n": "LIG넥스원", "ac": "stock", "sec": "방위산업", "ps": "산업재", "th": [("방산", "core")]},
    {"t": "LMT", "m": "US", "n": "Lockheed Martin", "ac": "stock", "sec": "방위산업", "ps": "산업재", "th": [("방산", "core")]},
    {"t": "ITA", "m": "US", "n": "iShares US Aerospace & Defense ETF", "ac": "equity_etf", "sec": "방위산업", "ps": "산업재", "th": [("방산", "core")]},

    # ── 조선 ──
    {"t": "009540", "m": "KRX", "n": "HD한국조선해양", "ac": "stock", "sec": "조선", "ps": "산업재", "th": [("조선", "core")]},
    {"t": "010140", "m": "KRX", "n": "삼성중공업", "ac": "stock", "sec": "조선", "ps": "산업재", "th": [("조선", "core")]},
    {"t": "042660", "m": "KRX", "n": "한화오션", "ac": "stock", "sec": "조선", "ps": "산업재", "th": [("조선", "core")]},
    {"t": "010620", "m": "KRX", "n": "HD현대미포", "ac": "stock", "sec": "조선", "ps": "산업재", "th": [("조선", "core")]},

    # ── 금융 ──
    {"t": "105560", "m": "KRX", "n": "KB금융", "ac": "stock", "sec": "은행", "ps": "금융", "th": [("금융", "core"), ("배당", "adjacent")]},
    {"t": "055550", "m": "KRX", "n": "신한지주", "ac": "stock", "sec": "은행", "ps": "금융", "th": [("금융", "core"), ("배당", "adjacent")]},
    {"t": "086790", "m": "KRX", "n": "하나금융지주", "ac": "stock", "sec": "은행", "ps": "금융", "th": [("금융", "core"), ("배당", "adjacent")]},
    {"t": "316140", "m": "KRX", "n": "우리금융지주", "ac": "stock", "sec": "은행", "ps": "금융", "th": [("금융", "core"), ("배당", "adjacent")]},
    {"t": "JPM", "m": "US", "n": "JPMorgan Chase", "ac": "stock", "sec": "은행", "ps": "금융", "th": [("금융", "core")]},
    {"t": "XLF", "m": "US", "n": "Financial Select Sector SPDR", "ac": "equity_etf", "sec": "금융", "ps": "금융", "th": [("금융", "core")]},

    # ── 배당 ETF ──
    {"t": "SCHD", "m": "US", "n": "Schwab US Dividend Equity ETF", "ac": "dividend_etf", "sec": "배당", "ps": "배당", "th": [("배당", "core")]},
    {"t": "VYM", "m": "US", "n": "Vanguard High Dividend Yield ETF", "ac": "dividend_etf", "sec": "배당", "ps": "배당", "th": [("배당", "core")]},
    {"t": "JEPI", "m": "US", "n": "JPMorgan Equity Premium Income ETF", "ac": "dividend_etf", "sec": "배당", "ps": "배당", "th": [("배당", "core")]},
]


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
    try:
        for rec in _SEED:
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
            "skipped_detail": skipped, "total_seed": len(_SEED)}


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
