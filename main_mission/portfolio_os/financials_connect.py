"""재무/DART 커넥터 (Track C) — 금융감독원 OpenDART(공식·무료) → `fundamentals` 적재.

CEO 본질: 개별주 **저평가 우량주 필터**(security_selection.quality_filter)는 *구조화된*
재무/밸류에이션 수치가 있어야 동작한다. 그 수치를 **공식·무료** 소스(DART)에서 적재한다.
fundamentals 가 채워지면 security_selection._structured_financials 가 자동으로 그 행을 읽어
quality_filter 가 활성화된다(본 모듈은 적재만 — 선정 본문은 건드리지 않음).

불변 원칙(CLAUDE.md §2, §11.8):
  - **공식/무료 우선.** OpenDART Open API(무료, 가입 후 인증키). 임의 추측 endpoint 금지.
  - **가짜 점수/데이터 금지.** 키 없으면 명확 실패(FinancialsConfigError) — 합성 재무 0건.
    응답이 'no data'(status=013)면 정직하게 0건. 부실/적자를 우량주로 표기하지 않는다.
  - **secret(.env) 0.** 인증키(DART_API_KEY)는 .env 에서만 로드, 코드/DB/로그 평문 금지.
  - **자동주문/policy 변경 0.** 여기서는 재무 수치 적재까지만. 비중·주문은 사람 승인.
  - 지능 = 규칙 + Claude+메모리. **Anthropic API 미사용.**
  - **출처·기준일(as_of)·freshness 저장.** macro_connect 의 obs_date decay 사상과 동일.

확인한 공식 endpoint (WebSearch/WebFetch 로 검증 — 임의추측 아님):
  - 정기보고서 재무정보(전체 재무제표):
      https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json
        ?crtfc_key=..&corp_code=(8자리)&bsns_year=YYYY&reprt_code=(11011|11012|11013|11014)
        &fs_div=(CFS 연결|OFS 별도)
      응답: status('000' 성공 / '013' 무자료), message, list[{sj_div, account_nm,
            thstrm_amount, ...}]  (sj_div: BS 재무상태표·IS 손익·CIS 포괄손익·CF 현금흐름)
      reprt_code: 11013 1분기·11012 반기·11014 3분기·11011 사업보고서(연간)
      (docs: opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019020)
  - 기업 고유번호(corp_code) 매핑: OpenDART 는 6자리 종목코드(ticker)가 아니라 8자리
      corp_code 를 쓴다. 매핑 파일(CORPCODE.xml, zip):
      https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key=..
      (docs: opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019018)
      → ticker(stock_code) → corp_code 매핑은 .env 또는 corp_code_map.json 에서 제공받는다.
        매핑 미제공이면 정직하게 not_connected(가짜 corp_code 추측 금지).

  python -m main_mission.portfolio_os.financials_connect --status
  python -m main_mission.portfolio_os.financials_connect --load 005930 --year 2024 --reprt 11011
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from .store import db as store_db

ROOT = Path(__file__).resolve().parents[2]

DART_BASE = "https://opendart.fss.or.kr/api"
FINSTATE_ALL = f"{DART_BASE}/fnlttSinglAcntAll.json"
CORPCODE_URL = f"{DART_BASE}/corpCode.xml"

# reprt_code → period suffix (period 컬럼 표기: 'YYYY-Qn' 또는 'YYYY').
_REPRT_PERIOD = {"11013": "Q1", "11012": "Q2", "11014": "Q3", "11011": "Q4"}
# 연간(사업보고서)은 'YYYY' 로, 분기/반기는 'YYYY-Qn' 로 저장.
_REPRT_ANNUAL = "11011"

# freshness: macro_connect 와 동일 사상(반감기 decay). 재무는 분기 발표라 반감기 길게.
HALF_LIFE_DAYS = 120.0


class FinancialsConfigError(RuntimeError):
    """재무(DART) 연결 실패(키 없음/매핑 없음/endpoint 응답 오류) — 가짜 성공 금지 신호."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env)
    except ImportError:
        from .envfallback import load_env_file
        load_env_file(env)


