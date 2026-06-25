"""성장 메모리 — **lesson 후보 / 승격 lesson 분리**.

원칙: `lessons` 는 아무 로그나 쌓는 곳이 아니다.
관찰은 `lesson_candidates` 에 모으고, **승격 기준**을 충족할 때만 `lessons` 로 올린다.

승격 기준 (모두 충족):
  - 반복성: observed_count >= 2
  - 근거 또는 결과: evidence_ref 있음 OR outcome 있음
  - 확신: confidence >= 0.6

outdated lesson 은 confidence decay / archive (decay()).

  python -m main_mission.portfolio_os.lessons --promote
  python -m main_mission.portfolio_os.lessons --list
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .store import db as store_db

MIN_OBSERVED = 2
MIN_CONFIDENCE = 0.6

# confidence decay: 마지막 참조 후 HALF_LIFE_DAYS 마다 절반. ARCHIVE_BELOW 미만이고
# MIN_AGE_DAYS 초과면 archive(삭제 아님 — status만 변경, append-only 존중).
HALF_LIFE_DAYS = 90.0
ARCHIVE_BELOW = 0.15
MIN_AGE_DAYS = 30.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # ISO8601 (tz 有) 또는 'YYYY-MM-DD HH:MM:SS'(datetime('now'), UTC).
        if "T" in s:
            return datetime.fromisoformat(s)
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_days(row, now: datetime) -> float:
    ts = _parse_ts(row["last_seen_at"] if "last_seen_at" in row.keys() else None) or _parse_ts(row["created_at"])
    if ts is None:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts).total_seconds() / 86400.0)


def decayed_confidence(row, now: datetime | None = None) -> float:
    """저장 confidence를 freshness로 감쇠한 유효 confidence (0~base)."""
    now = now or datetime.now(timezone.utc)
    base = float(row["confidence"] or 0.0)
    return round(base * (0.5 ** (_age_days(row, now) / HALF_LIFE_DAYS)), 4)


def add_candidate(scope: str, title: str, body: str, *, ref: str | None = None,
                  account_index: int | None = None, evidence_ref: str | None = None,
                  outcome: str | None = None, confidence: float = 0.0, source: str = "claude_agent",
                  agent: str | None = None) -> dict:
    """같은 (scope, ref, title) 이면 관찰 횟수만 증가(반복성 누적), 아니면 새 후보.
    posthook이 호출 — 즉시 lessons로 가지 않고 후보로만 모은다(승격은 promote())."""
    conn = store_db.connect()
    try:
        now = _now()
        ex = conn.execute(
            "SELECT id, observed_count FROM lesson_candidates WHERE scope=? AND IFNULL(ref,'')=IFNULL(?,'') AND title=? AND status='candidate'",
            (scope, ref, title),
        ).fetchone()
        if ex:
            conn.execute(
                "UPDATE lesson_candidates SET observed_count=observed_count+1, body=?, evidence_ref=COALESCE(?,evidence_ref), "
                "outcome=COALESCE(?,outcome), confidence=MAX(confidence,?), agent=COALESCE(?,agent), last_seen_at=?, updated_at=? WHERE id=?",
                (body, evidence_ref, outcome, confidence, agent, now, now, ex["id"]),
            )
            cid = ex["id"]
        else:
            cur = conn.execute(
                "INSERT INTO lesson_candidates(account_index, scope, ref, title, body, evidence_ref, outcome, confidence, source, agent, last_seen_at, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (account_index, scope, ref, title, body, evidence_ref, outcome, confidence, source, agent, now, now, now),
            )
            cid = cur.lastrowid
        conn.commit()
        return {"ok": True, "candidate_id": cid}
    finally:
        conn.close()


def _eligible(row) -> bool:
    return (row["observed_count"] >= MIN_OBSERVED
            and (row["confidence"] or 0) >= MIN_CONFIDENCE
            and (bool(row["evidence_ref"]) or bool(row["outcome"])))


def promote() -> dict:
    """승격 기준 충족 후보를 lessons 로 승격."""
    conn = store_db.connect()
    promoted = []
    try:
        rows = conn.execute("SELECT * FROM lesson_candidates WHERE status='candidate'").fetchall()
        for r in rows:
            if not _eligible(r):
                continue
            conn.execute(
                "INSERT INTO lessons(account_index, scope, ref, title, body, confidence, source, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (r["account_index"], r["scope"], r["ref"], r["title"], r["body"], r["confidence"], "promoted", _now()),
            )
            conn.execute("UPDATE lesson_candidates SET status='promoted', updated_at=? WHERE id=?", (_now(), r["id"]))
            promoted.append({"id": r["id"], "scope": r["scope"], "ref": r["ref"], "title": r["title"]})
        conn.commit()
        return {"ok": True, "promoted_count": len(promoted), "promoted": promoted}
    finally:
        conn.close()


def search(scope: str | None = None, ref: str | None = None, limit: int = 20,
           agent: str | None = None) -> list:
    """다음 decision 에서 검색 가능 — 단, 자동 반영이 아니라 참고.
    archived 제외. 결과에 decay-가중 confidence(`eff_confidence`) 부여해 정렬."""
    conn = store_db.connect()
    try:
        sql = ("SELECT id, scope, ref, title, body, confidence, source, agent, "
               "last_seen_at, created_at FROM lessons WHERE IFNULL(status,'active')!='archived'")
        args: list = []
        if scope:
            sql += " AND scope=?"; args.append(scope)
        if ref:
            sql += " AND ref=?"; args.append(ref)
        if agent:
            sql += " AND IFNULL(agent,?)=?"; args.append(agent); args.append(agent)
        rows = conn.execute(sql, args).fetchall()
        now = datetime.now(timezone.utc)
        out = []
        for r in rows:
            d = dict(r)
            d["eff_confidence"] = decayed_confidence(r, now)
            out.append(d)
        out.sort(key=lambda d: (d["eff_confidence"], d["id"]), reverse=True)
        return out[:limit]
    finally:
        conn.close()


def touch(lesson_ids: list[int]) -> int:
    """참조된 lesson의 freshness 갱신(last_seen_at=now) — decay 시계 리셋."""
    if not lesson_ids:
        return 0
    conn = store_db.connect()
    try:
        now = _now()
        conn.executemany("UPDATE lessons SET last_seen_at=? WHERE id=?", [(now, i) for i in lesson_ids])
        conn.commit()
        return len(lesson_ids)
    finally:
        conn.close()


def decay(half_life_days: float = HALF_LIFE_DAYS, archive_below: float = ARCHIVE_BELOW,
          min_age_days: float = MIN_AGE_DAYS) -> dict:
    """outdated lesson 정리: 유효 confidence가 archive_below 미만이고 min_age_days 초과면
    status='archived' (삭제 아님). 다음 prehook 검색에서 제외된다."""
    conn = store_db.connect()
    archived = []
    try:
        now = datetime.now(timezone.utc)
        rows = conn.execute(
            "SELECT id, confidence, last_seen_at, created_at FROM lessons WHERE IFNULL(status,'active')!='archived'"
        ).fetchall()
        for r in rows:
            if _age_days(r, now) >= min_age_days and decayed_confidence(r, now) < archive_below:
                conn.execute("UPDATE lessons SET status='archived' WHERE id=?", (r["id"],))
                archived.append(r["id"])
        conn.commit()
        return {"ok": True, "archived_count": len(archived), "archived": archived,
                "criteria": {"half_life_days": half_life_days, "archive_below": archive_below, "min_age_days": min_age_days}}
    finally:
        conn.close()


def overview() -> dict:
    conn = store_db.connect()
    try:
        cand = conn.execute("SELECT status, COUNT(*) c FROM lesson_candidates GROUP BY status").fetchall()
        less = conn.execute("SELECT COUNT(*) c FROM lessons").fetchone()
        return {
            "ok": True,
            "candidates": {r["status"]: r["c"] for r in cand},
            "lessons": less["c"],
            "criteria": {"min_observed": MIN_OBSERVED, "min_confidence": MIN_CONFIDENCE, "needs_evidence_or_outcome": True},
        }
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--promote", action="store_true")
    ap.add_argument("--decay", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.promote:
        out = promote()
    elif args.decay:
        out = decay()
    elif args.list:
        out = {"ok": True, "overview": overview(), "lessons": search()}
    else:
        out = overview()
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
