"""거시/시장 데이터 연결 (Track B) — ECOS(한은)·FRED(미국) → macro_indicators 적재 + 포트폴리오 매핑.

CEO 강조: **거시가 우선**. 한국/미국 기준금리·미10년물·달러원·유가·VIX/VKOSPI·지수.
단순 표시가 아니라 *판단*(현금밴드·채권/국채·위험자산·성장속도·달러노출·미국ETF·헤지)에 연결한다.

본질 원칙(불변 — CLAUDE.md §2, §11.8):
  - **가짜 데이터 금지.** API 키 없으면 명확 실패(MacroConfigError) — 합성 점수/지표 0건.
  - **출처·기준일·freshness 저장.** macro_indicators(indicator, obs_date, value, source, captured_at).
    obs_date 기준 decay 로 stale 판정(evidence decay 와 동일 사상).
  - **데이터 없으면 data_available=False.** 거시축 가짜 점수 금지.
  - **자동주문/policy 변경 0.** 여기서는 데이터 적재 + 해석(후보 신호)까지만. 비중/주문은 사람 승인.
  - 비밀(.env) 0 — 키는 .env(ECOS_API_KEY/FRED_API_KEY)에서만 로드, 코드/DB/로그 평문 금지.
  - 지능 = 규칙 + Claude+메모리 (Anthropic API 미사용).

확인한 공식 endpoint (WebSearch 로 검증 — 임의추측 아님):
  - FRED:  https://api.stlouisfed.org/fred/series/observations
           ?series_id=..&api_key=..&file_type=json&sort_order=desc&limit=..
           (docs: fred.stlouisfed.org/docs/api/fred/series_observations.html)
  - ECOS:  https://ecos.bok.or.kr/api/StatisticSearch/{KEY}/json/kr/{start}/{end}/
           {stat_code}/{cycle}/{start_date}/{end_date}[/{item1}]
           (cycle: A 연·Q 분기·M 월·D 일. 날짜는 cycle 별 포맷: M→YYYYMM, D→YYYYMMDD)
           (docs: ecos.bok.or.kr/api/)

  python -m main_mission.portfolio_os.macro_connect --load        # ECOS+FRED 적재(키 필요)
  python -m main_mission.portfolio_os.macro_connect --snapshot    # 최신 지표+freshness(키 불필요)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from .store import db as store_db

ROOT = Path(__file__).resolve().parents[2]

# freshness: obs_date 가 이만큼 오래되면 stale (지표 성격상 월/분기 발표라 기본 60일).
#   evidence(반감기 90일) 와 같은 사상이되, 거시지표는 발표주기가 있어 STALE 임계를 따로 둔다.
STALE_DAYS_DEFAULT = 60.0
HALF_LIFE_DAYS = 45.0   # freshness confidence decay 반감기 (지표 신선도 가중)

# 지표별 stale 임계(발표주기 반영). 없으면 STALE_DAYS_DEFAULT.
_STALE_OVERRIDE = {
    "policy_rate": 120.0, "policy_rate_us": 120.0,   # 기준금리 — 회의 주기(6~8주) 길다
    "cpi_yoy": 75.0, "cpi_yoy_us": 75.0,             # CPI 월간
    "credit_growth_yoy": 75.0,
    "yield_10y": 10.0, "yield_2y": 10.0, "yield_10y_us": 10.0, "yield_2y_us": 10.0,  # 일간
    "fx_usdkrw": 10.0, "wti_oil": 10.0,              # 일간
    "vix": 7.0, "vkospi": 7.0,                       # 일간(심리)
    "kospi": 7.0, "kosdaq": 7.0, "nasdaq": 7.0, "sp500": 7.0,  # 일간(지수)
}


class MacroConfigError(RuntimeError):
    """거시 데이터 연결 실패(키 없음/endpoint 미확인) — 가짜 성공 금지 신호."""


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


def _parse_obs_date(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    try:
        if len(s) == 8 and s.isdigit():        # YYYYMMDD (ECOS D)
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        if len(s) == 6 and s.isdigit():        # YYYYMM (ECOS M) → 월 1일
            return date(int(s[:4]), int(s[4:6]), 1)
        if len(s) == 4 and s.isdigit():        # YYYY (ECOS A) → 연 1일
            return date(int(s), 1, 1)
        return date.fromisoformat(s[:10])       # YYYY-MM-DD (FRED)
    except (ValueError, TypeError):
        return None


def _iso_obs_date(s: str | None) -> str | None:
    d = _parse_obs_date(s)
    return d.isoformat() if d else None


# ============================================================
# freshness / stale (obs_date decay — evidence decay 사상 재사용)
# ============================================================
def freshness(obs_date: str | None, *, indicator: str | None = None,
              now: date | None = None) -> dict:
    """obs_date → {age_days, stale, decay, stale_threshold_days}. obs_date 없으면 stale=True."""
    now = now or date.today()
    d = _parse_obs_date(obs_date)
    threshold = _STALE_OVERRIDE.get(indicator or "", STALE_DAYS_DEFAULT)
    if d is None:
        return {"age_days": None, "stale": True, "decay": 0.0,
                "stale_threshold_days": threshold, "reason": "obs_date 없음/파싱 불가"}
    age = max(0, (now - d).days)
    decay = round(0.5 ** (age / HALF_LIFE_DAYS), 4)
    return {"age_days": age, "stale": age > threshold, "decay": decay,
            "stale_threshold_days": threshold,
            "reason": (f"obs_date {d.isoformat()} 가 {age}일 경과(임계 {threshold:.0f}일) — stale"
                       if age > threshold else None)}


# ============================================================
# 적재 (멱등 upsert) — PK(indicator, obs_date)
# ============================================================
def upsert_indicator(indicator: str, obs_date: str, value: float, source: str,
                     *, conn=None) -> bool:
    """macro_indicators 1행 멱등 upsert. value None/비숫자면 skip(가짜 0 금지)."""
    iso = _iso_obs_date(obs_date)
    if iso is None or value is None:
        return False
    try:
        val = float(value)
    except (ValueError, TypeError):
        return False
    own = conn is None
    conn = conn or store_db.connect()
    try:
        conn.execute(
            "INSERT INTO macro_indicators(indicator, obs_date, value, source, captured_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(indicator, obs_date) DO UPDATE SET "
            "value=excluded.value, source=excluded.source, captured_at=excluded.captured_at",
            (indicator, iso, val, source, _now()))
        conn.commit()
        return True
    finally:
        if own:
            conn.close()


def upsert_series(indicator: str, observations: list[tuple], source: str, *, conn=None) -> int:
    """[(obs_date, value), ...] 를 멱등 upsert → 기록 건수. (출처/기준일 보존)."""
    own = conn is None
    conn = conn or store_db.connect()
    n = 0
    try:
        for obs_date, value in observations:
            if upsert_indicator(indicator, obs_date, value, source, conn=conn):
                n += 1
        return n
    finally:
        if own:
            conn.close()


# ============================================================
# FRED fetcher (미국) — 공식 endpoint 검증됨
# ============================================================
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# 포트폴리오에 쓰는 미국 거시 series_id → 내부 indicator 이름.
FRED_SERIES = {
    "DFEDTARU": "policy_rate_us",     # Fed funds target upper bound (%)
    "DGS10": "yield_10y_us",          # 10Y Treasury (%)
    "DGS2": "yield_2y_us",            # 2Y Treasury (%)
    "DCOILWTICO": "wti_oil",          # WTI crude (USD/bbl)
    "VIXCLS": "vix",                  # CBOE VIX (sentiment 로도 미러)
    "NASDAQCOM": "nasdaq",            # NASDAQ Composite
    "SP500": "sp500",                 # S&P 500
    "CPIAUCSL": "cpi_index_us",       # CPI index (yoy 는 별도 계산 가능)
}


def _http_get_json(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "portfolio-os/macro"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https only)
        return json.loads(resp.read().decode("utf-8"))


def fred_api_key() -> str:
    _load_env()
    key = (os.getenv("FRED_API_KEY") or "").strip()
    if not key:
        raise MacroConfigError(
            "FRED_API_KEY 가 .env 에 없습니다 — 미국 거시(FRED) 미연동. "
            "https://fredaccount.stlouisfed.org/apikeys 에서 키 발급 후 .env 에 설정. "
            "(가짜 데이터 생성 안 함)")
    return key


def fetch_fred_series(series_id: str, *, limit: int = 12, api_key: str | None = None) -> list[tuple]:
    """FRED 한 series 최신 관측 → [(obs_date 'YYYY-MM-DD', value float)]. '.' (결측)은 제외.

    endpoint(검증): api.stlouisfed.org/fred/series/observations
      ?series_id&api_key&file_type=json&sort_order=desc&limit
    """
    key = api_key or fred_api_key()
    q = urllib.parse.urlencode({
        "series_id": series_id, "api_key": key, "file_type": "json",
        "sort_order": "desc", "limit": int(limit)})
    try:
        data = _http_get_json(f"{FRED_BASE}?{q}")
    except urllib.error.HTTPError as e:
        raise MacroConfigError(f"FRED HTTP {e.code} (series={series_id}) — 키/네트워크 확인") from e
    except urllib.error.URLError as e:
        raise MacroConfigError(f"FRED 연결 실패 (series={series_id}): {e.reason}") from e
    out: list[tuple] = []
    for o in data.get("observations", []):
        v = o.get("value")
        if v in (None, ".", ""):     # FRED 결측 = '.' → 가짜 0 금지, 제외
            continue
        try:
            out.append((o.get("date"), float(v)))
        except (ValueError, TypeError):
            continue
    return out


# ============================================================
# ECOS fetcher (한국) — 공식 endpoint 검증됨
# ============================================================
ECOS_BASE = "https://ecos.bok.or.kr/api/StatisticSearch"

# (stat_code, item_code, cycle) → indicator. 통계표/항목 코드는 ECOS 통계목록 기준.
#   ⚠️ 일부 stat/item 코드는 ECOS 가 개정할 수 있어, 응답 검증 후 적재(가짜 0 금지).
ECOS_SERIES = {
    # 한국은행 기준금리 (722Y001, 0101000 한국은행 기준금리, M 월)
    "policy_rate": {"stat": "722Y001", "item": "0101000", "cycle": "M"},
    # 국고채 10년 (817Y002, 010210000, D 일)
    "yield_10y": {"stat": "817Y002", "item": "010210000", "cycle": "D"},
    # 국고채 2년 (817Y002, 010195000, D 일)
    "yield_2y": {"stat": "817Y002", "item": "010195000", "cycle": "D"},
    # 원/달러 환율 (731Y001, 0000001 매매기준율, D 일)
    "fx_usdkrw": {"stat": "731Y001", "item": "0000001", "cycle": "D"},
    # 소비자물가 전년동월비 (901Y009, 0, M 월) — 응답 검증 후 적재
    "cpi_yoy": {"stat": "901Y009", "item": "0", "cycle": "M"},
}


def ecos_api_key() -> str:
    _load_env()
    key = (os.getenv("ECOS_API_KEY") or "").strip()
    if not key:
        raise MacroConfigError(
            "ECOS_API_KEY 가 .env 에 없습니다 — 한국 거시(한은 ECOS) 미연동. "
            "https://ecos.bok.or.kr/api/ 에서 인증키 발급 후 .env 에 설정. "
            "(가짜 데이터 생성 안 함)")
    return key


def _ecos_date_window(cycle: str, points: int) -> tuple[str, str]:
    """cycle 별 검색 시작/종료 날짜 문자열. 최신 points 개 정도 포괄하도록 넉넉히."""
    today = date.today()
    if cycle == "D":
        # 영업일 고려 — points*2일 정도 뒤로
        from datetime import timedelta
        start = today - timedelta(days=points * 2 + 10)
        return start.strftime("%Y%m%d"), today.strftime("%Y%m%d")
    if cycle == "M":
        y, m = today.year, today.month - points
        while m <= 0:
            m += 12
            y -= 1
        return f"{y}{today.month:02d}".replace(f"{today.year}", str(y)) or f"{y}{m:02d}", today.strftime("%Y%m")
    if cycle == "Q":
        return f"{today.year - 3}Q1", f"{today.year}Q4"
    return str(today.year - points), str(today.year)


def fetch_ecos_series(indicator: str, *, points: int = 12, api_key: str | None = None) -> list[tuple]:
    """ECOS 한 지표 최신 관측 → [(obs_date raw, value float)]. 설정에 없는 indicator 는 에러.

    endpoint(검증): ecos.bok.or.kr/api/StatisticSearch/{KEY}/json/kr/{1}/{N}/
      {stat}/{cycle}/{start}/{end}/{item}
    """
    cfg = ECOS_SERIES.get(indicator)
    if cfg is None:
        raise MacroConfigError(f"ECOS 미설정 indicator: {indicator!r} (ECOS_SERIES 확인)")
    key = api_key or ecos_api_key()
    start, end = _ecos_date_window(cfg["cycle"], points)
    n = max(points, 100)
    path = "/".join([ECOS_BASE, key, "json", "kr", "1", str(n),
                     cfg["stat"], cfg["cycle"], start, end, cfg["item"]])
    try:
        data = _http_get_json(path)
    except urllib.error.HTTPError as e:
        raise MacroConfigError(f"ECOS HTTP {e.code} (indicator={indicator}) — 키/코드 확인") from e
    except urllib.error.URLError as e:
        raise MacroConfigError(f"ECOS 연결 실패 (indicator={indicator}): {e.reason}") from e
    # ECOS 오류 응답: {"RESULT": {"CODE": "INFO-xxx", "MESSAGE": ".."}}
    if "RESULT" in data and "StatisticSearch" not in data:
        msg = (data.get("RESULT") or {}).get("MESSAGE", "")
        raise MacroConfigError(f"ECOS 응답 오류 (indicator={indicator}): {msg} "
                               "— 통계표/항목 코드 확인 필요(가짜 데이터 생성 안 함)")
    rows = ((data.get("StatisticSearch") or {}).get("row")) or []
    out: list[tuple] = []
    for r in rows:
        v = r.get("DATA_VALUE")
        if v in (None, "", "-"):
            continue
        try:
            out.append((r.get("TIME"), float(v)))
        except (ValueError, TypeError):
            continue
    return out


# ============================================================
# 적재 오케스트레이션
# ============================================================
def load_fred(*, limit: int = 14, conn=None) -> dict:  # 14개월 — CPI 전년比(YoY) 계산용 이력 확보
    """FRED 미국 거시 전체 적재. 키 없으면 MacroConfigError(상위에서 not_connected 처리)."""
    key = fred_api_key()
    own = conn is None
    conn = conn or store_db.connect()
    written: dict[str, int] = {}
    try:
        for series_id, indicator in FRED_SERIES.items():
            obs = fetch_fred_series(series_id, limit=limit, api_key=key)
            written[indicator] = upsert_series(indicator, obs, "fred", conn=conn)
        return {"source": "fred", "written": written, "total": sum(written.values())}
    finally:
        if own:
            conn.close()


def load_ecos(*, points: int = 14, conn=None) -> dict:  # 14개월 — CPI 전년比(YoY) 계산용 이력 확보
    """ECOS 한국 거시 전체 적재. 키 없으면 MacroConfigError."""
    key = ecos_api_key()
    own = conn is None
    conn = conn or store_db.connect()
    written: dict[str, int] = {}
    errors: dict[str, str] = {}
    try:
        for indicator in ECOS_SERIES:
            try:
                obs = fetch_ecos_series(indicator, points=points, api_key=key)
                written[indicator] = upsert_series(indicator, obs, "ecos", conn=conn)
            except MacroConfigError as e:
                # 개별 통계표 코드 개정 시 — 정직하게 기록(다른 지표는 계속 적재).
                errors[indicator] = str(e)
        return {"source": "ecos", "written": written, "errors": errors,
                "total": sum(written.values())}
    finally:
        if own:
            conn.close()


def load_all(*, conn=None) -> dict:
    """ECOS + FRED 모두 적재 시도. 한쪽 키만 있어도 가능한 만큼 적재(정직)."""
    out: dict = {"loaded_at": _now(), "fred": None, "ecos": None, "not_connected": []}
    try:
        out["fred"] = load_fred(conn=conn)
    except MacroConfigError as e:
        out["not_connected"].append({"source": "fred", "reason": str(e)})
    try:
        out["ecos"] = load_ecos(conn=conn)
    except MacroConfigError as e:
        out["not_connected"].append({"source": "ecos", "reason": str(e)})
    out["any_loaded"] = bool((out["fred"] and out["fred"]["total"])
                             or (out["ecos"] and out["ecos"]["total"]))
    return out


# ============================================================
# 스냅샷 — 최신 지표 + freshness/stale (키 불필요, DB 만 읽음)
# ============================================================
def macro_snapshot(*, conn=None) -> dict:
    """macro_indicators 의 각 indicator 최신값 + obs_date + freshness/stale.

    데이터 없으면 data_available=False(가짜 점수 금지). 거시가 비어 있으면 정직하게 not_connected.
    """
    own = conn is None
    conn = conn or store_db.connect()
    try:
        rows = conn.execute(
            "SELECT m.indicator, m.obs_date, m.value, m.source, m.captured_at "
            "FROM macro_indicators m "
            "JOIN (SELECT indicator, MAX(obs_date) md FROM macro_indicators GROUP BY indicator) x "
            "ON m.indicator=x.indicator AND m.obs_date=x.md").fetchall()
    finally:
        if own:
            conn.close()

    today = date.today()
    indicators: dict[str, dict] = {}
    fresh_n = stale_n = 0
    for r in rows:
        fr = freshness(r["obs_date"], indicator=r["indicator"], now=today)
        indicators[r["indicator"]] = {
            "value": float(r["value"]), "obs_date": r["obs_date"], "source": r["source"],
            "captured_at": r["captured_at"], "age_days": fr["age_days"],
            "stale": fr["stale"], "freshness_decay": fr["decay"],
            "stale_threshold_days": fr["stale_threshold_days"], "stale_reason": fr["reason"]}
        if fr["stale"]:
            stale_n += 1
        else:
            fresh_n += 1

    data_available = bool(indicators)
    return {
        "data_available": data_available,
        "as_of": today.isoformat(),
        "indicators": indicators,
        "fresh_count": fresh_n,
        "stale_count": stale_n,
        "note": ("거시지표 미연동 — ECOS_API_KEY/FRED_API_KEY 설정 후 "
                 "`python -m main_mission.portfolio_os.macro_connect --load` 로 적재하세요."
                 if not data_available else
                 f"거시 {len(indicators)}개 지표 (신선 {fresh_n} / stale {stale_n}). "
                 "stale 지표는 판단 가중을 낮춥니다(정직)."),
    }


def _spread(ind: dict, a: str, b: str):
    """장단기 스프레드(국내/미국 자동 선택). 둘 다 fresh 일 때만 사용(stale 은 None)."""
    ra, rb = ind.get(a), ind.get(b)
    if ra and rb and not ra["stale"] and not rb["stale"]:
        return round(ra["value"] - rb["value"], 3)
    return None


# ============================================================
# 거시 → 포트폴리오 매핑 (판단 신호 — 후보만, 자동적용 금지)
# ============================================================
def cpi_yoy_from_index(indicator: str, *, conn=None) -> float | None:
    """CPI '지수' 시계열에서 전년比(YoY) 계산 — 최신 obs vs 약 12개월 전 obs.
    이력 부족(13개월 미만)이면 None(미계산 — false YoY 라벨 금지). 월간 가정."""
    own = conn is None
    conn = conn or store_db.connect()
    try:
        rows = conn.execute(
            "SELECT value FROM macro_indicators WHERE indicator=? ORDER BY obs_date DESC LIMIT 14",
            (indicator,)).fetchall()
    except Exception:  # noqa: BLE001
        return None
    finally:
        if own:
            conn.close()
    if len(rows) < 13:
        return None
    try:
        latest = float(rows[0]["value"]); year_ago = float(rows[12]["value"])
    except (TypeError, ValueError, KeyError):
        return None
    if year_ago <= 0:
        return None
    return round((latest / year_ago - 1.0) * 100.0, 1)


def macro_to_portfolio(snapshot: dict | None = None) -> dict:
    """거시 스냅샷 → 포트폴리오 의미 신호(후보). **주문/policy 자동변경 0.**

    규칙(CEO 강조 — 거시 우선):
      - 금리↑/인상기      → 현금/단기채↑, 위험자산↓, 성장속도 완화.
      - 장단기 역전(<0)   → 침체 선행 → 방어↑, 헤지 검토.
      - CPI(고인플레)↑    → 현금/단기채 선호, 듀레이션 짧게.
      - 달러(원/달러)↑    → 미국ETF/달러노출 우호(환차익), 단 과열 경계.
      - 유가↑             → 인플레/비용 압력 → 방어 가산.
      - VIX↑(공포)        → 헤지/현금 검토.
    stale 지표는 신호에서 제외(정직). 데이터 없으면 connected=False(거시 미연동 명시)."""
    snap = snapshot or macro_snapshot()
    ind = snap.get("indicators", {})
    if not snap.get("data_available"):
        return {"connected": False, "signals": [], "tilts": {},
                "note": "거시 미연동 — 거시→포트폴리오 매핑 불가(정직). 키 설정·적재 필요.",
                "requires_user_approval": True, "auto_applied": False}

    def fresh(name):
        x = ind.get(name)
        return x if (x and not x["stale"]) else None

    signals: list[dict] = []
    # tilt: 방향(+ 방어/현금 강화, - 위험 강화) 누적 — 후보 강도 표현용(자동적용 아님).
    tilts = {"cash_band": 0.0, "short_bond": 0.0, "risk_assets": 0.0,
             "growth_pace": 0.0, "us_etf": 0.0, "usd_exposure": 0.0, "hedge": 0.0,
             "bond_duration": 0.0}

    def add(name, direction, detail, **deltas):
        signals.append({"name": name, "direction": direction, "detail": detail})
        for k, v in deltas.items():
            tilts[k] = round(tilts.get(k, 0.0) + v, 3)

    # 1) 금리 수준/인상기 — 한·미 기준금리 + 10년물.
    for rate_key, label in (("policy_rate", "한국 기준금리"), ("policy_rate_us", "미국 기준금리")):
        r = fresh(rate_key)
        if r and r["value"] >= 3.0:
            add(f"high_rate_{rate_key}", "defensive",
                f"{label} {r['value']:.2f}% — 고금리 환경: 현금/단기채 선호, 위험자산 속도 완화.",
                cash_band=+1.0, short_bond=+1.0, risk_assets=-1.0, growth_pace=-1.0,
                bond_duration=-1.0)

    # 2) 장단기 금리 역전 — 한국/미국.
    for a, b, label in (("yield_10y", "yield_2y", "한국 국채"),
                        ("yield_10y_us", "yield_2y_us", "미국 국채")):
        sp = _spread(ind, a, b)
        if sp is not None and sp <= 0:
            add(f"curve_inversion_{a}", "defensive",
                f"{label} 10Y-2Y {sp:+.2f}%p (역전) — 침체 선행: 방어↑·헤지 검토.",
                cash_band=+1.0, risk_assets=-1.0, hedge=+1.0)

    # 3) 고인플레 — CPI는 *지수값*이라 전년比(YoY)를 12개월 전 지수에서 계산. 계산 가능한 첫 지표(한국 우선·미국 폴백) 사용.
    #    이력 부족이면 표기 생략(지수값을 전년比%로 오표기 금지).
    for cpi_name in [n for n in ("cpi_yoy", "cpi_index_us") if fresh(n)]:
        yoy = cpi_yoy_from_index(cpi_name)
        if yoy is None:
            continue   # 이 지표는 이력 부족 — 다음 지표 시도(false YoY 금지)
        if yoy >= 3.0:
            label = "한국" if cpi_name == "cpi_yoy" else "미국"
            add("high_inflation", "defensive",
                f"{label} 소비자물가 전년比 {yoy:.1f}% — 인플레 부담: 현금/단기채 선호, 듀레이션 짧게.",
                cash_band=+0.5, short_bond=+1.0, bond_duration=-1.0)
        break  # 계산 가능한 첫 지표만 사용(중복 신호 방지)

    # 4) 달러(원/달러) 강세 → 미국ETF/달러노출 우호.
    fx = fresh("fx_usdkrw")
    if fx:
        if fx["value"] >= 1350.0:
            add("usd_strong", "usd_favorable",
                f"원/달러 {fx['value']:.0f} — 달러 강세: 미국ETF/달러노출 우호(환차익), 단 추격 경계.",
                us_etf=+1.0, usd_exposure=+1.0)
        elif fx["value"] <= 1200.0:
            add("usd_weak", "usd_caution",
                f"원/달러 {fx['value']:.0f} — 달러 약세: 미국ETF 신규 환노출은 분할/관망 검토.",
                us_etf=-0.5, usd_exposure=-0.5)

    # 5) 유가(WTI)↑ → 인플레/비용 압력.
    oil = fresh("wti_oil")
    if oil and oil["value"] >= 90.0:
        add("oil_high", "defensive",
            f"WTI {oil['value']:.0f}달러 — 유가 부담(인플레/비용): 방어 가산.",
            cash_band=+0.5, risk_assets=-0.5)

    # 6) VIX(공포) — sentiment 미러(macro 적재 시 함께 들어옴).
    vix = fresh("vix")
    if vix and vix["value"] >= 25.0:
        add("fear_spike", "defensive",
            f"VIX {vix['value']:.0f} — 변동성 확대(공포): 헤지/현금 검토.",
            cash_band=+0.5, hedge=+1.0, risk_assets=-0.5)

    # 종합 성향: 방어 tilt 합이 양(+)이면 defensive 기울기.
    defensive_score = round(tilts["cash_band"] + tilts["short_bond"] + tilts["hedge"]
                            - tilts["risk_assets"], 2)
    if defensive_score >= 2.0:
        lean = "defensive"
    elif defensive_score <= -1.0:
        lean = "aggressive"
    else:
        lean = "neutral"

    return {
        "connected": True,
        "as_of": snap.get("as_of"),
        "lean": lean,                      # 거시가 가리키는 방어/공격 기울기(후보)
        "defensive_score": defensive_score,
        "signals": signals,                # 사람이 읽는 거시 해석(후보)
        "tilts": tilts,                    # 버킷별 방향 가중(자동적용 아님)
        "fresh_count": snap.get("fresh_count"),
        "stale_count": snap.get("stale_count"),
        "requires_user_approval": True,
        "auto_applied": False,
        "auto_order_created": False,
        "note": ("거시→포트폴리오 해석입니다(후보). 현금밴드/채권/달러노출/미국ETF/헤지에 "
                 "'방향'만 제시하며, 비중·주문 자동변경은 없습니다(사람 승인 필요). "
                 "stale 지표는 신호에서 제외했습니다(정직)."),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", action="store_true", help="ECOS+FRED 적재(키 필요)")
    ap.add_argument("--snapshot", action="store_true", help="최신 지표+freshness 출력")
    ap.add_argument("--map", action="store_true", help="거시→포트폴리오 매핑 출력")
    args = ap.parse_args()
    try:
        if args.load:
            out = load_all()
        elif args.map:
            out = macro_to_portfolio()
        else:
            out = macro_snapshot()
    except MacroConfigError as e:
        out = {"ok": False, "not_connected": True, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
