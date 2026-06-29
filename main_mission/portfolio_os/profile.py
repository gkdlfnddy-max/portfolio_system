"""투자 프로필 (대전제 운용방식 + 중전제 관심/생각) 저장·조회 — 계좌별.

종목(소전제) 이전 단계. 자유입력을 받아 DB 에 저장하고, Claude(메모리 에이전트)가
되물어 구조화한다. 쓰기는 백엔드만, 웹은 조회.

  python -m main_mission.portfolio_os.profile --account 1 --get
  python -m main_mission.portfolio_os.profile --account 1 --json '{"posture_text":"..."}'
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from .store import db as store_db


# 테마 키워드/헤지 정규화 — **단일 원본 config/portfolio/themes.json** 에서 로드(하드코딩 금지).
from . import configs as _cfg
THEME_KEYWORDS = _cfg.load("themes")["keywords"]


# 섹터 + **지수/시장**(인버스로 숏 가능한 대상). 한국장·미국장도 지수 헤지로 인식.
_SECT = r"반도체|바이오|로봇|양자|2차전지|에너지|방산|코스피200|코스피|코스닥|나스닥|S&P|한국\s*장|미국\s*장"
# 지수/시장 라벨 정규화 — 인버스 ETF 단위로(설정 파일).
_HEDGE_NORM = _cfg.load("themes")["hedge_norm"]


def hedge_themes(text: str) -> str:
    """인버스/숏 의도 키워드 앞 **창(40자) 안의 모든 섹터·지수**를 헤지 테마로 추출(라벨 정규화).
    구두점 무관 — '…반도체… 한국장이나 미국장에 인버스', '코스피…숏'처럼 여러 대상도 함께 포착."""
    t = text or ""
    found = set()
    for m in re.finditer(r"인버스|숏|하락\s*베팅|공매도", t):
        before = t[max(0, m.start() - 40):m.start()]
        for sec in re.findall(_SECT, before):
            norm = _HEDGE_NORM.get(sec.replace(" ", ""), sec.replace(" ", ""))
            found.add(norm)
    labels = []
    for h in found:
        matched = None
        for label, kws in THEME_KEYWORDS.items():
            if h in label or any(h in k or k in h for k in kws):
                matched = label
                break
        labels.append(matched or h)
    return ", ".join(sorted(set(labels)))


_IDEA_TAGS = [
    ("레버리지", r"레버리지|2\s*배|3\s*배|1\s*배로|전환"),
    ("숏/헤지", r"숏|인버스|공매도|하락\s*베팅"),
    ("채권/금리", r"채권|금리|장단기|듀레이션|국채|장기채|단기채"),
    ("현금운용", r"현금"),
    ("밸류", r"저평가|밸류|가치주|싸"),
    ("시황", r"코스피|코스닥|나스닥|s&p|반도체|상승|하락|대출|자본|지표|버블|과열"),
]


def _extract_ideas(t: str) -> list[dict]:
    """고정 스키마에 안 들어가는 **자유 전략 아이디어/의견**을 절(clause) 단위로 보존(형식 강요 X).
    투자엔 정답이 없으므로 다양한 주제를 그대로 담는다 — 규칙은 태깅만, 판단은 Claude+메모리."""
    clauses = re.split(r"[.!?\n·]+|\.\.+|\s그리고\s|\s또\s|~|…", t)
    seen: set = set()
    ideas: list[dict] = []
    for c in clauses:
        s = c.strip(" ,·-")
        if len(s) < 6 or s in seen:
            continue
        tags = [name for name, pat in _IDEA_TAGS if re.search(pat, s, re.I)]
        opinionated = re.search(r"생각|같아|싶|나쁘지\s*않|괜찮|좋|전략|베팅|보여|보이|것도|하면|들고", s)
        if tags or opinionated:
            seen.add(s)
            ideas.append({"text": s, "tags": tags})
        if len(ideas) >= 12:
            break
    return ideas


def distill(text: str) -> dict:
    """컨셉(자유 입력)에서 대전제+중전제 신호를 추린다 — 규칙 기반 1차 정리(즉시).
    키워드/현금/관심/개별주/지역/속도 추출 + 보완 제안(gaps).
    깊은 다듬기는 Claude+메모리(세션)에서. 결과는 편집 가능한 제안일 뿐."""
    # 전각/물결 변형 정규화 — '20～40'(전각 ～), '20〜40' 등도 범위로 인식(사용자 명시값 정확 적용).
    t = (text or "").replace("～", "~").replace("〜", "~").replace("∼", "~").replace("－", "-").replace("ー", "-")

    risk = ""
    if re.search(r"공격|적극|공세|성장|레버리지", t):
        risk = "aggressive"
    elif re.search(r"방어|보수|안전|안정|지키", t):
        risk = "defensive"
    elif re.search(r"중립|균형", t):
        risk = "neutral"

    short = ""
    if re.search(r"숏|인버스", t):
        if re.search(r"숏\s*(은|는)?\s*(안|없|미사용|하지\s*않|제외)", t):
            short = "none"
        elif re.search(r"숏.*적극|적극.*숏", t):
            short = "active"
        else:
            short = "insurance"
    elif re.search(r"숏\s*(안|없|하지\s*않)", t):
        short = "none"

    cmin = cmax = None
    m = re.search(r"현금[^0-9]{0,8}(\d{1,2})\s*%?\s*[~\-–]\s*(\d{1,2})", t)
    if m:
        cmin, cmax = float(m.group(1)), float(m.group(2))
    else:
        m2 = re.search(r"현금\s*밴드[^0-9]{0,6}(\d{1,2})", t)
        if m2:
            cmin = cmax = float(m2.group(1))

    cur_cash = None
    mcur = re.search(r"(지금|현재)[^0-9]{0,10}(\d{1,3})\s*%", t)
    if mcur:
        cur_cash = float(mcur.group(2))

    horizon = ""
    mh = re.search(r"(\d+\s*[~\-–]?\s*\d*\s*년|장기|단기|중기)", t)
    if mh:
        horizon = mh.group(1).strip()

    themes = [label for label, kws in THEME_KEYWORDS.items() if any(k in t for k in kws)]
    interests = ", ".join(themes)

    indiv_cap = None
    mic = re.search(r"(개별|개인)\s*종목[^0-9]{0,15}(\d{1,2})\s*%", t)
    if mic:
        indiv_cap = float(mic.group(2))
    else:
        mic2 = re.search(r"(개별|개인)[^0-9]{0,12}(\d{1,2})\s*%", t)
        if mic2:
            indiv_cap = float(mic2.group(2))

    indiv_count = None
    mcnt = re.search(r"(\d{1,2})\s*개", t)
    if mcnt and re.search(r"개별|개인|종목", t):
        indiv_count = int(mcnt.group(1))

    region = ""
    if re.search(r"전\s*세계|글로벌|전세계", t):
        region = "전세계 분산"
    elif re.search(r"미국", t):
        region = "미국 중심"
    elif re.search(r"국내|한국|코스피|코스닥", t):
        region = "국내 중심"

    pace = ""
    if re.search(r"빠르게\s*변경.{0,10}(아니|않|안\s*[함해하])|천천히|느리게|장기로\s*천천|자주\s*(안|않|아니)", t):
        pace = "slow"
    elif re.search(r"자주|빈번|단타|빠르게\s*(사고|팔|회전)", t):
        pace = "fast"
    elif risk:
        pace = "normal"

    # 정리된 키워드 (사람이 한눈에)
    rk = {"aggressive": "공격적", "neutral": "중립", "defensive": "방어적"}
    sk = {"none": "숏 안 함", "insurance": "숏 보험수준", "active": "숏 적극"}
    pk = {"slow": "천천히 조정", "normal": "보통 속도", "fast": "빠른 조정"}
    keywords = []
    if risk:
        keywords.append(rk[risk])
    if short:
        keywords.append(sk[short])
    if cmin is not None:
        keywords.append(f"현금밴드 {int(cmin)}~{int(cmax)}%")
    if cur_cash is not None:
        keywords.append(f"현재현금 {int(cur_cash)}%")
    if horizon:
        keywords.append(f"기간 {horizon}")
    if interests:
        keywords.append(f"관심 {interests}")
    if region:
        keywords.append(region)
    if indiv_cap is not None:
        keywords.append(f"개별주 한도 {int(indiv_cap)}%")
    if indiv_count is not None:
        keywords.append(f"개별 {indiv_count}종목")
    if pace:
        keywords.append(pk[pace])

    bond_intent = bool(re.search(r"채권|금리|장단기|듀레이션|장기채|단기채|국채", t))
    if bond_intent:
        keywords.append("현금·채권 구성 검토")

    # 보완 제안 (부족한·발전시킬 점) — 규칙 기반 기본. 깊은 코칭은 Claude.
    gaps = []
    if not risk:
        gaps.append("투자 성향(공격/중립/방어)이 분명치 않음")
    if cmin is None:
        gaps.append("현금 밴드(평소 유지할 현금 범위)가 명시 안 됨")
    if cur_cash is not None and cmax is not None and cur_cash > cmax:
        gaps.append(f"현재 현금 {int(cur_cash)}%가 목표밴드 상한({int(cmax)}%)보다 높음 → 분할 매수로 점진 진입 계획 필요")
    if region == "전세계 분산":
        gaps.append("전세계 비중을 지역(미국/유럽/한국 등)으로 어떻게 나눌지 미정")
    if indiv_cap is not None and indiv_count is None:
        gaps.append("개별주 총합 한도는 정했지만 몇 종목으로 나눌지 미정")
    if indiv_count is not None and not interests:
        gaps.append("개별 종목 후보(소전제)를 아직 안 정함")
    if not re.search(r"손절|손실|하락장|방어\s*트리거|드로우|drawdown", t):
        gaps.append("하락장 방어 기준(손절·현금 확대 트리거)이 없음")
    if not re.search(r"섹터.{0,4}(한도|상한|최대|집중)", t):
        gaps.append("섹터 집중 상한 미명시(기본 30% 적용 중) — 관심 테마가 많으면 쏠림 주의")

    return {
        "ok": True,
        "suggested": {
            "risk_tolerance": risk, "short_policy": short,
            "cash_min_pct": cmin, "cash_max_pct": cmax, "horizon": horizon,
            "interests_text": interests, "individual_cap_pct": indiv_cap,
            "individual_count": indiv_count, "region_pref": region, "rebalance_pace": pace,
            "hedge_themes": hedge_themes(t),
        },
        "keywords": keywords,
        "gaps": gaps,
        "ideas": _extract_ideas(t),   # 고정 스키마 밖 자유 아이디어/의견(형식 강요 X)
        "current_cash_hint": cur_cash,
        "note": "규칙 기반 1차 정리입니다(Anthropic API 미사용). 값은 직접 수정 가능하고, Claude가 메모리로 더 다듬으며 성장합니다.",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except Exception:
        return None


def _norm_bond_split(raw, duration_pref) -> dict | None:
    """mixed 듀레이션 분할 정규화 → {short, long} 합 100.

    - duration_pref 가 mixed 가 아니면 split 무의미 → None.
    - raw 미지정/파싱 실패 → 기본 단기50/장기50.
    - 합이 100 이 아니면 비율 유지하며 100 으로 정규화(자동 보정).
    """
    if (duration_pref or "").strip().lower() != "mixed":
        return None
    short = long = None
    if isinstance(raw, dict):
        short, long = _num(raw.get("short")), _num(raw.get("long"))
    elif isinstance(raw, str) and raw.strip():
        try:
            d = json.loads(raw)
            short, long = _num(d.get("short")), _num(d.get("long"))
        except (ValueError, TypeError, AttributeError):
            short = long = None
    if short is None or long is None:
        return {"short": 50.0, "long": 50.0}          # 기본 단기50/장기50
    short, long = max(0.0, short), max(0.0, long)
    tot = short + long
    if tot <= 0:
        return {"short": 50.0, "long": 50.0}
    return {"short": round(short * 100.0 / tot, 1), "long": round(long * 100.0 / tot, 1)}


def get(account_index: int) -> dict | None:
    conn = store_db.connect()
    try:
        r = conn.execute("SELECT * FROM investor_profile WHERE account_index=?", (account_index,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def save(account_index: int, data: dict) -> dict:
    from . import regionbond
    reg = regionbond.parse_region(data.get("region_pref") or "")
    bsrc = (data.get("posture_text") or "") + " " + (data.get("views_text") or "")
    bond = regionbond.parse_bond(bsrc)
    bond_target = (_num(data.get("bond_target_pct")) if str(data.get("bond_target_pct") or "").strip() != ""
                   else bond["bond_target_pct"])
    bond_dur = data.get("bond_duration_pref") or bond["duration_pref"]
    # 채권 허용 유형 — CEO 방침상 국채만(government_only) 고정 기본. 비국채는 무시(저장 안 함).
    bond_allowed = (data.get("bond_allowed_types") or regionbond.BOND_ALLOWED_DEFAULT)
    if str(bond_allowed).strip().lower() != regionbond.BOND_ALLOWED_DEFAULT:
        bond_allowed = regionbond.BOND_ALLOWED_DEFAULT  # 비국채 요청은 강제로 국채만으로 복귀
    # duration_split — mixed 일 때만 의미. 기본 {short:50, long:50}, 사용자 변경 시 합100 검증.
    bond_split = _norm_bond_split(data.get("bond_duration_split"), bond_dur)
    bond_split_json = json.dumps(bond_split, ensure_ascii=False) if bond_split else None
    region_targets_json = json.dumps(reg["targets"], ensure_ascii=False) if reg["targets"] else None
    warnings = reg["warnings"] + bond["notes"]
    if bond.get("non_government"):
        warnings.append("국채 외 채권 의도는 반영하지 않았습니다(government_only).")

    conn = store_db.connect()
    try:
        conn.execute(
            "INSERT INTO investor_profile(account_index, posture_text, risk_tolerance, short_policy, "
            "cash_min_pct, cash_max_pct, horizon, interests_text, views_text, "
            "individual_cap_pct, individual_count, region_pref, rebalance_pace, doc, hedge_themes, "
            "region_targets, bond_target_pct, bond_duration_pref, "
            "bond_allowed_types, bond_duration_split, "
            "policy_type, user_overrides_json, disabled_rules_json, refined_by, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_index) DO UPDATE SET posture_text=excluded.posture_text, "
            "risk_tolerance=excluded.risk_tolerance, short_policy=excluded.short_policy, "
            "cash_min_pct=excluded.cash_min_pct, cash_max_pct=excluded.cash_max_pct, horizon=excluded.horizon, "
            "interests_text=excluded.interests_text, views_text=excluded.views_text, "
            "individual_cap_pct=excluded.individual_cap_pct, individual_count=excluded.individual_count, "
            "region_pref=excluded.region_pref, rebalance_pace=excluded.rebalance_pace, doc=excluded.doc, "
            "hedge_themes=excluded.hedge_themes, region_targets=excluded.region_targets, "
            "bond_target_pct=excluded.bond_target_pct, bond_duration_pref=excluded.bond_duration_pref, "
            "bond_allowed_types=excluded.bond_allowed_types, bond_duration_split=excluded.bond_duration_split, "
            "policy_type=excluded.policy_type, user_overrides_json=excluded.user_overrides_json, "
            "disabled_rules_json=excluded.disabled_rules_json, "
            "refined_by=excluded.refined_by, updated_at=excluded.updated_at",
            (account_index,
             (data.get("posture_text") or None),
             (data.get("risk_tolerance") or None),
             (data.get("short_policy") or None),
             _num(data.get("cash_min_pct")),
             _num(data.get("cash_max_pct")),
             (data.get("horizon") or None),
             (data.get("interests_text") or None),
             (data.get("views_text") or None),
             _num(data.get("individual_cap_pct")),
             (int(data["individual_count"]) if str(data.get("individual_count") or "").strip().isdigit() else None),
             (data.get("region_pref") or None),
             (data.get("rebalance_pace") or None),
             (json.dumps(data["doc"], ensure_ascii=False) if isinstance(data.get("doc"), (dict, list))
              else (data.get("doc") or None)),
             (data.get("hedge_themes") if data.get("hedge_themes") is not None
              else (hedge_themes(data.get("posture_text") or "") or None)),
             region_targets_json, bond_target, bond_dur,
             bond_allowed, bond_split_json,
             (data.get("policy_type") or None),
             (json.dumps(data["user_overrides"], ensure_ascii=False) if isinstance(data.get("user_overrides"), (dict, list))
              else (data.get("user_overrides_json") or None)),
             (json.dumps(data["disabled_rules"], ensure_ascii=False) if isinstance(data.get("disabled_rules"), (dict, list))
              else (data.get("disabled_rules_json") or None)),
             (data.get("refined_by") or "user"),
             _now()),
        )
        # 변경 이력 적재 (진화하는 전제 버전 추적)
        saved = get_in_tx(conn, account_index)
        conn.execute(
            "INSERT INTO investor_profile_history(account_index, snapshot, source, created_at) VALUES(?,?,?,?)",
            (account_index, json.dumps(saved, ensure_ascii=False), (data.get("refined_by") or "user"), _now()),
        )
        conn.commit()
        return {"ok": True, "account_index": account_index, "version_saved": True, "warnings": warnings,
                "region_targets": reg["targets"], "bond_target_pct": bond_target}
    finally:
        conn.close()


def get_in_tx(conn, account_index: int) -> dict | None:
    r = conn.execute("SELECT * FROM investor_profile WHERE account_index=?", (account_index,)).fetchone()
    return dict(r) if r else None


def _split_interests(text: str) -> list[str]:
    """interests_text 를 항목 리스트로 분해(구분자 , / · 모두 허용, 공백 정리)."""
    raw = (text or "").replace("/", ",").replace("·", ",")
    out: list[str] = []
    for s in raw.split(","):
        t = s.strip()
        if t and t not in out:
            out.append(t)
    return out


def add_interest(account_index: int, theme: str, *, source: str = "research_candidate") -> dict:
    """관심 분야(interests_text)에 테마를 **중복 없이** 추가한다 — neutral(방향 미정).

    조사 후보로 추가된 테마가 관심 분야 + 관심 테마별 정리에 '방향 미정'으로 등장하게
    하는 끊긴 고리의 연결점. **자동 long/policy/주문 반영은 일절 없다** — 단지 관심 목록에
    올려 사용자가 방향을 정할 수 있게 할 뿐(방향 미지정 시 allocation 미반영).
    """
    theme = (theme or "").strip()
    if not theme:
        return {"ok": False, "error": "빈 테마는 추가할 수 없습니다."}

    prof = get(account_index) or {}
    items = _split_interests(prof.get("interests_text") or "")
    # 정규화 비교(키워드 라벨 동일시 중복으로 간주) — 표시는 원문 유지.
    have_norm = {_norm_for_interest(t) for t in items}
    if _norm_for_interest(theme) in have_norm:
        return {"ok": True, "account_index": account_index, "theme": theme,
                "added": False, "interests_text": ", ".join(items),
                "note": "이미 관심 분야에 있습니다(중복 추가 안 함)."}

    items.append(theme)
    new_text = ", ".join(items)
    # 다른 컬럼은 그대로 유지하면서 interests_text 만 갱신해 재저장(버전 이력도 남김).
    payload = dict(prof)
    payload["interests_text"] = new_text
    payload["refined_by"] = source
    # save() 가 처리하는 파생/JSON 필드 충돌 방지: 원본 doc/override 문자열은 그대로 통과.
    save(account_index, payload)
    return {"ok": True, "account_index": account_index, "theme": theme,
            "added": True, "interests_text": new_text}


def _norm_for_interest(h: str) -> str:
    """관심 분야 중복 판정용 정규화 — THEME_KEYWORDS 라벨로 묶고, 없으면 소문자/공백 제거."""
    h = (h or "").strip()
    for label, kws in THEME_KEYWORDS.items():
        if h == label or any(h == k for k in kws):
            return label
    return h.lower().replace(" ", "")


def history(account_index: int, limit: int = 20) -> list:
    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT id, snapshot, source, created_at FROM investor_profile_history "
            "WHERE account_index=? ORDER BY id DESC LIMIT ?", (account_index, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int)
    ap.add_argument("--get", action="store_true")
    ap.add_argument("--json", metavar="PAYLOAD")
    ap.add_argument("--distill", metavar="TEXT")  # 계좌 불필요 (순수 텍스트→대전제)
    ap.add_argument("--history", action="store_true")
    args = ap.parse_args()
    try:
        if args.distill is not None:
            out = distill(args.distill)
        elif args.history and args.account:
            out = {"ok": True, "history": history(args.account)}
        elif args.get and args.account:
            out = {"ok": True, "profile": get(args.account)}
        elif args.json and args.account:
            out = save(args.account, json.loads(args.json))
        else:
            out = {"ok": False, "error": "--distill, 또는 --account 와 함께 --get/--json"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
