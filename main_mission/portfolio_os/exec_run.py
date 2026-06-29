"""분할 집행 실행기 — 승인된 분할 plan 을 브로커에 예약 지정가로 제출(웹 '집행' 배선).

build_split_plan(제안/draft)과 분리된 **실집행 엔트리포인트**. 웹 라우트가 spawn 한다.

⚠️ 안전(절대 규칙 §4·5·6·15·16) — 다층 게이트:
  - **모의투자(paper) 우선.** --mode 명시 필수 — env(KIS_MODE=live)로 silent live 금지.
  - **--approve(전략 승인) 없으면 거부** — 무승인 자동매매 금지.
  - **live 는 이중 확인**: --i-understand-live=I_UNDERSTAND_LIVE + KIS_LIVE_CONFIRM=I_UNDERSTAND
    (후자는 factory `_require_live_confirm` 가 강제 — 둘 다 있어야 live broker 생성).
  - 시장가 매수 금지·지정가만 · 리스크 게이트 · idempotency · health · 매수여력 ·
    live 제출시점 재검증 · audit 는 모두 submit_order 내부 게이트(SSOT). 여기서 우회 불가.
  - 가격 없으면 스킵(가짜 숫자 금지) — build_split_plan 과 동일.
  - 지능 = Claude+메모리 (Anthropic API 미사용).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import exec_plan
from .broker.factory import get_broker
from .broker.port import Account

LIVE_PHRASE = "I_UNDERSTAND_LIVE"  # 웹/CLI 측 이중 확인 문구(환경변수 KIS_LIVE_CONFIRM 와 별개 레이어)


def _load_plan(account_index: int, picks: dict, *, rounds: int, period: int,
               knee: float, cash: float | None, token: str | None, fetch_fx: bool = False,
               equity_option: str = "none"):
    """picks → 가격/예수금/시장 자동 로드 후 build_split_plan(집행 직전 재계산 — draft 와 동일 로직).

    반환: (plan|None, available_cash, error|None)
    """
    from . import price_history
    try:
        from .security_selection import _TICKER_META
    except Exception:  # noqa: BLE001
        _TICKER_META = {}
    from .store import db as _db

    if cash is None:  # 예수금 자동 조회(최신 스냅샷)
        conn = _db.connect()
        try:
            row = conn.execute(
                "SELECT cash_krw FROM account_snapshots WHERE account_index=? "
                "ORDER BY datetime(captured_at) DESC, id DESC LIMIT 1", (account_index,)).fetchone()
            cash = float(row["cash_krw"]) if row and row["cash_krw"] is not None else 0.0
        finally:
            conn.close()
    if not cash or cash <= 0:
        return None, 0.0, {"ok": False, "stage": "cash", "error": "예수금 없음 — 계좌 동기화(sync) 후 재시도"}

    tickers = [t for arr in (picks or {}).values() if isinstance(arr, list) for t in arr]
    prices: dict = {}
    markets: dict = {}
    for t in tickers:
        bars = price_history.load_history(t)
        if bars:
            try:
                prices[t] = float(bars[-1]["close"])
            except (TypeError, ValueError, KeyError):
                pass
        m = _TICKER_META.get(t, {})
        mk = m.get("market")
        if mk:
            ccy = "KRW" if mk in ("KRX", "KOSPI", "KOSDAQ") else "USD"
            markets[t] = (mk, ccy)
    tok = token or datetime.now(timezone.utc).strftime("%Y%m%d")
    # 외화(USD) 종목 환율은 실제 집행(CLI) 경로에서만 네트워크 조회(fetch_fx=True).
    #   테스트/직접호출(default)은 None=환산 비활성으로 순수 유지(오프라인).
    fx_rates = exec_plan.fx_rates_for_markets(markets) if fetch_fx else None
    plan = exec_plan.build_split_plan(account_index, picks, prices=prices, cash_krw=cash,
                                      rounds=rounds, period_days=period, knee_pct=knee,
                                      markets=markets, plan_token=tok, fx_rates=fx_rates,
                                      equity_option=equity_option)
    return plan, cash, None


def run(account_index: int, picks: dict, *, mode: str, approve: bool,
        rounds: int = exec_plan.ROUNDS_DEFAULT, period: int = 14,
        knee: float = exec_plan.KNEE_PCT_DEFAULT, cash: float | None = None,
        token: str | None = None, live_confirm: str = "", fetch_fx: bool = False,
        equity_option: str = "none") -> dict:
    """승인된 plan 을 집행. 각 게이트 실패는 stage 로 명확 보고(부분 우회 불가)."""
    # 1) 전략 승인 게이트 (무승인 자동매매 금지)
    if not approve:
        return {"ok": False, "stage": "approval", "submitted": 0,
                "error": "전략 승인 없음 — 무승인 집행 거부(자동매매 금지 §6)"}
    # 2) mode 명시 필수 (env 로 silent live 금지)
    if mode not in ("mock", "paper", "live"):
        return {"ok": False, "stage": "mode", "submitted": 0,
                "error": f"mode 명시 필요(mock|paper|live): {mode!r}"}
    # 3) live 이중 확인 (factory 가 KIS_LIVE_CONFIRM 도 별도 강제)
    if mode == "live" and live_confirm != LIVE_PHRASE:
        return {"ok": False, "stage": "live_confirm", "submitted": 0,
                "error": f"live 이중확인 필요(--i-understand-live {LIVE_PHRASE}) + KIS_LIVE_CONFIRM=I_UNDERSTAND (§15)"}
    # 4) plan 재계산(집행 직전 최신 가격)
    plan, avail_cash, err = _load_plan(account_index, picks, rounds=rounds, period=period,
                                       knee=knee, cash=cash, token=token, fetch_fx=fetch_fx,
                                       equity_option=equity_option)
    if err:
        return {**err, "submitted": 0}
    if not plan.get("ok"):
        return {"ok": False, "stage": "plan", "submitted": 0, "error": plan.get("error", "plan 실패")}
    if plan.get("blocked"):
        return {"ok": False, "stage": "risk_gate", "submitted": 0,
                "error": "리스크 게이트 차단 — 한도 초과(분산 필요)",
                "over_limit_warnings": plan.get("over_limit_warnings", [])}
    if not plan.get("steps"):
        return {"ok": False, "stage": "plan", "submitted": 0,
                "error": "집행할 회차 없음 — 가격 미연동 또는 회차예산<1주(가격 적재·회차/종목 조정 필요)",
                "skipped": plan.get("skipped", [])}
    # 5) broker 주입 (live 는 factory `_require_live_confirm` 가 KIS_LIVE_CONFIRM 검증 → 없으면 RuntimeError)
    try:
        broker = get_broker(mode=mode, account_index=account_index)
    except Exception as e:  # noqa: BLE001 — live 하드락/키 실패는 명확 보고(가짜 성공 금지)
        return {"ok": False, "stage": "broker", "submitted": 0, "error": str(e)}
    # Account.mode 는 broker 실제 mode 와 일치해야 함(submit_order 의 mode-match 게이트).
    #   mock adapter 는 mode="paper" 로 보고 → account 도 그 값을 따른다(불일치 abort 방지).
    account = Account(id=account_index, mode=getattr(broker, "mode", mode))  # type: ignore[arg-type]
    # 6) 집행 — submit_order 내부 게이트(시장가 차단·idempotency·health·매수여력·live 재검증·audit)
    try:
        res = exec_plan.execute_plan(plan, broker, account, approved=True, available_cash_krw=avail_cash)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stage": "execute", "submitted": 0, "error": str(e)}
    return {**res, "stage": "done", "mode": mode, "plan_steps": plan["step_count"],
            "total_target_krw": plan.get("total_target_krw")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="분할 집행 실행기 — 승인된 plan 예약 지정가 제출(paper 우선)")
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--picks", required=True, help='JSON {"bucket":["TICKER",...]}')
    ap.add_argument("--mode", required=True, choices=["mock", "paper", "live"],
                    help="명시 필수 — env(KIS_MODE)로 silent live 금지")
    ap.add_argument("--approve", action="store_true", help="전략(CEO) 승인 — 없으면 집행 거부")
    ap.add_argument("--rounds", type=int, default=exec_plan.ROUNDS_DEFAULT)
    ap.add_argument("--period", type=int, default=14)
    ap.add_argument("--knee", type=float, default=exec_plan.KNEE_PCT_DEFAULT)
    ap.add_argument("--cash", type=float, default=None)
    ap.add_argument("--token", default=None)
    ap.add_argument("--i-understand-live", dest="live_confirm", default="",
                    help=f"live 이중확인 문구({LIVE_PHRASE})")
    ap.add_argument("--equity-option", dest="equity_option", default="none", choices=("none", "5", "10"),
                    help="개별주 carve(picks['individual'] 에 위험자산 5%/10% 균등)")
    a = ap.parse_args(argv)

    try:
        picks = json.loads(a.picks)
    except ValueError as e:
        sys.stdout.write(json.dumps({"ok": False, "stage": "input", "submitted": 0,
                                     "error": f"picks JSON 파싱 실패: {e}"}, ensure_ascii=False) + "\n")
        return 0
    out = run(a.account, picks, mode=a.mode, approve=a.approve, rounds=a.rounds,
              period=a.period, knee=a.knee, cash=a.cash, token=a.token, live_confirm=a.live_confirm,
              fetch_fx=True, equity_option=a.equity_option)  # 실집행(CLI)은 USD 환율 실조회
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
