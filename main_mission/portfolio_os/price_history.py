"""가격이력(price_history) 저장/조회 + 일봉 fetcher (KIS 실구현).

역할:
  1. price_history 테이블 멱등 upsert / 조회 (decline_signals 입력 형태로 반환).
  2. 일봉 fetcher — KIS `inquire-daily-itemchartprice`(tr_id FHKST03010100, mode 무관)
     **실구현**. 계좌 KIS 키로 토큰→일봉 조회(read-only, 주문 0)→멱등 upsert.
     키 없거나 조회 실패 시 **명확 에러**(stub 처럼 가짜 성공 금지).
  3. 기존 누적 quotes 에서 seed 하는 경로 (근사 — 단일시점가 → OHLC=close).

비밀/자격증명 없음(키는 .env, broker client 가 마스킹). 지능 없음(순수 데이터 이동).
read-only: place_order 등 주문 경로 미사용(조회 API 만 호출).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

from .store import db as store_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# 저장 / 조회
# ============================================================
def upsert_bars(instrument_code: str, bars: list[dict], source: str) -> dict:
    """일봉 리스트를 멱등 upsert. bars: [{trade_date, open, high, low, close, volume}].

    PK (instrument_code, trade_date) — 재실행해도 중복 없이 갱신.
    close 없는 행은 skip(최소 요건). 자동매매 아님 — 데이터 저장만.
    """
    conn = store_db.connect()
    written = 0
    try:
        now = _now()
        for b in bars:
            close = b.get("close")
            td = b.get("trade_date") or b.get("date")
            if close is None or not td:
                continue
            conn.execute(
                "INSERT INTO price_history(instrument_code, trade_date, open, high, low, close, volume, source, captured_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(instrument_code, trade_date) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
                "volume=excluded.volume, source=excluded.source, captured_at=excluded.captured_at",
                (instrument_code, str(td), b.get("open"), b.get("high"), b.get("low"),
                 float(close), b.get("volume"), source, now),
            )
            written += 1
        conn.commit()
        return {"ok": True, "instrument_code": instrument_code, "written": written, "source": source}
    finally:
        conn.close()


def load_history(instrument_code: str, limit: int = 400) -> list[dict]:
    """decline_signals.compute_signals 입력 형태로 반환 (오래된→최신 순).

    [{date, open, high, low, close, volume}]
    """
    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT trade_date, open, high, low, close, volume FROM price_history "
            "WHERE instrument_code=? ORDER BY trade_date DESC LIMIT ?",
            (instrument_code, limit),
        ).fetchall()
    finally:
        conn.close()
    out = [{"date": r["trade_date"], "open": r["open"], "high": r["high"],
            "low": r["low"], "close": r["close"], "volume": r["volume"]} for r in rows]
    out.reverse()  # 오래된 → 최신
    return out


def available_codes() -> list[str]:
    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT instrument_code, COUNT(*) n, MAX(trade_date) last FROM price_history "
            "GROUP BY instrument_code ORDER BY instrument_code").fetchall()
        return [{"instrument_code": r["instrument_code"], "bars": r["n"], "last": r["last"]} for r in rows]
    finally:
        conn.close()


# ============================================================
# 일봉 fetcher — 추상 인터페이스 + KIS 실구현
# ============================================================
class DailyBarFetcher:
    """브로커 일봉 fetcher 추상 인터페이스.

    구현체는 KIS/키움 일봉 endpoint 를 호출해 [{trade_date, open, high, low, close, volume}] 반환.
    """

    def fetch_daily(self, instrument_code: str, *, count: int = 200) -> list[dict]:  # pragma: no cover
        raise NotImplementedError


class KisDailyBarFetcher(DailyBarFetcher):
    """KIS 국내 일봉 fetcher — **실구현** (read-only, 주문 0).

    endpoint /uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice
      tr_id FHKST03010100 (mode 무관). 계좌 KIS 키로 토큰→일봉 조회.
    KIS 1회 최대 100건 → 날짜 윈도우로 과거로 페이징(rate limit 은 KisHttpClient 토큰버킷).

    adapter 는 get_daily_bars(read-only) 만 호출 — place_order/주문 경로 미사용.
    키 없으면 KisHttpClient.require_credentials 가 KisConfigError(명확 실패 — 가짜 성공 금지).
    """

    def __init__(self, account_index: int | None = None, *, adapter=None,
                 page_pause: float = 0.0) -> None:
        self.account_index = account_index
        self._adapter = adapter            # 주입(테스트/재사용) — 없으면 lazy 생성
        self._page_pause = page_pause

    def _get_adapter(self):
        if self._adapter is not None:
            return self._adapter
        # live 키여도 일봉은 read-only 라 조회 허용. 단 live adapter 생성에는
        # KIS_LIVE_CONFIRM 가드가 걸리므로(factory), 조회용으로는 mode 를 강제하지 않고
        # adapter 베이스 클래스를 직접 쓴다(get_daily_bars 는 _KisAdapterBase 공통).
        from .broker.kis_client import KisHttpClient
        from .broker.kis_adapter import KisPaperAdapter, KisLiveAdapter
        import os
        pre = f"KIS_ACCOUNT_{self.account_index}_" if self.account_index else ""
        acct_mode = (os.getenv(pre + "MODE") or os.getenv("KIS_MODE", "paper")).strip().lower()
        mode = "live" if acct_mode == "live" else "paper"
        client = KisHttpClient(mode=mode, account_index=self.account_index)
        client.require_credentials()  # 키 없으면 여기서 명확 실패(가짜 성공 금지)
        self._adapter = KisLiveAdapter(client) if mode == "live" else KisPaperAdapter(client)
        return self._adapter

    def fetch_daily(self, instrument_code: str, *, count: int = 200) -> list[dict]:
        """최근 count 거래일 근사치를 페이징으로 조회 → [{trade_date, OHLCV}] (오래된→최신).

        count 거래일을 달력일로 변환할 수 없으므로(휴장), 윈도우(달력 ~150일=약 100거래일)
        를 과거로 이동하며 충분히 모이거나 더 안 나올 때까지 페이징한다.
        """
        adapter = self._get_adapter()
        bars_by_date: dict[str, dict] = {}
        end = datetime.now(timezone.utc).date()
        # 100거래일 ≈ 달력 ~150일. 안전하게 윈도우당 150 달력일.
        window_days = 150
        # 필요한 거래일 만큼 페이지 + 빈 윈도우(휴장/상장 전) 허용 여유.
        max_pages = max(2, (count // 80) + 3)
        empty_streak = 0
        for _ in range(max_pages):
            start = end - timedelta(days=window_days)
            page = adapter.get_daily_bars(
                instrument_code, start=start.strftime("%Y%m%d"),
                end=end.strftime("%Y%m%d"),
            )
            if not page:
                # 빈 윈도우(휴장 구간/데이터 공백) — 한 번까지는 더 과거로 진행,
                # 연속 2회 비면 상장 이전으로 보고 중단(무한루프 방지).
                empty_streak += 1
                if empty_streak >= 2:
                    break
                end = start - timedelta(days=1)
                continue
            empty_streak = 0
            for b in page:
                bars_by_date[b["trade_date"]] = b
            if len(bars_by_date) >= count:
                break
            # 가장 오래된 행 하루 전으로 윈도우 이동(과거로)
            oldest = min(b["trade_date"] for b in page)
            new_end = datetime.strptime(oldest, "%Y-%m-%d").date() - timedelta(days=1)
            if new_end >= end:  # 진전 없음 → 중단(무한루프 방지)
                break
            end = new_end
            if self._page_pause:
                time.sleep(self._page_pause)
        bars = sorted(bars_by_date.values(), key=lambda b: b["trade_date"])
        return bars[-count:] if count and len(bars) > count else bars

    def fetch_and_store(self, instrument_code: str, *, count: int = 200) -> dict:
        """일봉 조회 → price_history 멱등 upsert. read-only(주문 0)."""
        bars = self.fetch_daily(instrument_code, count=count)
        if not bars:
            return {"ok": False, "instrument_code": instrument_code,
                    "reason": "no_bars_returned",
                    "note": "KIS 가 빈 응답 — 종목코드/거래일 확인. 가짜 데이터 미생성."}
        res = upsert_bars(instrument_code, bars, source="kis_daily")
        res["fetched"] = len(bars)
        res["range"] = {"from": bars[0]["trade_date"], "to": bars[-1]["trade_date"]}
        return res


class KiwoomDailyBarFetcher(DailyBarFetcher):
    """키움 일봉 fetcher — **stub** (opt10081 일봉차트조회 류). 사용자 endpoint 확인 필요."""

    def fetch_daily(self, instrument_code: str, *, count: int = 200) -> list[dict]:  # pragma: no cover
        raise NotImplementedError(
            "키움 일봉 fetcher 미구현(stub). 사용자 endpoint(opt10081 류) 확인 후 구현 필요."
        )


# ============================================================
# quotes seed (근사 — 정직: 실 일봉 아님)
# ============================================================
def seed_from_quotes(ticker: str, instrument_code: str | None = None) -> dict:
    """기존 누적 quotes(단일시점가)에서 price_history 를 근사 seed.

    정직: quotes 는 OHLC 가 없는 단일 시점가라 open=high=low=close 로 근사하고,
    하루 여러 quote 면 마지막 값을 종가로 본다. **실 일봉이 아니므로** 신호 정확도는
    일봉 fetch 보다 낮다(특히 ATR/거래량 다이버전스). source='quotes_seed'.
    """
    code = instrument_code or ticker
    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT date(captured_at) d, price FROM quotes WHERE ticker=? AND price IS NOT NULL "
            "ORDER BY captured_at ASC", (ticker,)).fetchall()
    finally:
        conn.close()
    by_day: dict[str, float] = {}
    for r in rows:
        by_day[r["d"]] = float(r["price"])  # 같은 날 마지막 = 종가 근사
    bars = [{"trade_date": d, "open": p, "high": p, "low": p, "close": p, "volume": None}
            for d, p in sorted(by_day.items())]
    res = upsert_bars(code, bars, source="quotes_seed")
    res["note"] = "근사 seed (OHLC=close, 거래량 없음). 실 백테스트는 일봉 fetch 권장."
    return res


def account_target_codes(account_index: int) -> list[str]:
    """계좌의 관심종목(universe_instruments) + 보유종목(holdings 최신 snapshot) 코드 집합.

    read-only(DB 조회만). decline_scan.scan_account_universe 와 동일 소스.
    """
    conn = store_db.connect()
    try:
        codes: dict[str, None] = {}
        for r in conn.execute(
            "SELECT ticker FROM universe_instruments WHERE account_index=? AND is_active=1",
            (account_index,)).fetchall():
            codes[r["ticker"]] = None
        snap = conn.execute(
            "SELECT id FROM account_snapshots WHERE account_index=? ORDER BY id DESC LIMIT 1",
            (account_index,)).fetchone()
        if snap:
            for r in conn.execute(
                "SELECT ticker FROM holdings WHERE snapshot_id=?", (snap["id"],)).fetchall():
                codes[r["ticker"]] = None
        return list(codes.keys())
    finally:
        conn.close()


def fetch_account_daily(account_index: int, *, count: int = 200,
                        codes: list[str] | None = None) -> dict:
    """계좌1 류: 보유/관심 종목 일봉을 KIS 키로 read-only 적재(주문 0).

    codes 미지정 시 DB 의 관심+보유 종목을 사용. 각 종목 멱등 upsert.
    KIS 키 없거나 조회 실패는 per-code error 로 정직히 기록(가짜 성공 금지).
    """
    fetcher = KisDailyBarFetcher(account_index=account_index, page_pause=0.0)
    target = codes if codes is not None else account_target_codes(account_index)
    if not target:
        return {"ok": False, "account_index": account_index,
                "reason": "no_target_codes",
                "note": "관심/보유 종목 없음 — --code 로 직접 지정하거나 universe/snapshot 먼저 적재."}
    results = []
    for code in target:
        try:
            results.append(fetcher.fetch_and_store(code, count=count))
        except Exception as e:  # noqa: BLE001
            results.append({"ok": False, "instrument_code": code, "error": str(e)})
    ok_n = sum(1 for r in results if r.get("ok"))
    return {"ok": ok_n > 0, "account_index": account_index, "read_only": True,
            "fetched_codes": ok_n, "total_codes": len(target), "results": results}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-from-quotes", metavar="TICKER")
    ap.add_argument("--code", help="instrument_code (단일 종목 일봉 fetch 또는 seed 대상)")
    ap.add_argument("--account", type=int, help="KIS 계좌 index (일봉 fetch 시 키 소스)")
    ap.add_argument("--fetch-daily", action="store_true",
                    help="KIS 일봉 적재(read-only). --account 필요. --code 지정 시 단일, 미지정 시 계좌 관심/보유 전체")
    ap.add_argument("--count", type=int, default=200, help="가져올 최근 거래일 수")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.fetch_daily:
        if args.account is None:
            out = {"ok": False, "error": "--fetch-daily 에는 --account N 필요 (KIS 키 소스)"}
        elif args.code:
            try:
                fetcher = KisDailyBarFetcher(account_index=args.account)
                out = fetcher.fetch_and_store(args.code, count=args.count)
            except Exception as e:  # noqa: BLE001
                out = {"ok": False, "code": args.code, "error": str(e)}
        else:
            out = fetch_account_daily(args.account, count=args.count)
    elif args.seed_from_quotes:
        out = seed_from_quotes(args.seed_from_quotes, args.code)
    elif args.list:
        out = {"ok": True, "codes": available_codes()}
    else:
        out = {"ok": False, "error": "--fetch-daily --account N [--code C] | --seed-from-quotes TICKER | --list"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
