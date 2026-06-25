"""'Claude 분석 전문가에게 조언 구하기' — 자유 질문 → 입력 방법·권장값·메모리 근거.

팝업에서 한 줄로 물으면(예: '반도체 인버스 비중 얼마?', '양자 어떻게 넣어?', '미국/한국 비중?'),
정책 한도 + 우리 메모리(lessons) 기반으로 **어떻게 넣을지 + 권장값 + 적용 제안**을 답한다.
'그대로 적용' 가능한 제안은 apply{field,value} 로 반환 → UI 가 폼에 반영. 개선 필요하면 재질문.

지능 = Claude+메모리 (Anthropic API 미사용). 즉시 답은 규칙+메모리, 심층은 Claude 세션 보강.

  python -m main_mission.portfolio_os.consult --account 1 --ask "반도체 인버스 비중 얼마?"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from .store import db as store_db
from . import policy as policy_mod
from . import lessons as lessons_mod
from .profile import THEME_KEYWORDS, _SECT


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(h: str) -> str:
    for label, kws in THEME_KEYWORDS.items():
        if h in label or any(h in k or k in h for k in kws):
            return label
    return h


def answer(account_index: int, question: str) -> dict:
    q = question or ""
    pol = policy_mod.latest(account_index)
    policy = pol["policy"] if pol else policy_mod.compile_policy(account_index)
    L = policy.get("limits", {})
    sector_max = L.get("sector_max_pct", 30.0)
    inv = L.get("inverse_max_pct", 10.0)
    one = L.get("one_order_cap_pct", 5.0)
    single = L.get("single_name_max_pct", 20.0)
    indiv = L.get("individual_cap_pct")

    themes = list(dict.fromkeys(re.findall(_SECT, q)))
    parts: list[str] = []
    suggestions: list[dict] = []
    refs: list[dict] = []

    if re.search(r"인버스|숏|헤지|하락\s*베팅", q):
        first = themes[0] if themes else "반도체"
        sec = ", ".join(_norm(t) for t in themes) or "해당 섹터"
        parts.append(f"{sec}을(를) 인버스/헤지로 넣으려면: ① 관심 테마에서 빼고 ② 대전제 컨셉에 '{first} 인버스'처럼 쓰면 헤지로 자동 분류됩니다 ③ 인버스 한도 {inv}% 내, 보통 3~5% 보험 수준이 적절합니다.")
        suggestions.append({"label": f"컨셉에 '{first} 인버스' 의도 추가",
                            "apply": {"field": "posture_append", "value": f" {first}에 인버스도 고려."}})

    if re.search(r"비중|얼마|몇|퍼센트|%|얼만|크기", q):
        parts.append(f"한도 기준: 섹터/테마 ≤ {sector_max}%, 단일종목 ≤ {single}%, 개별주 총합 {indiv if indiv is not None else '미설정'}%, 인버스 ≤ {inv}%, 1주문 ≤ {one}%. 한 테마가 흔들려도 계좌가 버티려면 테마당 10~15%, 개별주는 3~4% 권장.")

    if re.search(r"ETF|상장지수|운용사|액티브|아크|ARK", q):
        parts.append("변동 큰 테마(양자/바이오)는 ETF로 묶어 개별 리스크를 낮추세요. 액티브 테마 ETF(ARK류)는 운용보수(~0.75%+)와 구성종목 중복을 확인하세요.")

    if re.search(r"채권|금리|장단기|듀레이션|국채", q):
        parts.append("현금만 들기보다 단기채(캐시 대용 수익)+장기채(금리 하락 베팅)를 혼합하세요. 2026년 금리 불확실 → 단기 중심 + bond ladder(1·3·5년 만기 분산). 실행은 소전제에서 국채 ETF로.")

    if re.search(r"지역|미국|한국|글로벌|전\s*세계|환율", q):
        parts.append("지역 비중을 숫자로 정하세요(예: 미국 50 / 한국 40 / 기타 10). 그래야 환율·국가 노출이 관리됩니다.")
        suggestions.append({"label": "지역 비중 적용: 미국 50 / 한국 40 / 기타 10",
                            "apply": {"field": "region_pref", "value": "미국 50 / 한국 40 / 기타 10"}})

    for th in themes:
        for ln in lessons_mod.search(scope="sector", ref=_norm(th), limit=1):
            refs.append({"theme": _norm(th), "note": ln["body"]})

    if not parts:
        parts.append("질문을 조금 더 구체적으로 적어주세요. 예: '반도체 인버스 비중 얼마?', '양자 어떻게 넣어?', '미국/한국 비중?', '현금 대신 채권?'. 입력 방법과 권장값을 안내하고, 깊은 분석은 제가(Claude) 메모리로 보강합니다.")

    ans = " ".join(parts)
    conn = store_db.connect()
    try:
        conn.execute("INSERT INTO consultations(account_index, question, answer, refs, created_at) VALUES(?,?,?,?,?)",
                     (account_index, q, ans, json.dumps(refs, ensure_ascii=False), _now()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "answer": ans, "suggestions": suggestions, "refs": refs}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--ask", metavar="QUESTION")
    args = ap.parse_args()
    try:
        out = answer(args.account, args.ask) if args.ask else {"ok": False, "error": "--ask QUESTION"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
