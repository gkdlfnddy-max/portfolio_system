"""중전제(관심 분야 + 내 생각) **정리·핵심 아이디어 도출 + AI 의견/개선 제안**.

대전제 distill 과 짝을 이루는 중전제 분석.
  - 내 생각(views) → **핵심 아이디어** 추출 (지역/상품/진입/방어/개별 선호)
  - 관심 테마 → 역할(롱/헤지) + **메모리 의견**(lessons)
  - **AI 의견 + 개선 제안** (규칙 + 메모리 + 외부조사 누적). 사람이 반영/보류.

지능 = Claude+메모리 (Anthropic API 미사용). 즉시 결과는 규칙+메모리, 심층은 Claude 가 세션에서 보강.

  python -m main_mission.portfolio_os.analysis --account 1 --analyze
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from .store import db as store_db
from . import profile as profile_mod
from . import lessons as lessons_mod


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_ideas(views: str, interests: str) -> list[str]:
    """내 생각(자유 서술) → 핵심 아이디어 (규칙 기반 추출)."""
    v = (views or "") + " " + (interests or "")
    ideas = []
    if re.search(r"미국", v) and re.search(r"한국|국내", v):
        ideas.append("미국·한국 중심, 전세계는 소량 반영")
    elif re.search(r"전\s*세계|글로벌", v):
        ideas.append("전세계 분산 지향")
    if re.search(r"ETF|상장지수", v):
        ideas.append("ETF 선호 (개별 종목 리스크 분산)")
    if re.search(r"운용사|액티브|아크|ARK|ARKK", v):
        ideas.append("액티브 운용사(ARK류 테마 묶음) 수용")
    if re.search(r"분할|천천히|느리게|나눠", v):
        ideas.append("천천히 분할 매수로 진입")
    if re.search(r"현금[^.]{0,10}(대응|비중|방어|들고)", v):
        ideas.append("현금 비중으로 변동성 대응")
    if re.search(r"저평가|밸류|싸게|우량", v):
        ideas.append("개별주는 저평가 우량주 소수만")
    if re.search(r"인버스|숏|헤지|하락\s*베팅", v):
        ideas.append("과열 섹터는 인버스/헤지로 대응 고려")
    return ideas


def _suggestions(ideas: list[str], themes: list[str], hedge: list[str]) -> list[dict]:
    s = []
    if any("ETF" in i for i in ideas) and themes:
        s.append({"title": "테마는 ETF로 묶어 개별 리스크↓",
                  "detail": f"관심 테마({', '.join(themes)})는 변동·바이너리 리스크가 커서 ETF로 분산하면 안전합니다. 특히 양자/바이오는 ETF 코어 권장.",
                  "source": "benchmark"})
    if any("액티브" in i for i in ideas):
        s.append({"title": "액티브 테마 ETF는 보수·변동 확인",
                  "detail": "ARK류 액티브 테마 ETF는 상승·하락 변동과 운용보수(약 0.75%+)가 높습니다. 비중·보수율·구성종목 중복을 점검하세요.",
                  "source": "benchmark"})
    if any("미국·한국" in i for i in ideas):
        s.append({"title": "지역 비중을 숫자로 명시",
                  "detail": "미국/한국 중심이면 비율을 정하세요(예: 미국 50 / 한국 40 / 기타 10). 환율·국가 노출이 관리됩니다.",
                  "source": "rule"})
    if hedge:
        s.append({"title": "헤지(인버스)는 롱과 분리·한도 관리",
                  "detail": f"{', '.join(hedge)}은(는) 인버스 의도 → 롱 테마에서 빼고 소전제에서 인버스 ETF로, 숏/인버스 한도(10%) 안에서 관리.",
                  "source": "rule"})
    if len(themes) >= 4:
        s.append({"title": "테마가 많음 — 섹터 쏠림 점검",
                  "detail": f"관심 테마 {len(themes)}개. 각 테마 비중이 섹터 한도(30%)를 넘지 않게, 총 tilt도 과하지 않게 분산하세요.",
                  "source": "rule"})
    return s


def analyze(account_index: int, interests: str | None = None, views: str | None = None) -> dict:
    conn = store_db.connect()
    try:
        prof = conn.execute(
            "SELECT interests_text, views_text, hedge_themes, posture_text FROM investor_profile WHERE account_index=?",
            (account_index,)).fetchone()
    finally:
        conn.close()
    interests = interests if interests is not None else (prof["interests_text"] if prof else "")
    views = views if views is not None else (prof["views_text"] if prof else "")
    hedge = [h.strip() for h in (((prof["hedge_themes"] if prof else "") or "")).split(",") if h.strip()]
    if not hedge and prof and prof["posture_text"]:
        hedge = [h.strip() for h in (profile_mod.hedge_themes(prof["posture_text"]) or "").split(",") if h.strip()]

    themes_all = [t.strip() for t in re.split(r"[,/·]", interests or "") if t.strip()]
    # 방향성은 견해에서 추출 — 자동 long 금지(CEO 지시).
    from .field_advisors import classify_direction, _DIR_LABEL
    dir_text = (views or "") + " " + ((prof["posture_text"] if prof else "") or "")
    theme_view = []
    long_themes = []
    for t in themes_all:
        notes = [ln["body"] for ln in lessons_mod.search(scope="sector", ref=t, limit=1)]
        if t in hedge:
            direction, role = "short_or_hedge_candidate", "hedge"
        else:
            direction = classify_direction(t, dir_text)["direction"]
            role = "long" if direction == "long_candidate" else ("hedge" if direction == "short_or_hedge_candidate" else "watch")
        if role == "long":
            long_themes.append(t)
        theme_view.append({"theme": t, "role": role, "direction": direction,
                           "direction_label": _DIR_LABEL.get(direction, "방향 미정"),
                           "opinion": notes[0] if notes else None})

    ideas = extract_ideas(views, interests)
    suggestions = _suggestions(ideas, long_themes, hedge)

    # AI 종합 의견 (규칙+메모리 합성, 라벨: Claude 분석). 심층은 세션에서 Claude 가 보강 가능.
    parts = []
    if ideas:
        parts.append("핵심 아이디어: " + "; ".join(ideas) + ".")
    if long_themes:
        parts.append(f"롱 관심 {len(long_themes)}개({', '.join(long_themes)})는 ETF 코어 + 소수 개별로 변동을 누르는 게 합리적.")
    if hedge:
        parts.append(f"{', '.join(hedge)}은(는) 과열 헤지로 분리해 인버스 한도 내에서만.")
    parts.append("천천히 분할 + 현금밴드 대응이면 한 테마가 흔들려도 계좌 전체 충격은 제한적.")
    ai_opinion = " ".join(parts)

    result = {"ideas": ideas, "themes": theme_view, "suggestions": suggestions, "ai_opinion": ai_opinion,
              "analyzed_at": _now()}

    conn = store_db.connect()
    try:
        conn.execute("INSERT INTO analysis_requests(account_index, kind, input, result, status, created_at) "
                     "VALUES(?,?,?,?,?,?)",
                     (account_index, "midpremise",
                      json.dumps({"interests": interests, "views": views}, ensure_ascii=False),
                      json.dumps(result, ensure_ascii=False), "done", _now()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "account_index": account_index, **result}


def latest(account_index: int) -> dict | None:
    conn = store_db.connect()
    try:
        r = conn.execute("SELECT result, created_at FROM analysis_requests WHERE account_index=? ORDER BY id DESC LIMIT 1",
                         (account_index,)).fetchone()
        if not r:
            return None
        d = json.loads(r["result"])
        d["created_at"] = r["created_at"]
        return d
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--interests")
    ap.add_argument("--views")
    args = ap.parse_args()
    try:
        if args.analyze:
            out = analyze(args.account, args.interests, args.views)
        else:
            out = {"ok": False, "error": "--analyze"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
