"""세부 선정 위저드(종목·ETF 선정 화면)의 작업중 draft 저장/복원 — 계좌당 1건.

문제(해결 대상): 선정 화면의 모든 상태(고른 종목·개별주 carve·초안 승인 표시)가
브라우저 메모리에만 있어 새로고침/재접속 시 사라졌다("초안 승인 했는데 저장이 안 됨").

원칙(불변):
- 이건 **draft 저장만** 한다 — policy(목표비중)·주문에 **반영하지 않는다.**
  실제 반영은 confirmed allocation([[selection.py]]) + 리스크 게이트 + CEO 최종 승인 단계.
- 계좌별 격리(account scope) — 다른 계좌 draft 를 교차 적용 금지.
- 계좌당 현재 draft 1건만 유지(덮어쓰기). 이력이 아니라 "작업중 상태"의 복원이 목적.
- 지능 = Claude+메모리 (Anthropic API 미사용).

테이블(schema.sql, 편집 금지): selection_drafts
  (account_index PK, proposal_id, picks_json, equity_option, acknowledged,
   acknowledged_at, updated_at)

  python -m main_mission.portfolio_os.selection_draft --account 1 --load
  python -m main_mission.portfolio_os.selection_draft --account 1 --save --payload-b64 <b64(json)>
     payload(json) = {"picks":[{bucket,ticker,name,asset_class}],
                      "equity_option":"none|5|10", "acknowledged":true|false,
                      "proposal_id":"...(선택)"}
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timezone

from .store import db as store_db

EQUITY_OPTIONS = ("none", "5", "10")  # 개별주 carve(위험자산 60% 내 분배 비율)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_picks(raw) -> list[dict]:
    """입력 picks 를 안전한 최소 스키마로 정규화(불필요/위험 키 제거)."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    seen: set[tuple[str, str]] = set()
    for p in raw:
        if not isinstance(p, dict):
            continue
        bucket = str(p.get("bucket") or "").strip()
        ticker = str(p.get("ticker") or "").strip()
        if not bucket or not ticker:
            continue
        key = (bucket, ticker)
        if key in seen:  # 중복 제거(같은 bucket:ticker 1회)
            continue
        seen.add(key)
        out.append({
            "bucket": bucket,
            "ticker": ticker,
            "name": (str(p["name"]).strip() if p.get("name") not in (None, "") else None),
            "asset_class": (str(p["asset_class"]).strip() if p.get("asset_class") not in (None, "") else None),
        })
    return out


def load(account_index: int, *, conn=None) -> dict | None:
    """계좌의 현재 선정 draft(없으면 None)."""
    own = conn is None
    conn = conn or store_db.connect()
    try:
        r = conn.execute(
            "SELECT account_index, proposal_id, picks_json, equity_option, acknowledged, "
            "acknowledged_at, updated_at FROM selection_drafts WHERE account_index=?",
            (account_index,),
        ).fetchone()
    finally:
        if own:
            conn.close()
    if not r:
        return None
    try:
        picks = json.loads(r["picks_json"] or "[]")
    except (TypeError, ValueError):
        picks = []
    return {
        "account_index": r["account_index"],
        "proposal_id": r["proposal_id"],
        "picks": picks,
        "equity_option": r["equity_option"] or "none",
        "acknowledged": bool(r["acknowledged"]),
        "acknowledged_at": r["acknowledged_at"],
        "updated_at": r["updated_at"],
    }


def save(account_index: int, *, picks, equity_option: str = "none",
         acknowledged: bool = False, proposal_id: str | None = None) -> dict:
    """선정 draft upsert(계좌당 1건 덮어쓰기). policy/주문 미반영 — 저장만."""
    picks_n = _normalize_picks(picks)
    opt = equity_option if equity_option in EQUITY_OPTIONS else "none"
    ack = 1 if acknowledged else 0

    conn = store_db.connect()
    try:
        prev = conn.execute(
            "SELECT acknowledged, acknowledged_at FROM selection_drafts WHERE account_index=?",
            (account_index,),
        ).fetchone()
        # acknowledged_at: 미승인→승인 전환 시각 기록, 계속 승인이면 기존 시각 보존, 승인 해제면 null.
        if ack:
            ack_at = (prev["acknowledged_at"] if prev and prev["acknowledged"] else _now())
        else:
            ack_at = None

        conn.execute(
            "INSERT INTO selection_drafts(account_index, proposal_id, picks_json, equity_option, "
            "acknowledged, acknowledged_at, updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(account_index) DO UPDATE SET "
            "proposal_id=excluded.proposal_id, picks_json=excluded.picks_json, "
            "equity_option=excluded.equity_option, acknowledged=excluded.acknowledged, "
            "acknowledged_at=excluded.acknowledged_at, updated_at=excluded.updated_at",
            (account_index, proposal_id, json.dumps(picks_n, ensure_ascii=False),
             opt, ack, ack_at, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "account_index": account_index, "saved_picks": len(picks_n),
            "equity_option": opt, "acknowledged": bool(ack), "acknowledged_at": ack_at}


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="세부 선정 draft 저장/복원(계좌별, draft only)")
    ap.add_argument("--account", type=int, required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--load", action="store_true")
    g.add_argument("--save", action="store_true")
    ap.add_argument("--payload-b64", help="--save 시 base64(json) payload")
    args = ap.parse_args(argv)

    if args.account < 1:
        _emit({"ok": False, "error": "account 는 1 이상"})
        return 2

    if args.load:
        d = load(args.account)
        _emit({"ok": True, "draft": d})
        return 0

    # --save
    if not args.payload_b64:
        _emit({"ok": False, "error": "--save 에는 --payload-b64 필요"})
        return 2
    try:
        payload = json.loads(base64.b64decode(args.payload_b64).decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — 입력 파싱 실패는 사용자 입력 오류로 보고
        _emit({"ok": False, "error": f"payload 파싱 실패: {e}"})
        return 2
    if not isinstance(payload, dict):
        _emit({"ok": False, "error": "payload 는 object 여야 함"})
        return 2
    res = save(
        args.account,
        picks=payload.get("picks", []),
        equity_option=str(payload.get("equity_option", "none")),
        acknowledged=bool(payload.get("acknowledged", False)),
        proposal_id=(str(payload["proposal_id"]) if payload.get("proposal_id") else None),
    )
    _emit(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
