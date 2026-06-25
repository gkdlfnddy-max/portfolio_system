"""통합 개인화 루프 (Track A) — 사용자 선택/무시/수정 → 다음 조언 ranking 반영.

CEO 원칙(불변):
  - **계좌별 격리(교차 금지)**: 모든 함수는 account_index 필수. 한 계좌의 선호는
    절대 다른 계좌에 반영되지 않는다 (UNIQUE(account_index, scope, key)).
  - **공통 agent memory(lessons) 와 분리**: personalization_weights 는 *그 계좌
    사용자 개인 선호*만 담는다. 전문가 공통 노하우(agent lesson)는 별도 시스템.
  - **자동 주문/자동 policy 0**: 본 모듈은 *후보의 표시 순서(ranking)* 만 바꾼다.
    long/적용/주문 결정은 절대 하지 않는다. 가중치는 정렬에만 쓰인다.
  - **지능 = 규칙(베이지안 평활) 뿐. Anthropic / LLM API 미사용 (CLAUDE.md §17).**

동작:
  record_feedback(account, scope, key, action, reason=None):
      action ∈ {accepted, ignored, modified} 카운트++ → weight 재계산(upsert).
  weight_for(account, scope, key) -> float: 가중치 조회(미존재=중립 1.0).
  rank(account, scope, items) -> list: items 를 개인화 가중 적용해 정렬(표시순서만).

weight 산식 (베이지안 평활):
  pref = (accepted + ignored 의 반대) 를 평활해 점수화.
  - 선택(accepted) 多 → weight > 1 (상향)
  - 무시(ignored) 多 → weight < 1 (하향)
  - 수정(modified) 은 약한 긍정(관심은 있으나 그대로는 아님) — 작게 가산.
  - 표본이 적으면 1.0 근처(평활) — 한두 번으로 과격하게 바뀌지 않음.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .store import db as store_db

# 허용 scope (스키마 주석과 동일) — perspective|theme|advice_type|candidate_type|hedge.
VALID_SCOPES = {"perspective", "theme", "advice_type", "candidate_type", "hedge"}
VALID_ACTIONS = {"accepted", "ignored", "modified"}

# 베이지안 평활 파라미터 (한두 표본으로 과격 변동 금지).
_PRIOR = 2.0          # 가상 선험 표본(평활 강도) — 클수록 1.0 으로 천천히 이동
_SPAN = 0.9           # weight 진폭(1±_SPAN 범위로 매핑)
_MODIFIED_W = 0.4     # 수정은 약한 긍정(관심 있음). accepted 대비 가중 비율
_WEIGHT_MIN = 0.1
_WEIGHT_MAX = 2.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_weight(accepted: int, ignored: int, modified: int) -> float:
    """카운트 → weight (>1 선호 / <1 비선호). 베이지안 평활.

    긍정 신호 = accepted + modified*_MODIFIED_W (수정은 약한 긍정).
    부정 신호 = ignored.
    score = (pos + prior/2) / (pos + neg + prior)  ∈ (0,1), 표본 적으면 0.5 근처.
    weight = 1 + (score - 0.5) * 2 * _SPAN  → score 0.5 면 1.0(중립).
    """
    pos = float(accepted) + float(modified) * _MODIFIED_W
    neg = float(ignored)
    total = pos + neg
    score = (pos + _PRIOR / 2.0) / (total + _PRIOR)  # (0,1)
    weight = 1.0 + (score - 0.5) * 2.0 * _SPAN
    return round(max(_WEIGHT_MIN, min(_WEIGHT_MAX, weight)), 4)


def record_feedback(account_index: int | None, scope: str, key: str,
                    action: str, reason: str | None = None) -> dict:
    """사용자 피드백 1건 기록 → personalization_weights upsert(카운트++·weight 재계산).

    **계좌 격리 필수**: account_index 없으면 hard-block. 타 계좌 미반영.
    **자동 주문/policy 0**: 가중치만 갱신 — 어떤 적용도 하지 않음.
    """
    if account_index is None:
        return {"ok": False, "error": "account_index 없음 — 개인화는 계좌 귀속이 필수입니다(hard-block).",
                "gate": "block"}
    if scope not in VALID_SCOPES:
        return {"ok": False, "error": f"잘못된 scope: {scope}; 허용 {sorted(VALID_SCOPES)}"}
    if action not in VALID_ACTIONS:
        return {"ok": False, "error": f"잘못된 action: {action}; 허용 {sorted(VALID_ACTIONS)}"}
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "key 가 비어 있습니다"}

    col = {"accepted": "accepted_count", "ignored": "ignored_count",
           "modified": "modified_count"}[action]

    conn = store_db.connect()
    try:
        row = conn.execute(
            "SELECT accepted_count, ignored_count, modified_count FROM personalization_weights "
            "WHERE account_index=? AND scope=? AND key=?",
            (account_index, scope, key),
        ).fetchone()
        acc = int(row["accepted_count"]) if row else 0
        ign = int(row["ignored_count"]) if row else 0
        mod = int(row["modified_count"]) if row else 0
        if action == "accepted":
            acc += 1
        elif action == "ignored":
            ign += 1
        else:
            mod += 1
        weight = compute_weight(acc, ign, mod)
        now = _now()
        if row is None:
            conn.execute(
                "INSERT INTO personalization_weights("
                "account_index, scope, key, accepted_count, ignored_count, modified_count, "
                "last_reason, weight, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (account_index, scope, key, acc, ign, mod, reason, weight, now),
            )
        else:
            conn.execute(
                f"UPDATE personalization_weights SET {col}={col}+1, last_reason=?, "
                "weight=?, updated_at=? WHERE account_index=? AND scope=? AND key=?",
                (reason, weight, now, account_index, scope, key),
            )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "account_index": account_index, "scope": scope, "key": key,
            "action": action, "accepted_count": acc, "ignored_count": ign,
            "modified_count": mod, "weight": weight, "last_reason": reason}


def weight_for(account_index: int | None, scope: str, key: str) -> float:
    """(account, scope, key) 가중치 조회. 미존재/계좌 없음 = 중립 1.0.

    **계좌 격리**: account_index 로만 조회 — 타 계좌 가중치는 절대 반환 안 함.
    """
    if account_index is None or not key:
        return 1.0
    conn = store_db.connect()
    try:
        row = conn.execute(
            "SELECT weight FROM personalization_weights WHERE account_index=? AND scope=? AND key=?",
            (account_index, scope, (key or "").strip()),
        ).fetchone()
    finally:
        conn.close()
    return float(row["weight"]) if row else 1.0


def weights_map(account_index: int | None, scope: str) -> dict[str, float]:
    """해당 계좌·scope 의 모든 key→weight (rank() 배치 조회용 — 계좌 격리)."""
    if account_index is None:
        return {}
    conn = store_db.connect()
    try:
        rows = conn.execute(
            "SELECT key, weight FROM personalization_weights WHERE account_index=? AND scope=?",
            (account_index, scope),
        ).fetchall()
    finally:
        conn.close()
    return {r["key"]: float(r["weight"]) for r in rows}


def _item_key(item, key_field: str):
    if isinstance(item, dict):
        return item.get(key_field)
    return getattr(item, key_field, None)


def _base_score(item) -> float:
    """items 의 기존 점수(있으면) — confidence/score/weight 순. 없으면 1.0(중립)."""
    if isinstance(item, dict):
        for f in ("confidence", "score", "base_score"):
            if f in item and item[f] is not None:
                try:
                    return float(item[f])
                except (TypeError, ValueError):
                    pass
    return 1.0


def rank(account_index: int | None, scope: str, items: list,
         *, key_field: str = "key") -> list:
    """items 를 개인화 가중 적용해 **표시 순서만** 정렬(내림차순).

    - 각 item 의 key(key_field) 로 계좌별 weight 를 곱해 personalized_score 산출.
    - 반복 무시된 key(weight<1) → 하향, 선호 key(weight>1) → 상향.
    - 안정 정렬(stable): 가중치 동일하면 원래 순서 유지.
    - **자동 long/적용/주문 아님** — 반환은 정렬된 동일 items 일 뿐.

    items 각 원소는 dict 또는 객체. dict 에는 personalized_score/personalization_weight
    필드를 비파괴적으로 추가(복사). 객체는 (item, score) 판단 없이 순서만 바꿈.
    """
    if not items:
        return list(items)
    wmap = weights_map(account_index, scope)

    enriched = []
    for idx, item in enumerate(items):
        k = _item_key(item, key_field)
        w = wmap.get(k, 1.0) if k is not None else 1.0
        base = _base_score(item)
        pscore = base * w
        if isinstance(item, dict):
            out = dict(item)
            out["personalization_weight"] = round(w, 4)
            out["personalized_score"] = round(pscore, 6)
            enriched.append((pscore, idx, out))
        else:
            enriched.append((pscore, idx, item))

    # 내림차순(점수 큰 것 먼저), 동점은 원래 순서(idx) 유지 → stable.
    enriched.sort(key=lambda t: (-t[0], t[1]))
    return [e[2] for e in enriched]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="통합 개인화 루프 — 피드백 기록/가중 조회(표시순서만, API 미사용)")
    ap.add_argument("--account", type=int, help="계좌 인덱스 (필수 — 없으면 hard-block)")
    ap.add_argument("--record", action="store_true", help="피드백 기록")
    ap.add_argument("--scope", help=f"scope {sorted(VALID_SCOPES)}")
    ap.add_argument("--key", help="대상 key (예: 'C', '반도체')")
    ap.add_argument("--action", help=f"action {sorted(VALID_ACTIONS)}")
    ap.add_argument("--reason", help="무시/수정 이유(선택)")
    ap.add_argument("--weight", action="store_true", help="가중치 조회")
    args = ap.parse_args()

    try:
        if args.record:
            out = record_feedback(args.account, args.scope or "", args.key or "",
                                  args.action or "", reason=args.reason)
        elif args.weight:
            w = weight_for(args.account, args.scope or "", args.key or "")
            out = {"ok": True, "account_index": args.account, "scope": args.scope,
                   "key": args.key, "weight": w}
        else:
            out = {"ok": False, "error": "--record 또는 --weight 중 하나 필요"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
