"""KIS 종목별 투자자 매매동향 fetcher → investor_flows 적재 (분산축 데이터 소스).

역할:
  KIS `inquire-investor`(tr_id FHKST01010900, mode 무관)를 **read-only** 로 호출해
  종목별 외국인/기관/개인 순매수(+거래량 근사)를 일자별로 받아 `investor_flows` 에 멱등 적재.

원칙(불변):
  - **read-only**: 조회 endpoint(get)만 호출. place_order/주문 경로 미사용(주문 0).
  - **공식 TR 검증**: tr_id/endpoint/필드는 kis_endpoints.py 의 검증 상수만 사용(placeholder 금지).
  - **정직**: 키 없거나 KIS 응답 이상(rt_cd!=0)이면 **명확 실패**(가짜 성공/가짜 0 금지).
  - **3주체만**: 본 TR 은 외국인/기관/개인만 제공. 연기금/프로그램 등 세부 주체는 만들지 않는다.
  - **비밀 없음**: 키는 .env(KisHttpClient 가 마스킹). 지능 없음(순수 데이터 이동).

응답 output[] 필드(검증, kis_endpoints.py 참조):
  stck_bsop_date / frgn_ntby_qty(외국인) / orgn_ntby_qty(기관) / prsn_ntby_qty(개인)
  frgn_shnu_vol + orgn_shnu_vol + prsn_shnu_vol → 시장 거래량 근사(총 매수 거래량).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import kis_endpoints as ep
from .kis_client import KisHttpClient
from ..store import db as store_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(value) -> float | None:
    """KIS 문자열 숫자 → float. 빈값/파싱불가 → None(가짜 0 금지)."""
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_investor_rows(resp: dict, instrument_code: str) -> list[dict]:
    """KIS inquire-investor 응답 → investor_flows 행 리스트(오래된→최신).

    rt_cd != 0(또는 None 아님) 이면 RuntimeError(가짜 성공 금지).
    각 행: {trade_date, foreign_net, institution_net, retail_net, volume}.
    순매수 3주체가 전부 비어있는(파싱 불가) 행은 skip(가짜 0 금지).
    """
    rt = resp.get("rt_cd")
    if rt not in (None, "0"):
        raise RuntimeError(
            f"투자자 매매동향 조회 실패 rt_cd={rt} msg={resp.get('msg1')} "
            f"(code={instrument_code})"
        )
    rows = resp.get("output") or resp.get("output1") or []
    out: list[dict] = []
    for r in rows:
        d = (r.get("stck_bsop_date") or "").strip()
        if len(d) != 8:
            continue  # 비거래일/빈 행 skip
        foreign = _num(r.get("frgn_ntby_qty"))
        inst = _num(r.get("orgn_ntby_qty"))
        retail = _num(r.get("prsn_ntby_qty"))
        if foreign is None and inst is None and retail is None:
            continue  # 순매수 데이터 전무 → 가짜 0 만들지 않음(skip)
        # 거래량 근사: 3주체 매수 거래량 합(시장 총 거래량 대용). 일부만 있으면 그만큼.
        buy_vols = [_num(r.get(k)) for k in
                    ("frgn_shnu_vol", "orgn_shnu_vol", "prsn_shnu_vol")]
        present = [v for v in buy_vols if v is not None]
        volume = sum(present) if present else None
        out.append({
            "trade_date": f"{d[0:4]}-{d[4:6]}-{d[6:8]}",
            "foreign_net": foreign,
            "institution_net": inst,
            "retail_net": retail,
            "volume": volume,
        })
    out.sort(key=lambda x: x["trade_date"])  # 오래된→최신
    return out


def upsert_flows(instrument_code: str, flows: list[dict], source: str = "kis_investor") -> dict:
    """investor_flows 멱등 upsert. PK(instrument_code, trade_date) — 재실행 중복 없음.

    스키마 편집 없음(기존 컬럼만 사용). 자동매매 아님 — 데이터 저장만.
    """
    conn = store_db.connect()
    written = 0
    try:
        now = _now()
        for f in flows:
            td = f.get("trade_date")
            if not td:
                continue
            conn.execute(
                "INSERT INTO investor_flows("
                "instrument_code, trade_date, foreign_net, institution_net, retail_net, "
                "volume, source, captured_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(instrument_code, trade_date) DO UPDATE SET "
                "foreign_net=excluded.foreign_net, institution_net=excluded.institution_net, "
                "retail_net=excluded.retail_net, volume=excluded.volume, "
                "source=excluded.source, captured_at=excluded.captured_at",
                (instrument_code, td, f.get("foreign_net"), f.get("institution_net"),
                 f.get("retail_net"), f.get("volume"), source, now),
            )
            written += 1
        conn.commit()
        return {"ok": True, "instrument_code": instrument_code,
                "written": written, "source": source}
    finally:
        conn.close()


class KisInvestorFetcher:
    """KIS 종목별 투자자 매매동향 fetcher — **실구현**(read-only, 주문 0).

    client(KisHttpClient.get)만 사용. 키 없으면 require_credentials 가 KisConfigError
    (명확 실패 — 가짜 성공 금지). place_order 등 주문 경로는 정의조차 안 함.
    """

    def __init__(self, account_index: int | None = None, *, client=None,
                 mode: str | None = None) -> None:
        self.account_index = account_index
        self._client = client
        self._mode = mode

    def _get_client(self):
        if self._client is not None:
            return self._client
        import os
        pre = f"KIS_ACCOUNT_{self.account_index}_" if self.account_index else ""
        acct_mode = (self._mode or os.getenv(pre + "MODE")
                     or os.getenv("KIS_MODE", "paper")).strip().lower()
        # 투자자 매매동향은 read-only 조회라 live 키여도 조회 자체는 안전.
        # 단 도메인/tr_id 분기는 mode 기준(live=live 도메인). 조회 전용이라 주문 가드 불필요.
        mode = "live" if acct_mode == "live" else "paper"
        client = KisHttpClient(mode=mode, account_index=self.account_index)
        client.require_credentials()  # 키 없으면 여기서 명확 실패(가짜 성공 금지)
        self._client = client
        return client

    def fetch(self, instrument_code: str) -> list[dict]:
        """종목별 투자자 매매동향 조회 → [{trade_date, foreign/institution/retail_net, volume}].

        read-only(get). 1회 호출로 KIS 가 제공하는 최근 일자들을 반환(오래된→최신).
        """
        client = self._get_client()
        resp = client.get(
            ep.PATH_DOMESTIC_INVESTOR,
            ep.TRID_DOMESTIC_INVESTOR,
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": instrument_code},
        )
        return parse_investor_rows(resp, instrument_code)

    def fetch_and_store(self, instrument_code: str) -> dict:
        """조회 → investor_flows 멱등 upsert. read-only(주문 0)."""
        flows = self.fetch(instrument_code)
        if not flows:
            return {"ok": False, "instrument_code": instrument_code,
                    "reason": "no_rows_returned",
                    "note": "KIS 가 빈/이상 응답 — 종목코드/거래일 확인. 가짜 데이터 미생성."}
        res = upsert_flows(instrument_code, flows)
        res["fetched"] = len(flows)
        res["range"] = {"from": flows[0]["trade_date"], "to": flows[-1]["trade_date"]}
        res["read_only"] = True
        return res


def fetch_account_investor(account_index: int, *, codes: list[str] | None = None) -> dict:
    """계좌 보유/관심 종목의 투자자 매매동향을 KIS 키로 read-only 적재(주문 0).

    codes 미지정 시 price_history.account_target_codes 와 동일 소스(관심+보유).
    per-code error 는 정직히 기록(가짜 성공 금지).
    """
    from .. import price_history as ph
    fetcher = KisInvestorFetcher(account_index=account_index)
    target = codes if codes is not None else ph.account_target_codes(account_index)
    if not target:
        return {"ok": False, "account_index": account_index,
                "reason": "no_target_codes",
                "note": "관심/보유 종목 없음 — --code 로 지정하거나 universe/snapshot 먼저 적재."}
    results = []
    for code in target:
        try:
            results.append(fetcher.fetch_and_store(code))
        except Exception as e:  # noqa: BLE001
            results.append({"ok": False, "instrument_code": code, "error": str(e)})
    ok_n = sum(1 for r in results if r.get("ok"))
    return {"ok": ok_n > 0, "account_index": account_index, "read_only": True,
            "fetched_codes": ok_n, "total_codes": len(target), "results": results}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="KIS 종목별 투자자 매매동향 적재(read-only, 주문 0)")
    ap.add_argument("--account", type=int, help="KIS 계좌 index (키 소스)")
    ap.add_argument("--code", help="instrument_code (단일 종목). 미지정 시 계좌 관심/보유 전체")
    args = ap.parse_args()
    if args.account is None:
        out = {"ok": False, "error": "--account N 필요 (KIS 키 소스). [--code C] 선택."}
    elif args.code:
        try:
            out = KisInvestorFetcher(account_index=args.account).fetch_and_store(args.code)
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "code": args.code, "error": str(e)}
    else:
        out = fetch_account_investor(args.account)
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