def dart_api_key() -> str:
    _load_env()
    key = (os.getenv("DART_API_KEY") or "").strip()
    if not key:
        raise FinancialsConfigError(
            "DART_API_KEY 가 .env 에 없습니다 — 재무(OpenDART) 미연동. "
            "https://opendart.fss.or.kr 에서 무료 인증키 발급 후 .env 에 설정하세요. "
            "(가짜 재무 데이터 생성 안 함)")
    return key


# ============================================================
# corp_code 매핑 (ticker 6자리 → DART corp_code 8자리)
# ============================================================
def _corp_map_path() -> Path:
    """corp_code 매핑 파일 경로. .env(DART_CORP_MAP) 우선, 없으면 data/corp_code_map.json."""
    _load_env()
    raw = (os.getenv("DART_CORP_MAP") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (ROOT / raw)
    return ROOT / "data" / "corp_code_map.json"


def load_corp_map() -> dict:
    """ticker(6자리) → corp_code(8자리) 매핑. 파일 없으면 빈 dict(정직 — 추측 금지)."""
    p = _corp_map_path()
    if not p.exists():
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    if not isinstance(obj, dict):
        return {}
    # 키/값을 문자열로 정규화 (6자리 zero-pad ticker 허용).
    return {str(k).strip().zfill(6) if str(k).strip().isdigit() else str(k).strip():
            str(v).strip().zfill(8) for k, v in obj.items() if v}


def resolve_corp_code(ticker: str, *, corp_map: dict | None = None) -> str:
    """ticker → corp_code. 8자리 숫자면 corp_code 로 간주(직접 전달 허용). 없으면 에러(추측 금지)."""
    tk = (ticker or "").strip()
    if not tk:
        raise FinancialsConfigError("빈 ticker — corp_code 결정 불가.")
    if tk.isdigit() and len(tk) == 8:
        return tk  # 이미 corp_code 를 직접 준 경우.
    m = corp_map if corp_map is not None else load_corp_map()
    key = tk.zfill(6) if tk.isdigit() else tk
    cc = m.get(key) or m.get(tk)
    if not cc:
        raise FinancialsConfigError(
            f"ticker {tk!r} 의 corp_code 매핑 없음 — OpenDART 는 8자리 corp_code 를 씁니다. "
            f"corp_code_map.json({_corp_map_path()}) 에 매핑을 추가하거나 "
            "`--build-corp-map`(키 필요)으로 생성하세요. (corp_code 추측 금지)")
    return cc


def build_corp_map(*, api_key: str | None = None, conn=None) -> dict:
    """CORPCODE.xml(zip) 다운로드 → {stock_code: corp_code} 매핑 생성 + 파일 저장.

    공식 endpoint(검증): opendart.fss.or.kr/api/corpCode.xml?crtfc_key=..
    상장사만(stock_code 가 빈 값 아닌 행) 매핑. 키 없으면 FinancialsConfigError.
    """
    key = api_key or dart_api_key()
    url = f"{CORPCODE_URL}?{urllib.parse.urlencode({'crtfc_key': key})}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "portfolio-os/financials"})
        with urllib.request.urlopen(req, timeout=30.0) as resp:  # noqa: S310 (https only)
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise FinancialsConfigError(f"DART corpCode HTTP {e.code} — 키/네트워크 확인") from e
    except urllib.error.URLError as e:
        raise FinancialsConfigError(f"DART corpCode 연결 실패: {e.reason}") from e
    # zip 이 아니라 JSON 오류면(키 불량 등) 정직 실패.
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        xml = zf.read(zf.namelist()[0]).decode("utf-8")
    except (zipfile.BadZipFile, IndexError) as e:
        snippet = raw[:200].decode("utf-8", "replace")
        raise FinancialsConfigError(
            f"DART corpCode 응답이 zip 이 아님 — 키 확인 필요. 응답: {snippet}") from e
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    mapping: dict[str, str] = {}
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        cc = (el.findtext("corp_code") or "").strip()
        if sc and cc:  # 상장사(stock_code 있음)만.
            mapping[sc.zfill(6)] = cc.zfill(8)
    p = _corp_map_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mapping, ensure_ascii=False, indent=0), encoding="utf-8")
    return {"ok": True, "mapped": len(mapping), "path": str(p)}


# ============================================================
# DART 재무 fetch + 파싱 (공식 endpoint)
# ============================================================
def _http_get_json(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "portfolio-os/financials"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https only)
        return json.loads(resp.read().decode("utf-8"))


