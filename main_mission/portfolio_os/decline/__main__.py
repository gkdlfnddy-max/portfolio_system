"""CLI — 종목 6축 메타인지 종합 조회(읽기 전용, 주문 0).

  python -m main_mission.portfolio_os.decline --code 005930 --sector 반도체
"""
from __future__ import annotations

import argparse
import json
import sys

from . import composite as composite_mod
from . import context as context_mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True, help="종목코드")
    ap.add_argument("--sector")
    ap.add_argument("--as-of", help="기준일 YYYY-MM-DD (이벤트/정책 최근성)")
    ap.add_argument("--no-track-record", action="store_true")
    args = ap.parse_args()
    try:
        ctx = context_mod.build_context(args.code, sector=args.sector, as_of_date=args.as_of)
        out = composite_mod.composite(ctx, use_track_record=not args.no_track_record)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
