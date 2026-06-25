"""Evidence 엔진 (O#10) — 외부 자료(뉴스/공시/리포트)를 근거로 적재·링크·회수.

본질 원칙 (불변):
  **외부 자료를 바로 매수/매도로 확정하지 않는다.** evidence 는 *입장(stance)* 을
  태깅한 근거일 뿐, 실제 비중 변경/주문 판단은 항상 호출측(사람 승인 흐름)이 한다.

stance 허용값 (이 중 하나):
  long_support | short_support | hedge_support | risk_warning |
  watch_only | insufficient_evidence | conflicting_evidence

freshness 기반 confidence decay:
  유효 confidence = base_confidence * 0.5 ** (age_days / HALF_LIFE_DAYS)
  (HALF_LIFE_DAYS=90 — 90일 지나면 절반, 오래된 근거는 자동 후순위)

저장: 기존 `evidence_documents` 테이블 사용 (스키마 불변).
  - 핵심 필드는 컬럼에 매핑(scope=source_type, ref=topic, affected_theme=theme ...).
  - 컬럼에 없는 구조화 필드(stance/account_index/publisher/key_claims/risk_points/
    base_confidence/freshness_at)는 body 안 JSON 봉투(__evidence__)에 담는다.
링크: theme_advice / decision / daily_review 별 link 테이블(없으면 idempotent 생성).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .store import db as store_db

HALF_LIFE_DAYS = 90.0

VALID_STANCES = {
    "long_support", "short_support", "hedge_support", "risk_warning",
    "watch_only", "insufficient_evidence", "conflicting_evidence",
}

# kind → (link 테이블, ref 컬럼)
_LINK_TABLES = {
    "theme_advice": ("theme_advice_evidence_links", "advice_id"),
    "decision": ("decision_evidence_links", "decision_id"),
    "daily_review": ("daily_review_evidence_links", "review_id"),
}

# 봉투 키 — evidence_documents.body 에 JSON 으로 동봉(스키마 불변 유지).
_ENVELOPE_KEY = "__evidence__"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s)
        elif " " in s:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        else:
            dt = datetime.strptime(s, "%Y-%m-%d")  # date-only (예: source_date)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        return None


def _ensure_link_tables(conn) -> None:
    """link 테이블 멱등 보장 (decision_evidence_links 는 schema.sql 에 이미 존재)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS theme_advice_evidence_links ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, advice_id INTEGER NOT NULL, "
        "evidence_id INTEGER NOT NULL, note TEXT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_review_evidence_links ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, review_id INTEGER NOT NULL, "
        "evidence_id INTEGER NOT NULL, note TEXT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )


def _decode_body(body: str | None) -> tuple[str, dict]:
    """body 에서 사람용 summary 와 구조화 봉투를 분리."""
    if not body:
        return "", {}
    try:
        obj = json.loads(body)
        if isinstance(obj, dict) and _ENVELOPE_KEY in obj:
            env = obj.get(_ENVELOPE_KEY) or {}
            return obj.get("summary", "") or "", env if isinstance(env, dict) else {}
    except (ValueError, TypeError):
        pass
    return body, {}


