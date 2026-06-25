"""성장 학습 — 축/신호별 과거 예측 적중 이력(track record).

루프: 징후 발화(예측) → 이후 실제 하락 여부 → 적중/미스를 growth/lessons 에 기록 →
다음 composite 가중에 반영. **쓸수록 가중이 정교화(성장).**

저장: 기존 `lessons` 시스템 재사용(새 API 호출 0).
  - scope='axis', ref=<axis 이름>.
  - **적중/미스를 별개 후보 title 로** 누적 → 같은 (scope,ref,title) 은 observed_count++ (반복성).
    적중 title = "축 적중 — {axis}", 미스 title = "축 미스 — {axis}".
  - candidate → (반복 ≥2 + 근거/결과 + confidence) → promoted (lessons.promote 기준 그대로).
  - reliability 는 candidate(누적 중) + 승격 lesson 모두에서 observed_count 를 읽어 합산.

reliability(축): 적중률(hits/(hits+misses)) 를 베이지안 평활(prior, 약한 표본 보정).
  데이터/이력 없으면 중립 0.5 — 단정 회피(정직).

⚠️ 적중/미스 기록은 **실현 결과**(실제 하락 발생/미발생)를 외부(백테스트/사후관찰)가 줄 때만.
   mock 으로 가짜 이력을 쌓아 '성장 완료' 보고 금지(CLAUDE.md §11.8).
"""
from __future__ import annotations

from .. import lessons as lessons_mod
from ..store import db as store_db

SCOPE = "axis"
AGENT = "decline-analyst"

# 베이지안 평활 — 표본 적을 때 0.5 로 끌어당김(약한 이력에 과신 금지)
_PRIOR_HITS = 1.0
_PRIOR_MISSES = 1.0


def _hit_title(axis: str) -> str:
    return f"축 적중 — {axis}"


def _miss_title(axis: str) -> str:
    return f"축 미스 — {axis}"


def _count(conn, axis: str, title: str) -> int:
    """candidate(누적 중) + 승격 lesson 의 observed_count 합. (적중/미스 횟수 = 관찰 횟수)"""
    total = 0
    c = conn.execute(
        "SELECT IFNULL(SUM(observed_count),0) n FROM lesson_candidates "
        "WHERE scope=? AND ref=? AND title=?", (SCOPE, axis, title)).fetchone()
    total += int(c["n"] or 0)
    # 승격된 lesson 은 observed_count 컬럼이 없으므로 행 수로(1건=최소 MIN_OBSERVED 이상이나 보수적 1).
    l = conn.execute(
        "SELECT COUNT(*) n FROM lessons WHERE scope=? AND ref=? AND title=? "
        "AND IFNULL(status,'active')!='archived'", (SCOPE, axis, title)).fetchone()
    # 승격 시 candidate status='promoted' 가 되어 위 SUM 에서 빠지지 않으므로(상태 무관 SUM),
    # 이중계산 방지: 승격 lesson 은 별도로 더하지 않는다(candidate SUM 이 진실 카운트).
    _ = l
    return total


def reliability(axis: str) -> dict:
    """축의 과거 예측 신뢰도(적중률). lesson_candidates(scope='axis', ref=axis) 누적 집계.

    반환: {axis, hits, misses, samples, reliability(0~1), source}
      이력 없으면 reliability=0.5(중립), source='no_track_record'(정직).
    """
    conn = store_db.connect()
    try:
        hits = _count(conn, axis, _hit_title(axis))
        misses = _count(conn, axis, _miss_title(axis))
    finally:
        conn.close()
    samples = hits + misses
    if samples == 0:
        return {"axis": axis, "hits": 0, "misses": 0, "samples": 0,
                "reliability": 0.5, "source": "no_track_record"}
    rel = (hits + _PRIOR_HITS) / (hits + misses + _PRIOR_HITS + _PRIOR_MISSES)
    return {"axis": axis, "hits": hits, "misses": misses, "samples": samples,
            "reliability": round(rel, 3), "source": "track_record"}


def reliabilities(axes: list[str]) -> dict[str, dict]:
    return {a: reliability(a) for a in axes}


