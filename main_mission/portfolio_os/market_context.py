"""Market context — 금리·경제 전망 + 채권 듀레이션 추천 (규칙 기반).

CEO 방침: 채권 듀레이션(단기/중기/장기)은 금리·경제 전망에 따라
**매 점검마다 지속 분석·추천**되어야 한다.

지능 원칙(§17): Anthropic/LLM API 미사용. 추천은 **규칙 + 프로젝트 데이터**로만 산출.

데이터 정직성(§7, §2-9): 실시간 금리·경제 데이터 소스는 **아직 미연동**.
  → mock 숫자를 진짜처럼 제시하지 않는다. data_connected=False 로 명시하고,
    보수적 기본값(불확실)을 "데이터 소스 미연동" 라벨과 함께 사용한다.
  → 실제 피드(FRED / 한국은행 / 뉴스)가 붙으면 current_context() 가 그 값을 채우면 된다.

  python -m main_mission.portfolio_os.market_context
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .store import db as store_db

# 듀레이션 라벨(한글) — 단기/중기/장기/사다리 3+1종 명시.
DURATION_KO = {
    "short": "단기",
    "intermediate": "중기",
    "long": "장기",
    "mixed": "사다리(혼합)",
}

_RATE_OUTLOOKS = ("rising", "falling", "uncertain")
_ECONOMY_STATES = ("expansion", "slowdown", "uncertain")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_context() -> dict:
    """금리·경제 전망 구조 반환.

    실시간 데이터 소스 미연동 → data_connected=False + 보수적 기본값(불확실).
    구조는 실제 피드(FRED/한은/뉴스)가 그대로 채울 수 있도록 설계.
    """
    # NOTE: 실시간 금리/경제 피드가 연동되면 이 블록에서 rate_outlook/economy/지표를 채운다.
    # 현재는 어떤 소스도 연결돼 있지 않으므로 정직하게 미연동을 표시한다.
    return {
        "rate_outlook": "uncertain",   # rising | falling | uncertain
        "economy": "uncertain",        # expansion | slowdown | uncertain
        "summary": "금리·경제 데이터 소스 미연동 — 보수적 기본값(불확실) 사용",
        "source": None,                # 예: "FRED:DGS10" / "BOK:base_rate" / "news" (연동 시 채움)
        "data_connected": False,       # 실데이터 연동 여부 — False면 추천은 보수적 기본값
        # 구조화된 지표 슬롯(연동 시 채움). 지금은 비어 있음(가짜 숫자 금지).
        "rates": {},                   # 예: {"us_10y": 4.2, "kr_base": 3.0}
        "fx": {},                      # 예: {"usdkrw": 1350}
        "indices": {},                 # 예: {"kospi": 2600}
        "news": [],                    # 예: [{"title": ..., "source": ...}]
        "captured_at": _now(),
    }


def recommend_duration(context: dict, current_pref: str | None = None) -> dict:
    """금리·경제 전망 → 채권 듀레이션 추천 (규칙 기반, 단기/중기/장기 3종 명시).

    규칙:
      - 금리 상승(rising): 듀레이션 확대는 평가손 위험 → 단기(short) 권장, 장기 경고.
      - 금리 불확실(uncertain): 사다리(mixed)로 분산 → 단기+중기 혼합, 장기 비중 경고.
      - 금리 하락(falling) + 경기 둔화(slowdown): 장기(long) 가능 — 듀레이션 확대로 금리하락 베팅.
      - 금리 하락 + 그 외: 중기(intermediate) 권장 — 일부 듀레이션 확대.
    """
    rate = (context or {}).get("rate_outlook", "uncertain")
    economy = (context or {}).get("economy", "uncertain")
    connected = bool((context or {}).get("data_connected", False))

    warnings: list[str] = []

    if rate == "rising":
        recommended = "short"
        reason = "금리 상승 국면 — 듀레이션 확대 시 채권 평가손 위험. 단기채로 금리 재투자 유리."
        warnings.append("장기채(long) 비중 확대는 금리 상승기 평가손 위험 — 제한 권장")
    elif rate == "falling":
        if economy == "slowdown":
            recommended = "long"
            reason = "금리 하락 + 경기 둔화 — 듀레이션 확대(장기채)로 금리하락(가격상승) 베팅 가능."
        else:
            recommended = "intermediate"
            reason = "금리 하락 국면 — 중기채로 일부 듀레이션 확대(과도한 장기 집중은 보류)."
    else:  # uncertain (기본값 포함)
        recommended = "mixed"
        reason = "금리 방향 불확실 — 단기·중기 사다리(ladder)로 분산해 금리 방향 베팅 회피."
        warnings.append("장기채(long) 단독 비중 확대는 방향 불확실로 위험 — 사다리 분산 권장")

    if not connected:
        # 실데이터 미연동 → 추천은 어디까지나 보수적 기본값임을 명시(정직성).
        warnings.insert(0, "금리·경제 데이터 소스 미연동 — 보수적 기본값(불확실) 기준 추천")

    # 현재 선호(account 의 bond_duration_pref)와 비교.
    vs_current = None
    if current_pref:
        cur = current_pref if current_pref in DURATION_KO else current_pref
        if cur == recommended:
            vs_current = f"현재 선호({DURATION_KO.get(cur, cur)})와 추천 일치 — 변경 불필요"
        else:
            vs_current = (
                f"현재 선호 {DURATION_KO.get(cur, cur)} → 추천 {DURATION_KO.get(recommended, recommended)} "
                f"(금리·경제 전망에 따른 점검 결과)"
            )

    return {
        "recommended": recommended,                       # short|intermediate|long|mixed
        "recommended_ko": DURATION_KO.get(recommended, recommended),
        "reason": reason,
        "warnings": warnings,
        "vs_current": vs_current,
        "current_pref": current_pref,
        "rate_outlook": rate,
        "economy": economy,
        "data_connected": connected,
    }


def save_snapshot(context: dict) -> int:
    """market_context_snapshots 에 1행 insert → id 반환.

    rates/fx/indices/news 는 JSON 으로 저장. summary 는 사람이 읽는 요약.
    """
    ctx = context or {}
    conn = store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO market_context_snapshots(rates_json, fx_json, indices_json, news_json, summary, captured_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                json.dumps(ctx.get("rates", {}), ensure_ascii=False),
                json.dumps(ctx.get("fx", {}), ensure_ascii=False),
                json.dumps(ctx.get("indices", {}), ensure_ascii=False),
                json.dumps(ctx.get("news", []), ensure_ascii=False),
                ctx.get("summary"),
                ctx.get("captured_at") or _now(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def main() -> int:
    import sys

    ctx = current_context()
    rec = recommend_duration(ctx)
    sys.stdout.write(json.dumps({"context": ctx, "duration_recommendation": rec}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
