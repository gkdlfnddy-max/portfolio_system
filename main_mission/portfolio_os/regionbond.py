"""지역/채권 비중 — 자연어 파싱 · 정규화 · 검증.

상담/컨셉의 '미국 50 / 한국 40 / 기타 10', '채권 10%, 단기채 위주' 같은 텍스트를
**구조화된 정책 값**으로 변환한다. 자동 보정하지 않고, 합계 오류는 경고만.

현금과 채권은 **둘 다 방어자산이나 역할이 다르다** — 뭉개지 않는다.
  - 현금: 즉시 매수 여력
  - 채권: 안정자산 / 금리·경기 대응 (듀레이션 short|intermediate|long|mixed)

CEO 방침(불변): 채권은 **국채만**(government_only). 회사채/하이일드/신흥국채/복잡채권 금지.
비국채 의도가 텍스트에 보이면 validate 에서 차단(위반 목록에 올림).
"""
from __future__ import annotations

import re

# 허용되는 유일한 채권 유형 — 국채만. (확장 시에도 CEO 승인 필요)
BOND_ALLOWED_DEFAULT = "government_only"

# 비국채(차단 대상) 키워드 — 회사채/하이일드/신흥국채/복잡상품.
_NON_GOV_BOND = [
    (r"회사채|크레딧|크레디트|credit|corporate", "회사채/크레딧"),
    (r"하이일드|정크|투기등급|high\s*yield|junk", "하이일드/정크"),
    (r"신흥국\s*채권|이머징\s*채권|em\s*bond|emerging.*bond", "신흥국채"),
    (r"전환사채|cb\b|메자닌|mezzanine|구조화\s*채권|abs\b|mbs\b|코코본드|coco\b|영구채|후순위채",
     "복잡/구조화 채권"),
]


def detect_non_government_bonds(text: str) -> list[str]:
    """텍스트에서 비국채(국채 외) 채권 의도 라벨을 추출 — government_only 위반 후보."""
    t = text or ""
    found: list[str] = []
    for pat, label in _NON_GOV_BOND:
        if re.search(pat, t, re.I) and label not in found:
            found.append(label)
    return found

_REGION_PATTERNS = [
    (r"미국|미장|나스닥|s&p|에스앤피|usa|us\b", "미국"),
    (r"한국|국내|코스피|코스닥", "한국"),
    (r"유럽|선진국?|developed", "선진(유럽 등)"),
    (r"신흥국?|이머징|emerging|em\b", "신흥국"),
    (r"중국|china", "중국"),
    (r"일본|japan", "일본"),
    (r"글로벌|전\s*세계|기타|나머지|그\s*외|other|global", "기타/글로벌"),
]


def norm_region(name: str) -> str:
    for pat, label in _REGION_PATTERNS:
        if re.search(pat, name, re.I):
            return label
    return name.strip()


def parse_region(text: str) -> dict:
    """'미국 50 / 한국 40 / 기타 10' → {targets, total, warnings}. 합계 자동보정 안 함."""
    t = text or ""
    targets: dict[str, int] = {}
    for m in re.finditer(
        r"(미국|미장|한국|국내|코스피|코스닥|유럽|선진국?|신흥국?|이머징|중국|일본|글로벌|전\s*세계|기타|나머지)\s*[:=]?\s*(\d{1,3})\s*%?",
        t,
    ):
        lab = norm_region(m.group(1))
        targets[lab] = targets.get(lab, 0) + int(m.group(2))
    total = sum(targets.values())
    warnings = []
    if targets and total != 100:
        warnings.append(f"지역 비중 합계 {total}% (100% 아님) — 자동 보정하지 않으니 확인 필요")
    return {"targets": targets, "total": total, "warnings": warnings}