# ============================================================
# 일반 scope reliability (instrument / sector) — axis 와 동일 산식, scope 만 분리.
# scope='instrument' (종목 공통 노하우), scope='sector' (섹터 공통). **계좌 교차적용 아님.**
# ============================================================
def reliability_scoped(scope: str, ref: str) -> dict:
    """임의 scope(instrument/sector)의 예측 신뢰도. axis 와 같은 베이지안 평활.

    반환: {scope, ref, hits, misses, samples, reliability, source}
    """
    conn = store_db.connect()
    try:
        hits = _count_scoped(conn, scope, ref, _hit_title(ref))
        misses = _count_scoped(conn, scope, ref, _miss_title(ref))
    finally:
        conn.close()
    samples = hits + misses
    if samples == 0:
        return {"scope": scope, "ref": ref, "hits": 0, "misses": 0, "samples": 0,
                "reliability": 0.5, "source": "no_track_record"}
    rel = (hits + _PRIOR_HITS) / (hits + misses + _PRIOR_HITS + _PRIOR_MISSES)
    return {"scope": scope, "ref": ref, "hits": hits, "misses": misses,
            "samples": samples, "reliability": round(rel, 3), "source": "track_record"}


def _count_scoped(conn, scope: str, ref: str, title: str) -> int:
    c = conn.execute(
        "SELECT IFNULL(SUM(observed_count),0) n FROM lesson_candidates "
        "WHERE scope=? AND ref=? AND title=?", (scope, ref, title)).fetchone()
    return int(c["n"] or 0)


def record_outcome_scoped(scope: str, ref: str, *, predicted_decline: bool,
                          actual_decline: bool, ref_note: str | None = None,
                          confidence: float = 0.6, agent: str = AGENT) -> dict:
    """scope(instrument/sector) 단위 예측 vs 실현 결과 1건 기록 → lessons 후보 누적.

    axis record_outcome 과 동일 의미(예측=하락일 때만 채점). scope 만 분리.
    ⚠️ actual_decline 은 **실현 결과**(분석일 이후 일봉)여야 한다. mock 금지.
    """
    if not predicted_decline:
        return {"ok": False, "reason": "no_prediction_to_score"}
    hit = bool(actual_decline)
    title = _hit_title(ref) if hit else _miss_title(ref)
    body = (f"{scope}:{ref} 예측 {'적중' if hit else '미스'}. "
            + (ref_note or "실현 결과 기반(분석일 이후 일봉)."))
    res = lessons_mod.add_candidate(
        scope=scope, ref=ref, title=title, body=body,
        outcome=("hit" if hit else "miss"),
        confidence=confidence, source="decline_track_record", agent=agent,
    )
    return {"ok": True, "scope": scope, "ref": ref,
            "outcome": "hit" if hit else "miss", "candidate": res}


def record_outcome(axis: str, *, predicted_decline: bool, actual_decline: bool,
                   ref_note: str | None = None, confidence: float = 0.6) -> dict:
    """예측 vs 실현 결과 1건 기록 → lessons 후보 누적(성장).

    적중(hit): 예측=하락 & 실제=하락. 미스(miss): 예측=하락 & 실제=비하락(거짓경보).
    예측 자체가 비하락이면(징후 미발화) track record 에 기록하지 않음(true negative 제외 —
    이 엔진은 '발화 신뢰도'를 본다).

    ⚠️ actual_decline 은 **실현 결과**여야 한다(백테스트/사후관찰). mock 금지.
    """
    if not predicted_decline:
        return {"ok": False, "reason": "no_prediction_to_score",
                "note": "징후 미발화 — track record 미기록(발화 신뢰도만 누적)"}

    hit = bool(actual_decline)
    title = _hit_title(axis) if hit else _miss_title(axis)
    body = (f"{axis} 축 예측 {'적중' if hit else '미스'}. "
            + (ref_note or "실현 결과 기반(백테스트/사후관찰)."))
    res = lessons_mod.add_candidate(
        scope=SCOPE, ref=axis, title=title, body=body,
        outcome=("hit" if hit else "miss"),
        confidence=confidence, source="decline_track_record", agent=AGENT,
    )
    return {"ok": True, "axis": axis, "outcome": "hit" if hit else "miss",
            "candidate": res}
