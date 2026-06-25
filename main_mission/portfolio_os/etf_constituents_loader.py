"""ETF 구성 적재 커넥터 (Track E) — ETF 상위구성·섹터/국가 비중 → `etf_constituents`.

CEO 본질: ETF 는 *구성·비중·섹터/국가·중복보유*가 핵심이다(개별주와 다름). 이 모듈이
구성종목을 적재하면 etf_analysis(analyze_etf/overlap/중복노출)가 **자동으로** 동작한다
(본 모듈은 적재만 — etf_analysis 본문은 건드리지 않음).

저장 위치(스키마 편집 금지 — 이미 존재):
  `etf_constituents`(etf_ticker, constituent_ticker, constituent_name, weight_pct,
                     sector, country, as_of, source) · UNIQUE(etf_ticker, constituent_ticker, as_of)

불변 원칙(CLAUDE.md §2, §11.8):
  - **공식/무료 우선.** 한국 ETF 는 KRX/운용사 PDF·공식 구성내역(무료)에서, 미국 ETF 는
    운용사 공식 holdings(무료)에서 받는다. 실시간 자동 스크래핑보다 **공식 파일(csv/json)
    적재 + 수동 입력**을 우선한다. 자동 피드 미연동이면 정직하게 not_connected.
  - **가짜 구성/비중 금지.** 입력 없으면 적재 0건(추측 구성 0). 데이터 없으면
    etf_analysis 가 알아서 '미연동' 으로 정직 표기한다.
  - **자동주문/policy 변경 0.** 구성 적재까지만. secret(.env) 0 · **Anthropic API 미사용.**
  - **출처/기준일(as_of) 저장.**

입력 형식(공식/무료 파일을 이 구조로 정규화해서 넣는다 — 가짜 합성 금지):
  rows = [{"ticker": "NVDA", "name": "NVIDIA", "weight_pct": 8.1,
           "sector": "Semiconductors", "country": "US"}, ...]

  python -m main_mission.portfolio_os.etf_constituents_loader --status
  python -m main_mission.portfolio_os.etf_constituents_loader --load-file SOXX path/to/holdings.json --as-of 2026-06-01
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, timezone

from .store import db as store_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(t: str | None) -> str | None:
    return t.strip().upper() if isinstance(t, str) and t.strip() else None


def _to_float(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace("%", "").replace(",", "")
    if s in ("", "-", "."):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _normalize_row(r: dict) -> dict | None:
    """입력 1행 → 표준 구성행. constituent_ticker 또는 name 중 하나는 있어야 함(가짜 빈행 금지)."""
    tk = _norm(r.get("ticker") or r.get("constituent_ticker") or r.get("Ticker")
               or r.get("Symbol"))
    name = (r.get("name") or r.get("constituent_name") or r.get("Name")
            or r.get("Holding") or r.get("종목명"))
    name = name.strip() if isinstance(name, str) and name.strip() else None
    if not tk and not name:
        return None
    if not tk:
        tk = name.upper()  # 티커 없으면 이름을 식별자로(국내 일부 — 추측 0, 빈행 방지).
    weight = _to_float(r.get("weight_pct") or r.get("weight") or r.get("Weight")
                       or r.get("비중") or r.get("Weight (%)"))
    sector = r.get("sector") or r.get("Sector") or r.get("섹터")
    country = r.get("country") or r.get("Country") or r.get("국가")
    return {
        "constituent_ticker": tk,
        "constituent_name": name,
        "weight_pct": weight,
        "sector": (sector.strip() if isinstance(sector, str) and sector.strip() else None),
        "country": (country.strip() if isinstance(country, str) and country.strip() else None),
    }


# ============================================================
# 적재 (멱등 upsert) — UNIQUE(etf_ticker, constituent_ticker, as_of)
# ============================================================
def load_constituents(etf_ticker: str, rows: list[dict], *, as_of: str | None = None,
                      source: str = "manual", replace: bool = True, conn=None) -> dict:
    """ETF 구성종목 적재. rows 는 정규화 전 dict 리스트(여러 키 형식 허용).

    - as_of: 구성 기준일(없으면 오늘). etf_analysis 가 최신 as_of 를 자동 선택한다.
    - replace=True: 같은 (etf_ticker, as_of) 기존 행을 먼저 지운다(부분 갱신/유령행 방지).
    - 빈/가짜 행은 적재 안 함(constituent 식별자 없는 행 skip — 가짜 구성 0).
    적재되면 etf_analysis.analyze_etf/overlap/중복노출이 자동 동작.
    """
    etf = _norm(etf_ticker)
    if not etf:
        return {"ok": False, "error": "etf_ticker 필요."}
    as_of = as_of or date.today().isoformat()
    norm_rows = [n for n in (_normalize_row(r) for r in (rows or [])) if n]
    if not norm_rows:
        return {"ok": True, "etf_ticker": etf, "written": 0, "data_connected": False,
                "note": "구성 입력 0건 — 적재 안 함(가짜 구성 0, 정직)."}
    own = conn is None
    conn = conn or store_db.connect()
    written = 0
    try:
        if replace:
            conn.execute("DELETE FROM etf_constituents WHERE etf_ticker=? AND as_of=?",
                         (etf, as_of))
        seen: set[str] = set()
        for n in norm_rows:
            ck = n["constituent_ticker"]
            if ck in seen:   # 같은 as_of 내 중복 종목 — 첫 행만(UNIQUE 충돌 방지).
                continue
            seen.add(ck)
            conn.execute(
                "INSERT INTO etf_constituents(etf_ticker, constituent_ticker, "
                "constituent_name, weight_pct, sector, country, as_of, source, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(etf_ticker, constituent_ticker, as_of) DO UPDATE SET "
                "constituent_name=excluded.constituent_name, weight_pct=excluded.weight_pct, "
                "sector=excluded.sector, country=excluded.country, source=excluded.source",
                (etf, ck, n["constituent_name"], n["weight_pct"], n["sector"],
                 n["country"], as_of, source, _now()))
            written += 1
        conn.commit()
    finally:
        if own:
            conn.close()
    total_w = round(sum(n["weight_pct"] or 0.0 for n in norm_rows), 2)
    return {
        "ok": True, "etf_ticker": etf, "as_of": as_of, "written": written,
        "data_connected": written > 0, "total_weight_pct": total_w,
        "note": ("ETF 구성 적재 — etf_analysis(구성/노출/겹침/중복노출) 자동 동작." if written
                 else "적재 0건(가짜 구성 0, 정직)."),
    }


def load_from_file(etf_ticker: str, path: str, *, as_of: str | None = None,
                   source: str | None = None, conn=None) -> dict:
    """공식/무료 구성 파일(.json 배열 또는 .csv) → 적재. 임의 합성 0(파일 내용만)."""
    import os
    if not os.path.exists(path):
        return {"ok": False, "error": f"파일 없음: {path}"}
    src = source or f"file:{os.path.basename(path)}"
    try:
        if path.lower().endswith(".json"):
            obj = json.loads(open(path, encoding="utf-8").read())
            rows = obj.get("holdings", obj) if isinstance(obj, dict) else obj
            if not isinstance(rows, list):
                return {"ok": False, "error": "JSON 은 구성 dict 배열(또는 {holdings:[...]})이어야 함."}
        elif path.lower().endswith(".csv"):
            with open(path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
        else:
            return {"ok": False, "error": "지원 형식: .json(배열) 또는 .csv"}
    except (ValueError, OSError) as e:
        return {"ok": False, "error": f"파일 파싱 실패: {e}"}
    return load_constituents(etf_ticker, rows, as_of=as_of, source=src, conn=conn)


# ============================================================
# 연동 상태 (정직 표기)
# ============================================================
def status(*, conn=None) -> dict:
    """ETF 구성 적재 현황 — 적재된 ETF 수/행 수(정직). 자동 피드 미연동 표기."""
    own = conn is None
    conn = conn or store_db.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT etf_ticker) e, COUNT(*) n FROM etf_constituents").fetchone()
        etfs = int(row["e"] or 0)
        rows = int(row["n"] or 0)
        sample = [r["etf_ticker"] for r in conn.execute(
            "SELECT DISTINCT etf_ticker FROM etf_constituents ORDER BY etf_ticker LIMIT 20"
        ).fetchall()]
    finally:
        if own:
            conn.close()
    return {
        "manual_input": "available",          # 파일/수동 적재 동작.
        "auto_holdings_feed": "not_connected",  # 운용사/KRX 자동수집 미연동(공식 파일 우선).
        "etf_constituents_loaded_etfs": etfs,
        "etf_constituents_rows": rows,
        "loaded_etfs_sample": sample,
        "etf_analysis_active": rows > 0,      # 구성 적재되면 etf_analysis 자동 동작.
        "data_connected": rows > 0,
        "note": ("ETF 구성 미연동 — 공식/무료 구성 파일(.json/.csv) 적재 또는 수동 입력 필요. "
                 "가짜 구성 0(정직)." if rows == 0 else
                 f"ETF 구성 {rows}행({etfs}종목) 적재됨 — etf_analysis(구성/노출/겹침/중복노출) "
                 "자동 동작. 자동 피드는 미연동(공식 파일 우선)."),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 구성 적재(공식/무료·파일 우선, 가짜 구성 0)")
    ap.add_argument("--status", action="store_true", help="적재 현황")
    ap.add_argument("--load-file", nargs=2, metavar=("ETF_TICKER", "PATH"),
                    help="구성 파일(.json/.csv) 적재")
    ap.add_argument("--as-of", help="구성 기준일 'YYYY-MM-DD'(없으면 오늘)")
    ap.add_argument("--source", help="출처 라벨")
    args = ap.parse_args()
    try:
        if args.load_file:
            out = load_from_file(args.load_file[0], args.load_file[1],
                                 as_of=args.as_of, source=args.source)
        else:
            out = status()
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
