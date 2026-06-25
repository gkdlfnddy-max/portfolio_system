"""대전제 정리 시 **개선 제안(조언)** 도출 + 사람의 반영/보류 결정.

조언 출처(provenance):
  - rule       : profile.distill 의 규칙 기반 갭
  - benchmark  : 외부 우수사례 기반 휴리스틱 (design_v2 벤치마크)
  - lesson:<id>: 우리 agent 들이 누적한 메모리(lessons) — "성장하는 지식"
  - research   : Claude 가 세션에서 외부 조사해 lessons 로 적재한 근거

Anthropic API 미사용. 지능은 Claude+메모리. 각 제안은 사람이 반영/보류를 결정하고
그 결정은 advice_items 에 감사 저장된다(재제안 방지·추적).

  python -m main_mission.portfolio_os.advice --account 1 --generate "공격적, 현금 20~40% ..."
  python -m main_mission.portfolio_os.advice --account 1 --decide 3 accept
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


def _heuristics(sug: dict, current_cash_hint) -> list[dict]:
    """외부 우수사례(벤치마크) 기반 조언 — 추출된 전제에 따라 동적으로."""
    items = []
    region = sug.get("region_pref") or ""
    cmax = sug.get("cash_max_pct")
    indiv_cap = sug.get("individual_cap_pct")
    indiv_count = sug.get("individual_count")

    if "전세계" in region or "글로벌" in region:
        items.append({
            "title": "전세계 분산 — 지역 비중을 먼저 정하기",
            "detail": "글로벌 분산이면 미국/유럽/한국/신흥국 비중을 정해야 국가·통화 노출을 관리할 수 있습니다 (예: 미국 50 / 한국 30 / 기타 20). 지역 비중이 없으면 환율 리스크가 숨습니다.",
            "source": "benchmark", "severity": "suggest",
        })
    if indiv_count and indiv_cap:
        per = round(float(indiv_cap) / int(indiv_count), 1)
        items.append({
            "title": f"개별주 {indiv_count}종목 선정·점검 기준",
            "detail": f"개별 {indiv_cap}% 한도를 {indiv_count}종목에 나누면 종목당 약 {per}%. '저평가' 판단 지표(PER/PBR/PEG·부채비율·영업현금흐름)와 분기 실적·가이던스 체크 루틴을 미리 정해두면 흔들리지 않습니다.",
            "source": "benchmark", "severity": "suggest",
        })
    if current_cash_hint and cmax and current_cash_hint > cmax:
        items.append({
            "title": "현재 현금이 목표 상한보다 높음 — 분할 진입 필요",
            "detail": f"현재 현금 {int(current_cash_hint)}% > 목표 상한 {int(cmax)}%. 한 번에 사지 말고 며칠~주 단위 분할 매수로 점진 진입하세요(시스템이 분할 계획을 만들어 줍니다).",
            "source": "benchmark", "severity": "important",
        })
    items.append({
        "title": "하락장 방어 기준(손절·현금 확대 트리거) 정하기",
        "detail": "공격적일수록 방어선이 중요합니다. 예: 지수 -10% 시 현금 +5%p, 개별주 -20% 시 재평가. 트리거를 정하면 리스크 게이트가 검증에 반영할 수 있습니다.",
        "source": "benchmark", "severity": "suggest",
    })
    return items


def generate(account_index: int, concept: str) -> dict:
    d = profile_mod.distill(concept)
    sug = d.get("suggested", {})
    themes = [t.strip() for t in (sug.get("interests_text") or "").split(",") if t.strip()]

    raw: list[dict] = []
    # 0) 인버스/헤지 의도 감지 — 관심 테마는 '롱'으로 처리되므로 별도 안내(잘못된 롱 tilt 방지)
    if re.search(r"인버스|숏\s*(친다|간다|베팅)|하락\s*베팅|공매도", concept):
        sectors = ", ".join(set(re.findall(r"반도체|바이오|로봇|양자|2차전지|에너지|방산|미국장|한국장|코스피|나스닥", concept))) or "해당 섹터"
        raw.append({
            "title": "인버스/헤지 의도 감지 — 롱 테마와 분리 필요",
            "detail": f"{sectors}에 대한 인버스/하락 베팅 의도로 보입니다. 현재 '관심 테마'는 모두 롱(매수) 비중으로 처리됩니다. 헤지는 ① 관심 테마에서 빼고 ② 소전제(유니버스)에서 인버스 ETF로 추가해 ③ 숏/인버스 한도(기본 10%) 안에서 관리하세요. 그래야 의도와 반대로 매수되지 않습니다.",
            "source": "rule", "severity": "important",
        })
    # 1) 규칙 기반 갭
    for g in d.get("gaps", []):
        raw.append({"title": g, "detail": g, "source": "rule", "severity": "suggest"})
    # 2) 외부 우수사례 휴리스틱
    raw += _heuristics(sug, d.get("current_cash_hint"))
    # 2-b) 현금 vs 채권 구성 (금리/채권 언급 시) — 외부조사 근거
    if re.search(r"채권|금리|장단기|듀레이션|장기채|단기채|국채", concept):
        raw.append({
            "title": "현금 vs 채권 구성 — 보통은 둘 다, 장단기는 금리로",
            "detail": "현금만/채권만이 아니라 보통 둘 다 둡니다. ① 현금=비상·기회 자금(즉시성, 단 재투자위험) ② 단기채=캐시 대용 수익(듀레이션 짧아 금리 위험 작음) ③ 장기채=금리 하락 베팅(듀레이션 길수록 변동 큼). 2026년 현재 Fed 3.5~3.75%·추가 인하 기대 후퇴·10년물 4~5% 등락 → 단기채 중심으로 캐시 대용, 금리 하락 전환을 기대하면 장기채 일부. 예측이 빗나갈 때 대비 만기를 1·3·5년으로 나누는 bond ladder(사다리)가 안전합니다. 실행은 소전제(유니버스)에서 단기/장기 국채 ETF로 추가하세요. (외부조사 2026.06)",
            "source": "research", "severity": "suggest",
        })
    # 3) 우리 메모리(lessons) — 테마별 + 전제(premise)
    seen_lessons = set()
    for th in themes:
        for ln in lessons_mod.search(scope="sector", ref=th, limit=2):
            if ln["id"] in seen_lessons:
                continue
            seen_lessons.add(ln["id"])
            raw.append({"title": f"[메모리] {ln['title']}", "detail": ln["body"],
                        "source": f"lesson:{ln['id']}", "severity": "info"})
    for ln in lessons_mod.search(scope="premise", limit=3):
        if ln["id"] in seen_lessons:
            continue
        seen_lessons.add(ln["id"])
        raw.append({"title": f"[메모리] {ln['title']}", "detail": ln["body"],
                    "source": f"lesson:{ln['id']}", "severity": "info"})

    # 저장(계좌+title 중복은 기존 결정 유지). rejected 는 제외하고 반환.
    conn = store_db.connect()
    try:
        out = []
        for it in raw:
            ex = conn.execute("SELECT id, status FROM advice_items WHERE account_index=? AND title=?",
                              (account_index, it["title"])).fetchone()
            if ex:
                rec = {**it, "id": ex["id"], "status": ex["status"]}
            else:
                cur = conn.execute(
                    "INSERT INTO advice_items(account_index, title, detail, source, severity, suggested_field, "
                    "suggested_value, status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (account_index, it["title"], it["detail"], it["source"], it["severity"],
                     it.get("suggested_field"), it.get("suggested_value"), "open", _now()),
                )
                rec = {**it, "id": cur.lastrowid, "status": "open"}
            out.append(rec)
        conn.commit()
    finally:
        conn.close()

    # 중전제(관심 테마) 항목화 — 각 테마 + 역할(롱/헤지) + 메모리 분석.
    # 헤지는 '인버스/숏' 키워드 근처에 등장한 섹터만 (문장 전체 오탐 방지).
    # 헤지 판정은 profile.hedge_themes 단일 로직 사용(인버스 직전 가장 가까운 섹터)
    hedge_labels = set(s.strip() for s in (profile_mod.hedge_themes(concept) or "").split(",") if s.strip())
    # 방향성은 견해(views)+컨셉에서 추출 — 관심 테마를 자동 long 으로 두지 않는다(CEO 지시).
    from .field_advisors import classify_direction, _DIR_LABEL
    _prof = profile_mod.get(account_index) or {}
    dir_text = (_prof.get("views_text") or "") + " " + (concept or "")

    # 끊긴 고리 연결: '관심 테마별 정리'는 저장된 관심 분야(interests_text) 전체를 대상으로 한다.
    # distill 은 THEME_KEYWORDS 에 매칭되는 테마만 뽑으므로, [조사 후보로 추가]된 임의 테마
    # (예: 'AI 인프라', '단기국채')가 누락되던 끊긴 고리였다. 저장된 관심 분야를 합쳐(원문 순서 보존)
    # 모든 관심 테마가 '방향 미정'으로라도 등장하게 한다. (자동 long 없음 — direction 은 분류 결과)
    saved_interests = profile_mod._split_interests(_prof.get("interests_text") or "")
    theme_list: list[str] = list(themes)
    for th in saved_interests:
        if th not in theme_list:
            theme_list.append(th)

    themes_out = []
    for th in theme_list:
        notes = [ln["body"] for ln in lessons_mod.search(scope="sector", ref=th, limit=1)]
        if th in hedge_labels:
            direction, role = "short_or_hedge_candidate", "hedge"
        else:
            direction = classify_direction(th, dir_text)["direction"]
            role = "long" if direction == "long_candidate" else ("hedge" if direction == "short_or_hedge_candidate" else "watch")
        themes_out.append({"theme": th, "role": role, "direction": direction,
                           "direction_label": _DIR_LABEL.get(direction, "방향 미정"),
                           "notes": notes, "has_memory": bool(notes)})

    visible = [o for o in out if o["status"] != "rejected"]
    return {
        "ok": True, "account_index": account_index, "items": visible, "themes": themes_out,
        "counts": {"total": len(visible), "rule": sum(1 for o in visible if o["source"] == "rule"),
                   "benchmark": sum(1 for o in visible if o["source"] == "benchmark"),
                   "research": sum(1 for o in visible if o["source"] == "research"),
                   "lesson": sum(1 for o in visible if str(o["source"]).startswith("lesson"))},
        "note": "각 제안을 반영/보류로 결정하세요. 메모리/외부조사 근거는 Claude가 세션에서 더 채울 수 있습니다(API 아님).",
    }


def decide(advice_id: int, accept: bool) -> dict:
    conn = store_db.connect()
    try:
        conn.execute("UPDATE advice_items SET status=?, decided_at=? WHERE id=?",
                     ("accepted" if accept else "rejected", _now(), advice_id))
        conn.commit()
        return {"ok": True, "id": advice_id, "status": "accepted" if accept else "rejected"}
    finally:
        conn.close()


def listing(account_index: int) -> list:
    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT id, title, detail, source, severity, status, decided_at FROM advice_items "
            "WHERE account_index=? ORDER BY id DESC", (account_index,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--generate", metavar="CONCEPT")
    ap.add_argument("--decide", nargs=2, metavar=("ID", "ACCEPT"))
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    try:
        if args.generate is not None:
            out = generate(args.account, args.generate)
        elif args.decide:
            out = decide(int(args.decide[0]), args.decide[1].lower() in ("accept", "1", "true", "yes"))
        elif args.list:
            out = {"ok": True, "items": listing(args.account)}
        else:
            out = {"ok": False, "error": "--generate | --decide ID accept/reject | --list"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
