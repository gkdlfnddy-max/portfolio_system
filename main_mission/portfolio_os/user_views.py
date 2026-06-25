"""사용자(CEO) 투자 견해/통찰 — **1급 입력**. 계좌별 격리(교차적용 금지).

사용자가 자기 생각을 넣는다("반도체 장기 긍정·단기 고점 같다", "바이오는 ETF로만",
"양자는 관찰만", "로봇 장기 조금"). 이게 Portfolio OS 판단의 1급 입력이 된다.

핵심 원칙(불변):
- 견해는 데이터보다 **무조건 우위도, 무시도 아님.** 시스템은 견해 vs 데이터 **일치/충돌을 설명**한다.
- **자동 적용 금지.** 견해는 저장만 한다. allocation/policy *draft* 에만 반영되고,
  실제 반영은 Agent3 의 advice_items 미승인 게이트(사람 승인)를 거친다.
- 대전제(grand)/중전제(mid)/단기(short)/장기(long) **계층 분리.**
- 견해 변경 시 이전 것 status=superseded + superseded_by 로 **이력 보존**.
- 지능 = Claude+메모리 (Anthropic API 미사용).

테이블(이미 생성됨, 스키마 편집 금지): user_views
  (id, account_index, layer, theme, ticker, etf, stance, conviction, horizon,
   note, status, superseded_by, created_at, updated_at)

  python -m main_mission.portfolio_os.user_views --account 1 --list
  python -m main_mission.portfolio_os.user_views --account 1 --add --layer long --theme 반도체 --stance positive --conviction 0.7 --note "장기 긍정"
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .store import db as store_db

# ── 허용 enum (스키마 주석 SSOT) ──
LAYERS = ("grand", "mid", "short", "long")          # 대전제/중전제/단기/장기
STANCES = ("positive", "neutral", "negative", "observe")  # 긍정/중립/부정/관찰만
HORIZONS = ("short", "mid", "long")
STATUSES = ("active", "superseded", "archived")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _acct(account_index) -> int:
    n = int(account_index)
    if n < 1:
        raise ValueError("account_index 는 1 이상이어야 합니다")
    return n


def _norm_enum(value, allowed, field) -> str | None:
    if value is None:
        return None
    v = str(value).strip().lower()
    if not v:
        return None
    if v not in allowed:
        raise ValueError(f"{field} 는 {allowed} 중 하나여야 합니다 (받음: {value!r})")
    return v


def _norm_conviction(value) -> float | None:
    if value is None or value == "":
        return None
    f = float(value)
    if not (0.0 <= f <= 1.0):
        raise ValueError("conviction 은 0~1 범위여야 합니다")
    return f


def _clean(value) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    return v or None


def _row(r) -> dict:
    return {
        "id": r["id"],
        "account_index": r["account_index"],
        "layer": r["layer"],
        "theme": r["theme"],
        "ticker": r["ticker"],
        "etf": r["etf"],
        "stance": r["stance"],
        "conviction": r["conviction"],   # user_conviction (0~1)
        "horizon": r["horizon"],
        "note": r["note"],
        "status": r["status"],
        "superseded_by": r["superseded_by"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


# ────────────────────────────── CRUD ──────────────────────────────

def add(account_index: int, *, layer: str, theme=None, ticker=None, etf=None,
        stance=None, conviction=None, horizon=None, note=None) -> dict:
    """새 견해 1건 저장 (계좌별). layer 필수. 자동 적용 없음 — 저장만."""
    acct = _acct(account_index)
    lyr = _norm_enum(layer, LAYERS, "layer")
    if lyr is None:
        raise ValueError("layer 는 필수입니다 (grand|mid|short|long)")
    st = _norm_enum(stance, STANCES, "stance")
    hz = _norm_enum(horizon, HORIZONS, "horizon")
    conv = _norm_conviction(conviction)
    now = _now()
    conn = store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO user_views(account_index, layer, theme, ticker, etf, stance, "
            "conviction, horizon, note, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?, 'active', ?, ?)",
            (acct, lyr, _clean(theme), _clean(ticker), _clean(etf), st,
             conv, hz, _clean(note), now, now),
        )
        conn.commit()
        vid = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM user_views WHERE id=?", (vid,)).fetchone()
        return {"ok": True, "view": _row(row)}
    finally:
        conn.close()


def list_views(account_index: int, *, status: str = "active", layer=None,
               include_superseded: bool = False) -> list[dict]:
    """계좌별 견해 목록. 기본 active 만. include_superseded=True 면 이력 포함."""
    acct = _acct(account_index)
    sql = "SELECT * FROM user_views WHERE account_index=?"
    args: list = [acct]
    if not include_superseded:
        if status and status != "all":
            sql += " AND status=?"
            args.append(_norm_enum(status, STATUSES, "status"))
    if layer:
        sql += " AND layer=?"
        args.append(_norm_enum(layer, LAYERS, "layer"))
    sql += " ORDER BY id DESC"
    conn = store_db.connect()
    try:
        rows = conn.execute(sql, tuple(args)).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def by_layer(account_index: int) -> dict:
    """active 견해를 layer(대전제/중전제/단기/장기)별로 묶어 반환 — 계층 분리 노출."""
    out: dict[str, list[dict]] = {k: [] for k in LAYERS}
    for v in list_views(account_index, status="active"):
        out.setdefault(v["layer"], []).append(v)
    return out


def get(account_index: int, view_id: int) -> dict | None:
    """단건 조회 (계좌 격리 — 다른 계좌 id 는 None)."""
    acct = _acct(account_index)
    conn = store_db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM user_views WHERE id=? AND account_index=?",
            (int(view_id), acct),
        ).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def update(account_index: int, view_id: int, **fields) -> dict:
    """견해 변경 = **이력 보존 supersede**.

    이전 견해는 status='superseded' + superseded_by=<새 id> 로 두고,
    변경 필드를 반영한 **새 active 행**을 만든다. (덮어쓰기 아님 — 추적 가능.)
    """
    acct = _acct(account_index)
    old = get(acct, view_id)
    if not old:
        return {"ok": False, "error": "견해를 찾을 수 없습니다(계좌 격리)"}
    if old["status"] != "active":
        return {"ok": False, "error": f"active 견해만 변경할 수 있습니다(현재 {old['status']})"}

    # 변경 필드 머지 (미지정은 기존값 유지)
    merged = {
        "layer": fields.get("layer", old["layer"]),
        "theme": fields.get("theme", old["theme"]),
        "ticker": fields.get("ticker", old["ticker"]),
        "etf": fields.get("etf", old["etf"]),
        "stance": fields.get("stance", old["stance"]),
        "conviction": fields.get("conviction", old["conviction"]),
        "horizon": fields.get("horizon", old["horizon"]),
        "note": fields.get("note", old["note"]),
    }
    lyr = _norm_enum(merged["layer"], LAYERS, "layer")
    st = _norm_enum(merged["stance"], STANCES, "stance")
    hz = _norm_enum(merged["horizon"], HORIZONS, "horizon")
    conv = _norm_conviction(merged["conviction"])
    now = _now()
    conn = store_db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO user_views(account_index, layer, theme, ticker, etf, stance, "
            "conviction, horizon, note, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?, 'active', ?, ?)",
            (acct, lyr, _clean(merged["theme"]), _clean(merged["ticker"]),
             _clean(merged["etf"]), st, conv, hz, _clean(merged["note"]), now, now),
        )
        new_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE user_views SET status='superseded', superseded_by=?, updated_at=? "
            "WHERE id=? AND account_index=?",
            (new_id, now, int(view_id), acct),
        )
        conn.commit()
        new_row = conn.execute("SELECT * FROM user_views WHERE id=?", (new_id,)).fetchone()
        return {"ok": True, "view": _row(new_row), "superseded_id": int(view_id)}
    finally:
        conn.close()


def archive(account_index: int, view_id: int) -> dict:
    """견해 보관(archive) — 더 이상 유효하지 않은 견해. 이력은 남는다."""
    acct = _acct(account_index)
    old = get(acct, view_id)
    if not old:
        return {"ok": False, "error": "견해를 찾을 수 없습니다(계좌 격리)"}
    conn = store_db.connect()
    try:
        conn.execute(
            "UPDATE user_views SET status='archived', updated_at=? WHERE id=? AND account_index=?",
            (_now(), int(view_id), acct),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM user_views WHERE id=?", (int(view_id),)).fetchone()
        return {"ok": True, "view": _row(row)}
    finally:
        conn.close()


def history(account_index: int, view_id: int) -> list[dict]:
    """한 견해의 supersede 체인(과거→현재)을 추적해 반환."""
    acct = _acct(account_index)
    chain: list[dict] = []
    seen: set[int] = set()
    cur = get(acct, view_id)
    while cur and cur["id"] not in seen:
        chain.append(cur)
        seen.add(cur["id"])
        nxt = cur.get("superseded_by")
        cur = get(acct, nxt) if nxt else None
    return chain


# ─────────────────────── 견해 vs 데이터 비교 ───────────────────────

# 사용자 stance → 방향성 부호 (양수=상승/긍정 기대, 음수=하락/부정 기대, None=방향 없음)
_STANCE_DIR = {"positive": 1, "negative": -1, "neutral": 0, "observe": None}


def _signal_direction(data_signal: dict) -> tuple[int | None, str]:
    """데이터 신호 → 방향 부호 + 사람이 읽을 요약.

    data_signal 예(decline_signals.compute_signals 호환):
      {"risk_level": "high", "risk_score": 72, ...}  → 하락 위험↑ → 부호 -1
    또는 명시적:
      {"direction": "up"|"down"|"flat"}  /  {"bias": +1|-1|0}
    """
    if not isinstance(data_signal, dict):
        return None, "데이터 신호 없음"

    # 1) 명시적 bias/direction 우선
    if "bias" in data_signal and data_signal["bias"] is not None:
        b = int(data_signal["bias"])
        return (1 if b > 0 else -1 if b < 0 else 0), f"데이터 bias={b}"
    d = (data_signal.get("direction") or "").lower()
    if d in ("up", "positive", "bullish"):
        return 1, "데이터: 상승 신호"
    if d in ("down", "negative", "bearish"):
        return -1, "데이터: 하락 신호"
    if d in ("flat", "neutral"):
        return 0, "데이터: 중립"

    # 2) decline risk (하락 위험) → 위험 높을수록 하락 쪽(-1)
    lvl = (data_signal.get("risk_level") or "").lower()
    score = data_signal.get("risk_score")
    if lvl in ("high", "severe") or (isinstance(score, (int, float)) and score >= 60):
        return -1, f"데이터: 하락 위험↑ (risk_level={lvl or '-'}, score={score})"
    if lvl in ("low",) or (isinstance(score, (int, float)) and score < 30):
        return 1, f"데이터: 하락 위험 낮음 (risk_level={lvl or '-'}, score={score})"
    if lvl == "elevated" or (isinstance(score, (int, float)) and 30 <= score < 60):
        return 0, f"데이터: 하락 위험 보통 (risk_level={lvl or '-'}, score={score})"

    return None, "데이터 신호 방향 불명확"


def compare_view_vs_data(account_index: int, *, ticker=None, theme=None,
                         data_signal: dict) -> dict:
    """사용자 견해(active) vs 데이터 신호 비교 → {result, ...}.

    정직 원칙: 견해는 **정성적** 입력이며 데이터와 **별개**로 표시한다.
    충돌해도 어느 쪽이 더 신뢰되는지 **단정하지 않고 둘 다 제시**한다.

    result:
      no_view  — 해당 종목/테마에 active 견해 없음
      observe  — 사용자가 '관찰만' (방향 의견 없음 → 비교 대상 아님)
      agree    — 견해 방향 == 데이터 방향
      differ   — 한쪽이 중립이라 방향이 다름(충돌까지는 아님)
      conflict — 견해와 데이터가 **반대 방향** (정면 충돌)
    """
    acct = _acct(account_index)
    tk = _clean(ticker)
    th = _clean(theme)
    if not tk and not th:
        raise ValueError("ticker 또는 theme 중 하나는 필요합니다")

    # 계좌 격리 — active 견해 중 ticker/theme 매칭(둘 다 주면 둘 중 하나라도 매칭)
    candidates = []
    for v in list_views(acct, status="active"):
        if tk and (v["ticker"] == tk or v["etf"] == tk):
            candidates.append(v)
        elif th and v["theme"] == th:
            candidates.append(v)
    # 매칭 우선순위: conviction 높은 것 우선
    candidates.sort(key=lambda v: (v["conviction"] or 0.0), reverse=True)

    sig_dir, sig_desc = _signal_direction(data_signal)
    target = {"ticker": tk, "theme": th}

    if not candidates:
        return {
            "ok": True, "result": "no_view", "account_index": acct, "target": target,
            "data": {"direction": sig_dir, "desc": sig_desc},
            "view": None,
            "explanation": f"{tk or th}에 대한 저장된 견해가 없습니다. 데이터만 제시: {sig_desc}.",
        }

    view = candidates[0]
    view_dir = _STANCE_DIR.get(view["stance"])
    conv = view["conviction"]
    conv_txt = f"확신도 {conv:.0%}" if isinstance(conv, (int, float)) else "확신도 미입력"
    view_txt = f"내 견해: {view['stance'] or '의견없음'} ({conv_txt})"

    # observe = 관찰만 → 방향 의견 없음
    if view["stance"] == "observe" or view_dir is None:
        result = "observe"
        expl = (f"{tk or th}: 사용자가 '관찰만' 으로 두어 방향 의견이 없습니다. "
                f"데이터({sig_desc})만 참고하세요. 견해와 데이터는 별개 입력입니다.")
    elif sig_dir is None:
        result = "differ"
        expl = (f"{tk or th}: {view_txt}. 그러나 {sig_desc} — 데이터 방향이 불명확해 "
                f"일치/충돌을 단정할 수 없습니다. 둘 다 참고하세요.")
    elif view_dir == sig_dir:
        result = "agree"
        expl = (f"{tk or th}: {view_txt}, 그리고 {sig_desc}. "
                f"견해와 데이터가 **같은 방향**입니다(일치).")
    elif view_dir == 0 or sig_dir == 0:
        result = "differ"
        expl = (f"{tk or th}: {view_txt}, {sig_desc}. 한쪽이 중립이라 "
                f"정면 충돌은 아니지만 방향이 다릅니다.")
    else:
        result = "conflict"
        expl = (f"{tk or th}: {view_txt} 인데 {sig_desc} — **반대 방향(충돌)** 입니다. "
                f"어느 쪽이 옳다고 단정하지 않습니다. 견해(정성적)와 데이터를 함께 보고 판단하세요.")

    return {
        "ok": True, "result": result, "account_index": acct, "target": target,
        "view": view,
        "data": {"direction": sig_dir, "desc": sig_desc},
        "explanation": expl,
    }


# ──────────────────────────── CLI ────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--by-layer", action="store_true")
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--update", type=int, metavar="VIEW_ID")
    ap.add_argument("--archive", type=int, metavar="VIEW_ID")
    ap.add_argument("--history", type=int, metavar="VIEW_ID")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--all", action="store_true", help="목록에 superseded/archived 포함")
    ap.add_argument("--layer")
    ap.add_argument("--theme")
    ap.add_argument("--ticker")
    ap.add_argument("--etf")
    ap.add_argument("--stance")
    ap.add_argument("--conviction", type=float)
    ap.add_argument("--horizon")
    ap.add_argument("--note")
    ap.add_argument("--data-signal", help="compare 용 데이터 신호 JSON")
    args = ap.parse_args()

    try:
        if args.add:
            out = add(args.account, layer=args.layer, theme=args.theme, ticker=args.ticker,
                      etf=args.etf, stance=args.stance, conviction=args.conviction,
                      horizon=args.horizon, note=args.note)
        elif args.update is not None:
            fields = {k: v for k, v in {
                "layer": args.layer, "theme": args.theme, "ticker": args.ticker,
                "etf": args.etf, "stance": args.stance, "conviction": args.conviction,
                "horizon": args.horizon, "note": args.note,
            }.items() if v is not None}
            out = update(args.account, args.update, **fields)
        elif args.archive is not None:
            out = archive(args.account, args.archive)
        elif args.history is not None:
            out = {"ok": True, "history": history(args.account, args.history)}
        elif args.by_layer:
            out = {"ok": True, "by_layer": by_layer(args.account)}
        elif args.compare:
            ds = json.loads(args.data_signal) if args.data_signal else {}
            out = compare_view_vs_data(args.account, ticker=args.ticker, theme=args.theme, data_signal=ds)
        elif args.list:
            out = {"ok": True, "views": list_views(args.account, include_superseded=args.all)}
        else:
            out = {"ok": False, "error": "--list | --by-layer | --add | --update | --archive | --history | --compare"}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