def _to_float(s) -> float | None:
    """DART 금액 문자열('1,234,567' / '-' / '') → float. 결측은 None(가짜 0 금지)."""
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if t in ("", "-", "."):
        return None
    try:
        return float(t)
    except (ValueError, TypeError):
        return None


def fetch_finstate(corp_code: str, bsns_year: str, reprt_code: str = _REPRT_ANNUAL,
                   *, fs_div: str = "CFS", api_key: str | None = None) -> list[dict]:
    """OpenDART 전체 재무제표 1건 → list[{sj_div, account_nm, thstrm_amount, ...}] (원시 행).

    endpoint(검증): fnlttSinglAcntAll.json?crtfc_key&corp_code&bsns_year&reprt_code&fs_div
    status '000' 성공 / '013' 무자료(빈 리스트, 정직) / 그 외 코드는 FinancialsConfigError.
    """
    key = api_key or dart_api_key()
    q = urllib.parse.urlencode({
        "crtfc_key": key, "corp_code": corp_code, "bsns_year": str(bsns_year),
        "reprt_code": reprt_code, "fs_div": fs_div})
    try:
        data = _http_get_json(f"{FINSTATE_ALL}?{q}")
    except urllib.error.HTTPError as e:
        raise FinancialsConfigError(
            f"DART HTTP {e.code} (corp={corp_code}, {bsns_year}/{reprt_code}) — 키/네트워크 확인") from e
    except urllib.error.URLError as e:
        raise FinancialsConfigError(f"DART 연결 실패 (corp={corp_code}): {e.reason}") from e
    status = str(data.get("status") or "")
    if status == "013":     # 무자료 — 정직하게 0건(가짜 생성 금지).
        return []
    if status != "000":
        raise FinancialsConfigError(
            f"DART 응답 오류 status={status} msg={data.get('message')} "
            f"(corp={corp_code}, {bsns_year}/{reprt_code}) — 가짜 데이터 생성 안 함")
    return list(data.get("list") or [])


# 계정명(account_nm) 키워드 → fundamentals 컬럼. (sj_div 로 표/항목 구분)
#   가짜 값을 만들지 않도록, 키워드가 매칭된 항목만 추출(없으면 None 유지).
_BS_DEBT = ("부채총계",)            # 총부채 (재무상태표 BS)
_BS_EQUITY = ("자본총계",)          # 총자본 (BS) — debt_ratio = 부채/자본*100
_BS_INVENTORY = ("재고자산",)        # 재고 (BS)
_IS_REVENUE = ("매출액", "수익(매출액)", "영업수익")   # 매출 (손익 IS/CIS)
_IS_OP = ("영업이익",)               # 영업이익 (IS/CIS)
_IS_NET = ("당기순이익",)            # 순이익 (IS/CIS) — '지배기업' 포함 변형 주의
_CF_OP = ("영업활동현금흐름", "영업활동으로인한현금흐름")  # 영업현금흐름 (CF)


def _match_amount(rows: list[dict], sj_divs: tuple, names: tuple) -> float | None:
    """sj_div ∈ sj_divs 이고 account_nm 이 names 중 하나로 (정규화) 일치하는 thstrm_amount.

    공백 제거 후 정확/접두 매칭. 여러 개면 첫 매칭(가짜 합성 금지)."""
    def norm(s: str) -> str:
        return (s or "").replace(" ", "")
    targets = [norm(n) for n in names]
    for r in rows:
        if r.get("sj_div") not in sj_divs:
            continue
        nm = norm(r.get("account_nm"))
        if nm in targets:
            v = _to_float(r.get("thstrm_amount"))
            if v is not None:
                return v
    # 정확 매칭 실패 시 접두(예: '당기순이익(손실)') 한 번 더.
    for r in rows:
        if r.get("sj_div") not in sj_divs:
            continue
        nm = norm(r.get("account_nm"))
        if any(nm.startswith(t) for t in targets):
            v = _to_float(r.get("thstrm_amount"))
            if v is not None:
                return v
    return None