def parse_bond(text: str) -> dict:
    """'채권 10%', '단기채 위주', '장기채 제한' → {bond_target_pct, duration_pref, allowed_types, notes}."""
    t = text or ""
    bt = None
    m = re.search(r"채권[^0-9]{0,10}(\d{1,2})\s*%", t)
    if m:
        bt = float(m.group(1))
    dur = None
    if re.search(r"장단기|혼합|사다리|ladder", t):
        dur = "mixed"
    elif re.search(r"장기채|장기\s*채권|듀레이션\s*(길|확대)", t):
        dur = "long"
    elif re.search(r"단기채|단기\s*채권", t):
        dur = "short"
    elif re.search(r"중기채|중기", t):
        dur = "intermediate"
    notes = []
    if re.search(r"금리\s*(상승|오르|불확실)", t) and dur in (None, "long"):
        notes.append("금리 상승/불확실 — 장기채 듀레이션 제한 권장(short/intermediate 우선)")
    # 국채 외 채권 의도 감지 — CEO 방침상 차단 대상(노트로 명시).
    non_gov = detect_non_government_bonds(t)
    for label in non_gov:
        notes.append(f"'{label}' 의도 감지 — CEO 방침상 채권은 국채만(government_only) 허용. 반영 안 함.")
    return {"bond_target_pct": bt, "duration_pref": dur,
            "allowed_types": BOND_ALLOWED_DEFAULT, "non_government": non_gov, "notes": notes}


def validate(region_targets: dict | None, bond_target_pct: float | None,
             cash_band: dict | None, max_single_country: float = 70.0,
             emerging_max: float = 20.0, bond_allowed_types: str | None = None,
             bond_intent_text: str | None = None) -> list[dict]:
    """지역/채권 구조 검증 — risk gate 가 쓰는 위반 목록.

    bond_allowed_types: 허용 채권 유형(기본 government_only). government_only 가 아니면 차단.
    bond_intent_text: 자유입력 원문 — 비국채(회사채/하이일드/신흥국채/복잡상품) 의도면 차단.
    """
    v = []
    # --- 채권은 국채만(government_only) ---
    allowed = (bond_allowed_types or BOND_ALLOWED_DEFAULT).strip().lower()
    if allowed != BOND_ALLOWED_DEFAULT:
        v.append({"limit": "bond_allowed_types", "observed": allowed, "threshold": BOND_ALLOWED_DEFAULT,
                  "detail": f"허용 채권 유형이 '{allowed}' — CEO 방침상 국채만(government_only) 허용"})
    for label in detect_non_government_bonds(bond_intent_text or ""):
        v.append({"limit": "non_government_bond", "observed": label, "threshold": BOND_ALLOWED_DEFAULT,
                  "detail": f"비국채 의도 '{label}' 차단 — 채권은 국채만 허용(회사채/하이일드/신흥국채/복잡상품 금지)"})
    rt = region_targets or {}
    if rt:
        total = sum(rt.values())
        if total != 100:
            v.append({"limit": "region_sum", "observed": total, "threshold": 100, "detail": "지역 비중 합계가 100%가 아님"})
        for reg, w in rt.items():
            if w > max_single_country:
                v.append({"limit": "max_single_country_pct", "observed": w, "threshold": max_single_country,
                          "detail": f"'{reg}' 국가 집중 {w}% > 한도 {max_single_country}%"})
        if rt.get("신흥국", 0) > emerging_max:
            v.append({"limit": "emerging_market_max_pct", "observed": rt["신흥국"], "threshold": emerging_max,
                      "detail": "신흥국 비중 한도 초과"})
    if bond_target_pct is not None:
        # CEO 방침: bond_target_pct 는 **방어자산 중 국채 비율(0~100)**.
        # 방어 안에서만 배분되므로 100%를 초과할 수 없다(현금 상한 대비 total-% 비교 아님).
        if bond_target_pct > 100:
            v.append({"limit": "cash_bond_conflict", "observed": bond_target_pct, "threshold": 100,
                      "detail": "국채 비중이 방어자산의 100%를 초과(방어 안에서만 배분)"})
    return v
