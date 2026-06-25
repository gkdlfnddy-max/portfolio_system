"""이벤트 캘린더 + 심리지표 적재/조회 (decline 5/6축 데이터 연결).

본 모듈은 decline 의 **event 축**과 **sentiment 축**에 데이터를 채우는 ingestion/조회 지점이다.

원칙 (CLAUDE.md):
  - **이벤트 = 일정·위험 알림(예측 아님).** "FOMC 가 D-2 이니 발표 전후 변동성↑" 같은
    *사실 기반 일정 경보*만 만든다. "하락한다/오른다" 같은 방향 예측은 하지 않는다.
  - **자동주문 0.** 본 모듈은 어떤 주문도 만들지 않는다(조회/적재/알림 후보만).
  - **데이터 없으면 data_available=False (가짜 0 금지).** 일정/지표가 없으면 정직하게
    "미연동"으로 알린다. placeholder(가짜 일정/가짜 VIX) 금지.
  - **공식/공개 일정 우선.** 자동 연동(외부 캘린더 API)은 아직 없다 → 운영자가
    공식 발표 일정을 `seed_official_schedule()` 또는 `add_event()` 로 **수동 입력**한다.
    수동 입력임을 source 로 정직하게 표기(`manual`).
  - **secret 0.** 외부 키/자격증명을 쓰지 않는다.

테이블 (이미 생성됨 — 편집 금지):
  market_events(id, event_date, name, impact, region, source, captured_at)
  sentiment_index(indicator, obs_date, value, source, captured_at)  PK(indicator, obs_date)

거시축과의 분리:
  - macro(거시) = 금리/환율/유가/인플레 (macro_connect → macro_indicators)
  - sentiment(심리) = VIX/VKOSPI/풋콜/거래대금 (본 모듈 → sentiment_index)
  거시에 VIX 가 미러될 수 있으나, 심리축의 정식 데이터 출처는 sentiment_index 다.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from .store import db as store_db

# event 축이 인식하는 심리지표 키 (decline.context 의 _SENTIMENT_KEYS 와 정렬)
SENTIMENT_KEYS = ["vix", "vkospi", "put_call_ratio", "margin_balance_change_1m",
                  "trading_value_change"]

# 임박 경보 임계 (event 축 THRESHOLDS 와 동일 사상) — 발표 N일 이내면 변동성 경보.
ALERT_WINDOW_DAYS = 7      # 이 안에 들면 "다가오는 이벤트"로 표시
IMMINENT_DAYS = 3          # 이 안 + high impact 면 변동성 경보 발화

VALID_IMPACT = {"high", "medium", "low"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_date(s) -> str | None:
    """다양한 입력을 'YYYY-MM-DD' 로 정규화. 파싱 불가면 None(가짜 날짜 금지)."""
    if s is None:
        return None
    if isinstance(s, date):
        return s.isoformat()
    s = str(s).strip()
    try:
        if len(s) == 8 and s.isdigit():           # YYYYMMDD
            return date(int(s[:4]), int(s[4:6]), int(s[6:8])).isoformat()
        return date.fromisoformat(s[:10]).isoformat()
    except (ValueError, TypeError):
        return None


# ============================================================
# 1) 이벤트 캘린더 — 적재(멱등) / 조회
# ============================================================
def add_event(event_date, name: str, *, impact: str = "medium",
              region: str | None = None, source: str = "manual", conn=None) -> bool:
    """market_events 1건 적재. 같은 (event_date, name, region) 은 멱등(중복 적재 안 함).

    date/name 누락이나 파싱 실패면 skip(가짜 일정 금지) → False.
    source 기본 'manual' — 자동 연동이 아니라 운영자 수동 입력임을 정직하게 표기.
    """
    iso = _iso_date(event_date)
    if iso is None or not name or not str(name).strip():
        return False
    imp = str(impact).lower().strip()
    if imp not in VALID_IMPACT:
        imp = "medium"
    own = conn is None
    conn = conn or store_db.connect()
    try:
        # (event_date, name, region) 동일하면 중복으로 보고 갱신만(멱등).
        existing = conn.execute(
            "SELECT id FROM market_events WHERE event_date=? AND name=? "
            "AND IFNULL(region,'')=IFNULL(?,'') LIMIT 1",
            (iso, str(name).strip(), region)).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE market_events SET impact=?, source=?, captured_at=? WHERE id=?",
                (imp, source, _now(), existing["id"]))
        else:
            conn.execute(
                "INSERT INTO market_events(event_date, name, impact, region, source, captured_at) "
                "VALUES(?,?,?,?,?,?)",
                (iso, str(name).strip(), imp, region, source, _now()))
        conn.commit()
        return True
    finally:
        if own:
            conn.close()


def add_events(events: list[dict], *, source: str = "manual", conn=None) -> int:
    """여러 일정 일괄 적재 → 적재 건수. 각 dict: {event_date, name, impact?, region?}."""
    own = conn is None
    conn = conn or store_db.connect()
    n = 0
    try:
        for e in events:
            if add_event(e.get("event_date"), e.get("name"),
                         impact=e.get("impact", "medium"),
                         region=e.get("region"),
                         source=e.get("source", source), conn=conn):
                n += 1
        return n
    finally:
        if own:
            conn.close()


# 공식/공개 발표 *유형*(템플릿). 실제 발표일은 매월/매기 공식 캘린더에서 확정되므로,
# 여기에 날짜를 하드코딩하지 않는다(placeholder 금지). 운영자가 날짜를 넣어 seed 한다.
OFFICIAL_EVENT_TYPES = [
    {"name": "FOMC",         "impact": "high",   "region": "US",
     "desc": "미 연준 통화정책회의 — 금리/점도표"},
    {"name": "한국 금통위",   "impact": "high",   "region": "KR",
     "desc": "한국은행 금융통화위원회 — 기준금리"},
    {"name": "미국 CPI",      "impact": "high",   "region": "US",
     "desc": "미 소비자물가 — 인플레/금리경로"},
    {"name": "미국 고용(NFP)", "impact": "high",   "region": "US",
     "desc": "미 비농업 고용 — 경기/금리경로"},
    {"name": "한국 CPI",      "impact": "medium", "region": "KR",
     "desc": "한국 소비자물가"},
    {"name": "실적발표",      "impact": "medium", "region": None,
     "desc": "주요 기업 분기 실적"},
    {"name": "배당락",        "impact": "low",    "region": "KR",
     "desc": "배당 기준일 경과(배당락)"},
]


def seed_official_schedule(dated_events: list[dict], *, conn=None) -> int:
    """공식 발표 일정을 적재한다. dated_events: [{name, event_date, impact?, region?}, ...].

    **공식 캘린더에서 확정된 날짜를 운영자가 넣어야** 한다(자동 연동 미구현).
    유형 템플릿(OFFICIAL_EVENT_TYPES)으로 impact/region 기본값을 보완한다.
    날짜 없는 항목은 skip(placeholder 금지).
    """
    by_name = {t["name"]: t for t in OFFICIAL_EVENT_TYPES}
    rows = []
    for e in dated_events:
        name = e.get("name")
        iso = _iso_date(e.get("event_date"))
        if not name or iso is None:
            continue  # 날짜 없는 공식 일정은 적재 안 함(정직)
        tmpl = by_name.get(name, {})
        rows.append({
            "event_date": iso, "name": name,
            "impact": e.get("impact", tmpl.get("impact", "medium")),
            "region": e.get("region", tmpl.get("region")),
        })
    return add_events(rows, source="official", conn=conn)


def list_events(*, region: str | None = None, limit: int = 100, conn=None) -> list[dict]:
    """적재된 이벤트 조회(최신순). region 필터 가능."""
    own = conn is None
    conn = conn or store_db.connect()
    try:
        if region:
            rows = conn.execute(
                "SELECT event_date, name, impact, region, source FROM market_events "
                "WHERE region=? ORDER BY event_date DESC LIMIT ?", (region, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT event_date, name, impact, region, source FROM market_events "
                "ORDER BY event_date DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def upcoming_events(*, as_of: str | None = None, window_days: int = ALERT_WINDOW_DAYS,
                    region: str | None = None, conn=None) -> list[dict]:
    """as_of 기준 window_days 이내(오늘 이후)의 다가오는 일정 → days_until 부여, 임박순.

    데이터 없으면 빈 리스트(가짜 일정 금지).
    """
    aod = date.fromisoformat(_iso_date(as_of) or date.today().isoformat())
    out = []
    for e in list_events(region=region, limit=500, conn=conn):
        ed = _iso_date(e.get("event_date"))
        if ed is None:
            continue
        days = (date.fromisoformat(ed) - aod).days
        if 0 <= days <= window_days:
            out.append({**e, "event_date": ed, "days_until": days})
    return sorted(out, key=lambda x: x["days_until"])


# ============================================================
# 2) 이벤트 위험 알림 (예측 아님 — 일정 기반 변동성 경보)
# ============================================================
def event_risk_alert(*, as_of: str | None = None, region: str | None = None, conn=None) -> dict:
    """다가오는 고영향 발표를 **변동성 위험 알림**으로 정리(예측 아님·자동주문 0).

    반환:
      {data_available, as_of, upcoming:[...], imminent:[...],
       alert(bool), suggestions:[...](관망/진입속도/현금/헤지 후보 — 사람 승인 필요), note}

    suggestions 는 *후보 조언*일 뿐 자동 적용/주문이 아니다. 방향(상승/하락) 예측을 담지 않는다.
    """
    aod = _iso_date(as_of) or date.today().isoformat()
    up = upcoming_events(as_of=aod, region=region, conn=conn)
    if not up:
        # 일정 데이터가 아예 없거나 임박 없음. 데이터 자체 유무를 정직하게 구분.
        any_event = bool(list_events(limit=1, conn=conn))
        return {
            "data_available": any_event,
            "as_of": aod, "upcoming": [], "imminent": [], "alert": False,
            "suggestions": [],
            "note": ("다가오는 고영향 일정 없음(데이터 있음)." if any_event
                     else "경제 캘린더 미연동 — 이벤트 데이터 없음(공식 일정 수동 입력 필요)."),
        }
    imminent = [e for e in up
                if e["days_until"] <= IMMINENT_DAYS and str(e["impact"]).lower() == "high"]
    alert = bool(imminent)
    suggestions = []
    if alert:
        nearest = imminent[0]
        suggestions = [
            f"{nearest['name']} D-{nearest['days_until']} 전후 변동성 확대 가능 — "
            "신규 진입 속도 보수적(분할/관망) 검토(후보).",
            "발표 전 현금/헤지 후보 비중 점검(자동 적용 아님, 사람 승인 필요).",
        ]
    return {
        "data_available": True,
        "as_of": aod,
        "upcoming": up,
        "imminent": imminent,
        "alert": alert,
        "suggestions": suggestions,
        "note": ("일정 기반 변동성 위험 알림입니다(예측 아님). 발표 전후 변동성↑ 가능성만 "
                 "알리며, 방향(상승/하락) 예측·자동주문은 없습니다."),
    }


# ============================================================
# 3) 심리지표 — sentiment_index 적재(멱등) / 조회 (거시와 분리)
# ============================================================
def upsert_sentiment(indicator: str, obs_date, value, source: str = "manual", *, conn=None) -> bool:
    """sentiment_index 1행 멱등 upsert. value None/비숫자면 skip(가짜 0 금지) → False.

    indicator 예: vix | vkospi | put_call_ratio | margin_balance_change_1m | trading_value_change.
    """
    iso = _iso_date(obs_date)
    if iso is None or not indicator or value is None:
        return False
    try:
        val = float(value)
    except (ValueError, TypeError):
        return False
    own = conn is None
    conn = conn or store_db.connect()
    try:
        conn.execute(
            "INSERT INTO sentiment_index(indicator, obs_date, value, source, captured_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(indicator, obs_date) DO UPDATE SET "
            "value=excluded.value, source=excluded.source, captured_at=excluded.captured_at",
            (str(indicator).strip(), iso, val, source, _now()))
        conn.commit()
        return True
    finally:
        if own:
            conn.close()


def upsert_sentiment_series(indicator: str, observations: list[tuple],
                            source: str = "manual", *, conn=None) -> int:
    """[(obs_date, value), ...] 멱등 적재 → 건수."""
    own = conn is None
    conn = conn or store_db.connect()
    n = 0
    try:
        for obs_date, value in observations:
            if upsert_sentiment(indicator, obs_date, value, source, conn=conn):
                n += 1
        return n
    finally:
        if own:
            conn.close()


def sentiment_snapshot(*, conn=None) -> dict:
    """각 심리지표 최신값 1개씩 → {indicator: {value, obs_date, source}}.

    데이터 없으면 빈 dict(가짜 0 금지). 심리축이 data_available 판단에 쓴다.
    """
    own = conn is None
    conn = conn or store_db.connect()
    try:
        out: dict[str, dict] = {}
        for k in SENTIMENT_KEYS:
            row = conn.execute(
                "SELECT value, obs_date, source FROM sentiment_index "
                "WHERE indicator=? ORDER BY obs_date DESC LIMIT 1", (k,)).fetchone()
            if row is not None:
                out[k] = {"value": float(row["value"]), "obs_date": row["obs_date"],
                          "source": row["source"]}
        return out
    finally:
        if own:
            conn.close()


def sentiment_coverage(*, conn=None) -> dict:
    """심리축 데이터 정직성 요약 — 몇 개 지표가 실제로 적재됐는지.

    VIX 하나로 '심리축 완성' 과장 금지: present/total 과 confidence_hint 를 정직하게 보고.
    """
    snap = sentiment_snapshot(conn=conn)
    present = sorted(snap.keys())
    total = len(SENTIMENT_KEYS)
    n = len(present)
    return {
        "data_available": n > 0,
        "present": present,
        "missing": [k for k in SENTIMENT_KEYS if k not in snap],
        "present_count": n,
        "total": total,
        # 지표 수에 따른 확신 힌트(과장 방지). 1개면 낮음.
        "confidence_hint": round(0.3 + 0.7 * (n / float(total)), 3) if n else 0.0,
        "note": ("심리지표 미연동(VIX/VKOSPI/풋콜/거래대금)." if n == 0
                 else f"심리지표 {n}/{total}개 적재. 1~2개면 확신 낮게 해석(과장 금지)."),
    }


def main() -> int:  # pragma: no cover - CLI 편의(자동주문/연동 없음)
    import argparse
    ap = argparse.ArgumentParser(description="이벤트 캘린더 / 심리지표 조회(읽기 전용).")
    ap.add_argument("--events", action="store_true", help="다가오는 이벤트 알림")
    ap.add_argument("--sentiment", action="store_true", help="심리지표 커버리지")
    args = ap.parse_args()
    if args.events or not (args.events or args.sentiment):
        a = event_risk_alert()
        print("[이벤트] data_available=", a["data_available"], a["note"])
        for e in a["upcoming"]:
            print(f"  D-{e['days_until']:>2} {e['event_date']} {e['name']} ({e['impact']})")
    if args.sentiment:
        c = sentiment_coverage()
        print("[심리] ", c["note"], "present=", c["present"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