def parse_fundamentals(rows: list[dict]) -> dict:
    """DART 원시 행 → fundamentals 부분 dict (추출 가능한 항목만, 나머지는 None).

    추출: revenue, op_income, net_income, debt_ratio(부채/자본*100), op_margin(영업이익/매출*100),
          cash_flow_op, inventory. PER/PBR/ROE/EV-EBITDA/capex 는 재무제표만으로 산출 불가/
          별도 데이터 필요 → None(가짜 0 금지, 시장가 미연동 정직).
    """
    revenue = _match_amount(rows, ("IS", "CIS"), _IS_REVENUE)
    op_income = _match_amount(rows, ("IS", "CIS"), _IS_OP)
    net_income = _match_amount(rows, ("IS", "CIS"), _IS_NET)
    cash_flow_op = _match_amount(rows, ("CF",), _CF_OP)
    inventory = _match_amount(rows, ("BS",), _BS_INVENTORY)
    debt = _match_amount(rows, ("BS",), _BS_DEBT)
    equity = _match_amount(rows, ("BS",), _BS_EQUITY)

    debt_ratio = (round(debt / equity * 100.0, 2)
                  if debt is not None and equity not in (None, 0) else None)
    op_margin = (round(op_income / revenue * 100.0, 2)
                 if op_income is not None and revenue not in (None, 0) else None)
    return {
        "revenue": revenue, "op_income": op_income, "net_income": net_income,
        "op_margin": op_margin, "debt_ratio": debt_ratio, "cash_flow_op": cash_flow_op,
        "inventory": inventory,
        # 재무제표만으로 산출 불가(주가/EBITDA 필요) — 미연동 정직(가짜 0 금지).
        "roe": None, "per": None, "pbr": None, "ev_ebitda": None, "capex": None,
    }


# ============================================================
# 적재 (멱등 upsert) — UNIQUE(ticker, period)
# ============================================================
def _freshness(as_of: str | None, *, now: date | None = None) -> float:
    """as_of decay → freshness(0~1). 없으면 0.0(가짜 신선 금지)."""
    if not as_of:
        return 0.0
    now = now or date.today()
    try:
        d = date.fromisoformat(str(as_of)[:10])
    except (ValueError, TypeError):
        return 0.0
    age = max(0, (now - d).days)
    return round(0.5 ** (age / HALF_LIFE_DAYS), 4)


def upsert_fundamentals(ticker: str, period: str, metrics: dict, *, source: str = "dart",
                        as_of: str | None = None, conn=None) -> bool:
    """fundamentals 1행 멱등 upsert(UNIQUE ticker,period).

    모든 핵심 metric 이 None 이면 적재 거부(가짜 빈 행 금지) → False.
    """
    tk = (ticker or "").strip()
    if not tk or not period:
        return False
    keys = ("revenue", "op_income", "net_income", "op_margin", "debt_ratio",
            "cash_flow_op", "roe", "per", "pbr", "ev_ebitda", "inventory", "capex")
    vals = {k: metrics.get(k) for k in keys}
    if all(v is None for v in vals.values()):
        return False  # 추출된 수치 0 — 빈 행 만들지 않음(정직).
    as_of = as_of or date.today().isoformat()
    fresh = _freshness(as_of)
    own = conn is None
    conn = conn or store_db.connect()
    try:
        conn.execute(
            "INSERT INTO fundamentals(ticker, period, revenue, op_income, net_income, "
            "op_margin, debt_ratio, cash_flow_op, roe, per, pbr, ev_ebitda, inventory, "
            "capex, source, as_of, freshness, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(ticker, period) DO UPDATE SET "
            "revenue=excluded.revenue, op_income=excluded.op_income, "
            "net_income=excluded.net_income, op_margin=excluded.op_margin, "
            "debt_ratio=excluded.debt_ratio, cash_flow_op=excluded.cash_flow_op, "
            "roe=excluded.roe, per=excluded.per, pbr=excluded.pbr, "
            "ev_ebitda=excluded.ev_ebitda, inventory=excluded.inventory, "
            "capex=excluded.capex, source=excluded.source, as_of=excluded.as_of, "
            "freshness=excluded.freshness",
            (tk, period, vals["revenue"], vals["op_income"], vals["net_income"],
             vals["op_margin"], vals["debt_ratio"], vals["cash_flow_op"], vals["roe"],
             vals["per"], vals["pbr"], vals["ev_ebitda"], vals["inventory"],
             vals["capex"], source, as_of, fresh, _now()))
        conn.commit()
        return True
    finally:
        if own:
            conn.close()


