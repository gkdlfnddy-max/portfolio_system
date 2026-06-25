"""종목 유니버스 — 계좌별 관심종목 + 목표비중 (소전제 골격).

직접 입력한 종목코드를 **KIS 시세조회로 검증**한 뒤에만 DB 에 저장한다.
쓰기는 백엔드(본 모듈)만. 웹은 DB 조회만 한다. 하드코딩 종목목록 없음.

  python -m main_mission.portfolio_os.universe --account 1 --add 000660
  python -m main_mission.portfolio_os.universe --account 1 --set-weight 000660 12.5
  python -m main_mission.portfolio_os.universe --account 1 --remove 000660
  python -m main_mission.portfolio_os.universe --account 1 --list
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

from .store import db as store_db
from .broker.kis_client import KisHttpClient, KisConfigError, _load_env
from .broker import kis_endpoints as ep


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _account_mode(n: int) -> str:
    return (os.getenv(f"KIS_ACCOUNT_{n}_MODE") or os.getenv("KIS_MODE", "paper")).strip().lower()


def verify(account_index: int, ticker: str) -> dict:
    """KIS 국내 시세조회로 거래 가능 종목인지 검증 + 현재가/이름."""
    mode = _account_mode(account_index)
    if mode not in ("paper", "live"):
        return {"ok": False, "error": f"검증하려면 paper/live 계좌가 필요합니다 (현재 {mode})."}
    try:
        client = KisHttpClient(mode, account_index=account_index)  # type: ignore
        client.require_credentials()
    except KisConfigError as e:
        return {"ok": False, "error": str(e)}
    try:
        resp = client.get(ep.PATH_DOMESTIC_PRICE, ep.TRID_DOMESTIC_PRICE,
                          {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"시세조회 실패: {e}"}

    if resp.get("rt_cd") not in (None, "0"):
        return {"ok": False, "error": f"KIS rt_cd={resp.get('rt_cd')} {resp.get('msg1')}"}
    out = resp.get("output", {}) or {}
    try:
        price = float(Decimal(str(out.get("stck_prpr") or "0")))
    except Exception:
        price = 0.0
    if price <= 0:
        return {"ok": False, "error": "시세가 없습니다 (거래정지/폐지/잘못된 코드 가능)."}
    # inquire-price 에는 한글 종목명이 없다(hts_kor_isnm=None). 가짜 이름 금지 → ticker 사용.
    # 한글 종목명은 종목마스터 적재(확장)로. 업종(bstp_kor_isnm)은 실데이터라 sector 로 보관.
    sector = out.get("bstp_kor_isnm")
    return {"ok": True, "ticker": ticker, "name": ticker, "sector": sector,
            "price": price, "market": "KRX", "mode": mode}


def add(account_index: int, ticker: str) -> dict:
    v = verify(account_index, ticker)
    if not v["ok"]:
        return v
    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO universe_instruments(account_index, ticker, market, name, asset_class, currency, "
            "last_price, verified_at, source, is_active, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?) "
            "ON CONFLICT(account_index, ticker, market) DO UPDATE SET name=excluded.name, "
            "asset_class=excluded.asset_class, last_price=excluded.last_price, "
            "verified_at=excluded.verified_at, is_active=1, updated_at=excluded.updated_at",
            (account_index, ticker, "KRX", v["name"], v.get("sector"), "KRW", v["price"], _now(),
             f"kis_{v['mode']}", _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "added": ticker, "name": v["name"], "price": v["price"]}


def set_weight(account_index: int, ticker: str, pct: float) -> dict:
    if pct < 0 or pct > 100:
        return {"ok": False, "error": "목표비중은 0~100 사이"}
    conn = store_db.connect()
    try:
        cur = conn.execute(
            "UPDATE universe_instruments SET target_weight_pct=?, updated_at=? "
            "WHERE account_index=? AND ticker=? AND is_active=1",
            (pct, _now(), account_index, ticker),
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "해당 종목이 유니버스에 없습니다."}
    finally:
        conn.close()
    return {"ok": True, "ticker": ticker, "target_weight_pct": pct}


def remove(account_index: int, ticker: str) -> dict:
    conn = store_db.connect()
    try:
        cur = conn.execute(
            "UPDATE universe_instruments SET is_active=0, updated_at=? WHERE account_index=? AND ticker=?",
            (_now(), account_index, ticker),
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "해당 종목이 없습니다."}
    finally:
        conn.close()
    return {"ok": True, "removed": ticker}


def listing(account_index: int) -> dict:
    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT ticker, name, target_weight_pct, last_price, verified_at FROM universe_instruments "
            "WHERE account_index=? AND is_active=1 ORDER BY id", (account_index,),
        ).fetchall()
        return {"ok": True, "instruments": [dict(r) for r in rows]}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--add", metavar="TICKER")
    ap.add_argument("--set-weight", nargs=2, metavar=("TICKER", "PCT"))
    ap.add_argument("--remove", metavar="TICKER")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    _load_env()

    try:
        if args.add:
            out = add(args.account, args.add.strip())
        elif args.set_weight:
            out = set_weight(args.account, args.set_weight[0].strip(), float(args.set_weight[1]))
        elif args.remove:
            out = remove(args.account, args.remove.strip())
        elif args.list:
            out = listing(args.account)
        else:
            out = {"ok": False, "error": "동작을 지정하세요 (--add/--set-weight/--remove/--list)"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