def add_evidence(source_type: str, *, theme: str | None = None, topic: str | None = None,
                 summary: str = "", stance: str = "insufficient_evidence",
                 source_url: str | None = None, source_title: str | None = None,
                 publisher: str | None = None, published_at: str | None = None,
                 confidence: float = 0.5, key_claims=None, risk_points=None,
                 account_index: int | None = None, conn=None) -> int:
    """근거 문서 1건 적재 → evidence_id.

    stance 는 VALID_STANCES 중 하나(외부 자료를 바로 매수/매도 확정하지 않음 — 입장 태깅만).
    freshness_at = now (수집 시점, decay 기준). published_at 은 별도 보존.
    """
    if stance not in VALID_STANCES:
        raise ValueError(f"invalid stance {stance!r}; one of {sorted(VALID_STANCES)}")
    now = _now()
    envelope = {
        "stance": stance,
        "account_index": account_index,
        "publisher": publisher,
        "published_at": published_at,
        "base_confidence": float(confidence),
        "freshness_at": now,
        "key_claims": list(key_claims) if isinstance(key_claims, (list, tuple)) else key_claims,
        "risk_points": list(risk_points) if isinstance(risk_points, (list, tuple)) else risk_points,
    }
    body = json.dumps({"summary": summary, _ENVELOPE_KEY: envelope}, ensure_ascii=False)
    own = conn is None
    conn = conn or store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO evidence_documents(scope, ref, source_type, title, body, url, "
            "freshness, confidence, affected_theme, affected_asset, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (source_type, topic, source_type, source_title, body, source_url,
             now, float(confidence), theme, topic, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        if own:
            conn.close()


def link_evidence(evidence_id: int, kind: str, ref_id: int, *, note: str | None = None,
                  conn=None) -> dict:
    """evidence 를 theme_advice|decision|daily_review 산출물에 링크."""
    if kind not in _LINK_TABLES:
        raise ValueError(f"invalid kind {kind!r}; one of {sorted(_LINK_TABLES)}")
    table, ref_col = _LINK_TABLES[kind]
    own = conn is None
    conn = conn or store_db.connect()
    try:
        _ensure_link_tables(conn)
        cur = conn.execute(
            f"INSERT INTO {table}({ref_col}, evidence_id, note, created_at) "
            "VALUES(?,?,?, datetime('now'))",
            (ref_id, evidence_id, note),
        )
        conn.commit()
        return {"ok": True, "link_id": int(cur.lastrowid), "table": table, "kind": kind}
    finally:
        if own:
            conn.close()


def decayed_confidence(base: float, freshness_at: str | None,
                       now: datetime | None = None) -> float:
    """freshness 기반 confidence decay: base * 0.5 ** (age_days/HALF_LIFE_DAYS)."""
    now = now or datetime.now(timezone.utc)
    ts = _parse_ts(freshness_at)
    if ts is None:
        return round(float(base or 0.0), 4)
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return round(float(base or 0.0) * (0.5 ** (age_days / HALF_LIFE_DAYS)), 4)


def recall_evidence(theme: str | None = None, stance: str | None = None,
                    max_age_days: float | None = None, *, account_index: int | None = None,
                    limit: int = 20, conn=None) -> list[dict]:
    """근거 회수 — freshness decay 적용 eff_confidence 부여, stance 보존, 계좌 격리.

    account_index 지정 시: 같은 계좌 + 계좌 무관(None) 근거만(다른 계좌는 격리/제외).
    eff_confidence 내림차순 정렬. max_age_days 초과 근거는 제외.
    """
    own = conn is None
    conn = conn or store_db.connect()
    try:
        sql = ("SELECT id, scope, ref, source_type, title, body, url, freshness, confidence, "
               "affected_theme, affected_asset, created_at FROM evidence_documents WHERE 1=1")
        args: list = []
        if theme:
            sql += " AND (affected_theme=? OR ref=?)"; args += [theme, theme]
        rows = conn.execute(sql, args).fetchall()
        now = datetime.now(timezone.utc)
        out: list[dict] = []
        for r in rows:
            summary, env = _decode_body(r["body"])
            ev_stance = env.get("stance") or "insufficient_evidence"
            ev_account = env.get("account_index")
            # 계좌 격리: 다른 계좌의 근거는 제외(계좌무관 None 은 공통).
            if account_index is not None and ev_account is not None and ev_account != account_index:
                continue
            if stance and ev_stance != stance:
                continue
            base = env.get("base_confidence")
            if base is None:
                base = r["confidence"]
            freshness_at = env.get("freshness_at") or r["freshness"] or r["created_at"]
            ts = _parse_ts(freshness_at)
            age_days = None if ts is None else max(0.0, (now - ts).total_seconds() / 86400.0)
            if max_age_days is not None and age_days is not None and age_days > max_age_days:
                continue
            out.append({
                "id": r["id"],
                "source_type": r["source_type"],
                "theme": r["affected_theme"],
                "topic": r["ref"],
                "summary": summary,
                "stance": ev_stance,
                "source_title": r["title"],
                "source_url": r["url"],
                "publisher": env.get("publisher"),
                "published_at": env.get("published_at"),
                "account_index": ev_account,
                "base_confidence": round(float(base or 0.0), 4),
                "eff_confidence": decayed_confidence(base, freshness_at, now),
                "age_days": None if age_days is None else round(age_days, 2),
                "key_claims": env.get("key_claims"),
                "risk_points": env.get("risk_points"),
                "freshness_at": freshness_at,
            })
        out.sort(key=lambda d: d["eff_confidence"], reverse=True)
        return out[:limit]
    finally:
        if own:
            conn.close()