def load_financials(ticker: str, bsns_year: int | str, reprt_code: str = _REPRT_ANNUAL,
                    *, fs_div: str = "CFS", corp_map: dict | None = None,
                    api_key: str | None = None, conn=None, store_ticker: str | None = None) -> dict:
    """ticker 한 종목·한 기간 재무 DART 적재 → 결과 요약. 키/매핑 없으면 FinancialsConfigError.

    store_ticker 가 주어지면 fundamentals 행을 그 키(예: 6자리 종목코드)로 저장한다.
    quality_filter 가 6자리 ticker 로 조회하므로, corp_code(8자리)로 fetch 하더라도
    적재는 항상 종목코드 기준으로 통일한다(미지정 시 ticker 그대로).
    """
    key = api_key or dart_api_key()
    corp_code = resolve_corp_code(ticker, corp_map=corp_map)
    save_tk = (store_ticker or ticker)
    rows = fetch_finstate(corp_code, bsns_year, reprt_code, fs_div=fs_div, api_key=key)
    if not rows:
        return {"ok": True, "ticker": save_tk, "data_connected": True, "written": False,
                "period": _period_label(bsns_year, reprt_code),
                "note": "DART 무자료(status=013) — 해당 기간 재무 없음(정직, 가짜 0)."}
    metrics = parse_fundamentals(rows)
    period = _period_label(bsns_year, reprt_code)
    as_of = f"{bsns_year}-12-31" if reprt_code == _REPRT_ANNUAL else None
    written = upsert_fundamentals(save_tk, period, metrics, source="dart",
                                  as_of=as_of, conn=conn)
    return {
        "ok": True, "ticker": save_tk, "corp_code": corp_code, "data_connected": True,
        "written": written, "period": period, "metrics": metrics,
        "note": ("fundamentals 적재 — security_selection.quality_filter 자동 활성." if written
                 else "추출 가능한 수치 없음 — 빈 행 만들지 않음(정직, 가짜 0)."),
    }


def _period_label(bsns_year: int | str, reprt_code: str) -> str:
    """reprt_code → period 컬럼 라벨('YYYY' 연간 / 'YYYY-Qn' 분기)."""
    y = str(bsns_year)
    if reprt_code == _REPRT_ANNUAL:
        return y
    return f"{y}-{_REPRT_PERIOD.get(reprt_code, 'Qx')}"


# reprt_code 표시 라벨(상태/리포트용).
_REPRT_LABEL = {"11013": "1분기", "11012": "반기", "11014": "3분기", "11011": "연간(사업보고서)"}


def load_financials_many(ticker: str, *, years: list[int] | None = None,
                         reprts: list[str] | None = None, fs_div: str = "CFS",
                         corp_map: dict | None = None, api_key: str | None = None,
                         conn=None) -> dict:
    """한 종목의 여러 기간(연간 + 분기) 재무를 한 번에 적재 — 전체 플로우 편의.

    years×reprts 조합 각각에 load_financials 를 호출(멱등). 한 기간 무자료(013)는 건너뛰고
    (정직 0건) 계속 진행. 키/매핑 없으면 첫 호출에서 FinancialsConfigError(가짜 0).
    corp_code 는 한 번만 해석해(중복 매핑 조회 방지) 각 호출에 8자리로 직접 전달한다.
    """
    key = api_key or dart_api_key()
    corp_code = resolve_corp_code(ticker, corp_map=corp_map)  # 매핑 1회만 해석.
    years = years or [date.today().year - 1]
    reprts = reprts or [_REPRT_ANNUAL]
    own = conn is None
    conn = conn or store_db.connect()
    results: list[dict] = []
    written = 0
    try:
        for y in years:
            for rc in reprts:
                # corp_code 로 fetch 하되, fundamentals 는 원래 ticker(종목코드)로 저장.
                r = load_financials(corp_code, y, rc, fs_div=fs_div,
                                    corp_map=corp_map, api_key=key, conn=conn,
                                    store_ticker=ticker)
                r["period"] = _period_label(y, rc)
                r["reprt"] = _REPRT_LABEL.get(rc, rc)
                if r.get("written"):
                    written += 1
                results.append(r)
    finally:
        if own:
            conn.close()
    return {
        "ok": True, "ticker": ticker, "corp_code": corp_code, "data_connected": True,
        "periods_attempted": len(results), "periods_written": written,
        "results": results,
        "note": (f"{ticker} 재무 {written}개 기간 적재 — fundamentals 기반 quality_filter 활성."
                 if written else
                 f"{ticker}: 적재된 기간 없음(무자료 또는 추출 수치 0) — 가짜 행 0(정직)."),
    }


# ============================================================
# 연동 상태(정직 표기) — 키/매핑 유무 + fundamentals 적재 현황
# ============================================================
def status(*, conn=None) -> dict:
    """재무(DART) 연동 상태 — 키 유무·corp_map 유무·fundamentals 적재 종목 수(정직)."""
    _load_env()
    has_key = bool((os.getenv("DART_API_KEY") or "").strip())
    corp_map = load_corp_map()
    own = conn is None
    conn = conn or store_db.connect()
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT ticker) t, COUNT(*) n FROM fundamentals").fetchone()
        tickers = int(row["t"] or 0)
        rows = int(row["n"] or 0)
    finally:
        if own:
            conn.close()
    connected = has_key and bool(corp_map)
    return {
        "data_connected": connected,
        "dart_api_key": "set" if has_key else "not_set",
        "corp_code_map": f"{len(corp_map)} tickers" if corp_map else "not_provided",
        "fundamentals_loaded_tickers": tickers,
        "fundamentals_rows": rows,
        "quality_filter_active": rows > 0,   # fundamentals 행이 있으면 우량주 필터 활성화 가능.
        "note": (
            "DART_API_KEY 미설정 — 재무 미연동(가짜 점수 0). opendart.fss.or.kr 에서 무료 키 발급."
            if not has_key else
            ("corp_code 매핑 미제공 — `--build-corp-map`(키 필요)으로 생성하거나 "
             "corp_code_map.json 추가 필요(corp_code 추측 금지)." if not corp_map else
             f"재무 연동 가능. fundamentals {rows}행({tickers}종목) 적재됨 — "
             "fundamentals 가 있으면 quality_filter 가 구조화 수치로 우량주 판정.")),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="재무/DART → fundamentals 적재(공식·무료, 가짜 0)")
    ap.add_argument("--status", action="store_true", help="연동 상태(키/매핑/적재 현황)")
    ap.add_argument("--build-corp-map", action="store_true",
                    help="CORPCODE.xml 다운로드 → ticker→corp_code 매핑 생성(키 필요)")
    ap.add_argument("--load", metavar="TICKER", help="한 종목 재무 적재")
    ap.add_argument("--load-many", metavar="TICKER",
                    help="한 종목 여러 기간(연간+분기) 일괄 적재(--years/--reprts)")
    ap.add_argument("--year", type=int, help="사업연도(예: 2024)")
    ap.add_argument("--years", help="--load-many 용 연도 목록(쉼표): 예 2022,2023,2024")
    ap.add_argument("--reprts", help="--load-many 용 보고서코드 목록(쉼표): 예 11011,11012,11013,11014")
    ap.add_argument("--reprt", default=_REPRT_ANNUAL,
                    help="보고서코드 11011 연간(기본)·11013 1Q·11012 반기·11014 3Q")
    ap.add_argument("--fs-div", default="CFS", help="CFS 연결(기본) | OFS 별도")
    args = ap.parse_args()
    try:
        if args.build_corp_map:
            out = build_corp_map()
        elif args.load_many:
            years = ([int(y) for y in args.years.split(",") if y.strip()]
                     if args.years else None)
            reprts = ([r.strip() for r in args.reprts.split(",") if r.strip()]
                      if args.reprts else None)
            out = load_financials_many(args.load_many, years=years, reprts=reprts,
                                       fs_div=args.fs_div)
        elif args.load:
            if not args.year:
                out = {"ok": False, "error": "--load 에는 --year 필요"}
            else:
                out = load_financials(args.load, args.year, args.reprt, fs_div=args.fs_div)
        else:
            out = status()
    except FinancialsConfigError as e:
        out = {"ok": False, "not_connected": True, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
