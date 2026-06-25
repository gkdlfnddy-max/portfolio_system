"""하락 징후 백테스트 헬퍼 — 종목별 예측력 노하우(결정론).

목적:
  1. 가격이력에서 과거 **낙폭 구간**(peak→trough > N%)을 라벨링.
  2. 각 낙폭 직전(선행 window 일)에 **어떤 신호가 선행**했는지 집계.
  3. 종목별 "하락 전 [신호들] 동반" 노하우를 산출 → growth/lessons 후보로 누적(성장).

정직: 이 헬퍼는 결정론(규칙)이며, 실 백테스트는 **일봉 데이터 fetch 후** 의미가 있다.
quotes_seed 근사 데이터로는 표본/정확도가 낮다(과장 금지).
자동매매 없음. Anthropic API 미사용.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import decline_signals as ds
from . import lessons as lessons_mod
from . import price_history as ph

# 낙폭 라벨 기준 (config 의미)
DEFAULT_DECLINE_PCT = 10.0   # peak→trough -10% 이상을 "하락 사건"으로
PRE_WINDOW = 10              # 사건 시작(peak) 직전 며칠의 신호를 선행으로 볼지
MIN_GAP = 5                  # 사건 간 최소 간격(겹침 방지)


def label_declines(history: list[dict], decline_pct: float = DEFAULT_DECLINE_PCT) -> list[dict]:
    """peak→trough 낙폭이 decline_pct 이상인 구간 라벨링 (결정론, 비겹침).

    반환: [{peak_idx, peak_date, peak_close, trough_idx, trough_date, trough_close, drawdown_pct}]
    """
    closes = ds._closes(history)
    n = len(closes)
    events = []
    i = 0
    while i < n:
        peak_idx = i
        peak = closes[i]
        # 고점 이후 최저점 탐색 (새 고점 갱신되면 거기서 재시작)
        trough_idx = i
        trough = closes[i]
        j = i + 1
        while j < n:
            if closes[j] > peak:
                break  # 새 고점 → 현 사건 종료, 거기서 다시
            if closes[j] < trough:
                trough = closes[j]
                trough_idx = j
            j += 1
        dd = (trough / peak - 1.0) * 100.0 if peak else 0.0
        if -dd >= decline_pct and trough_idx > peak_idx:
            events.append({
                "peak_idx": peak_idx, "peak_date": history[peak_idx].get("date"),
                "peak_close": round(peak, 4),
                "trough_idx": trough_idx, "trough_date": history[trough_idx].get("date"),
                "trough_close": round(trough, 4),
                "drawdown_pct": round(dd, 2),
            })
            i = trough_idx + MIN_GAP
        else:
            i = (j if j > i else i + 1)
    return events


def signals_before(history: list[dict], peak_idx: int, window: int = PRE_WINDOW) -> list[str]:
    """사건(peak) 직전 window 일 중 한 번이라도 발화한 신호 집합.

    각 시점 t 에서 history[:t+1] 로 compute_signals (look-ahead 없음).
    """
    fired_any: set[str] = set()
    start = max(ds.MIN_DATA_POINTS, peak_idx - window)
    for t in range(start, peak_idx + 1):
        slice_hist = history[: t + 1]
        try:
            res = ds.compute_signals(slice_hist)
        except ds.NotEnoughData:
            continue
        fired_any.update(res["fired"])
    return sorted(fired_any)


def backtest(instrument_code: str, *, history: list[dict] | None = None,
             decline_pct: float = DEFAULT_DECLINE_PCT, window: int = PRE_WINDOW) -> dict:
    """종목 백테스트: 낙폭 사건 + 각 사건 선행 신호 + 신호별 선행 빈도(예측력 근사).

    정직: precision/recall 이 아니라 "사건 직전 신호 동반 빈도"(연관성). 표본이 작으면
    신뢰 낮음(과장 금지) — confidence 는 사건 수에 따라 보수적으로 산정.
    """
    hist = history if history is not None else ph.load_history(instrument_code)
    if len(ds._closes(hist)) < ds.MIN_DATA_POINTS + window:
        return {"ok": False, "instrument_code": instrument_code, "reason": "not_enough_data",
                "data_points": len(ds._closes(hist))}

    events = label_declines(hist, decline_pct)
    lead: dict[str, int] = {}
    enriched = []
    for ev in events:
        sigs = signals_before(hist, ev["peak_idx"], window)
        for s in sigs:
            lead[s] = lead.get(s, 0) + 1
        enriched.append({**ev, "preceding_signals": sigs})

    n_events = len(events)
    signal_lead_rate = {k: round(v / n_events, 2) for k, v in
                        sorted(lead.items(), key=lambda kv: kv[1], reverse=True)} if n_events else {}

    return {
        "ok": True,
        "instrument_code": instrument_code,
        "decline_pct_threshold": decline_pct,
        "pre_window": window,
        "data_points": len(ds._closes(hist)),
        "decline_events": enriched,
        "event_count": n_events,
        "signal_lead_rate": signal_lead_rate,   # 신호별 (사건 직전 발화 / 전체 사건)
        "honest_note": "연관성(사건 직전 신호 동반 빈도)일 뿐 precision 아님. 표본 작으면 신뢰 낮음. 실 백테스트는 일봉 fetch 전제.",
    }


def accumulate_knowhow(instrument_code: str, bt: dict | None = None, *,
                       history: list[dict] | None = None, agent: str = "decline-analyst") -> dict:
    """백테스트 결과를 growth/lessons **후보**로 누적(성장).

    기존 growth 시스템(lessons.add_candidate) 사용 — 새 API 호출 금지.
    즉시 promoted 아님: candidate 로만(승격은 lessons.promote 기준 — 반복+근거+confidence).
    scope='instrument', ref=instrument_code (계좌 교차적용 아님 — 종목 단위 공통 노하우).
    """
    bt = bt or backtest(instrument_code, history=history)
    if not bt.get("ok") or bt["event_count"] == 0:
        return {"ok": False, "instrument_code": instrument_code,
                "reason": bt.get("reason", "no_decline_events")}

    # 사건 직전 가장 자주 선행한 신호 (lead_rate 상위)
    top = [k for k, v in bt["signal_lead_rate"].items() if v >= 0.5][:4]
    if not top:
        top = list(bt["signal_lead_rate"].keys())[:3]
    title = f"종목 {instrument_code} — 하락 전 선행 신호"
    body = (f"과거 {bt['data_points']}거래일에서 -{bt['decline_pct_threshold']:.0f}% 이상 낙폭 "
            f"{bt['event_count']}회. 사건 직전 {bt['pre_window']}일 빈출 선행 신호: "
            + (", ".join(top) if top else "뚜렷한 선행신호 없음") + ". "
            + bt["honest_note"])
    # confidence: 사건 수가 적으면 낮게(보수). 2회 미만이면 0.4, 그 이상 0.5~0.7.
    conf = 0.4 if bt["event_count"] < 2 else min(0.5 + 0.05 * bt["event_count"], 0.7)

    res = lessons_mod.add_candidate(
        scope="instrument", title=title, body=body, ref=instrument_code,
        outcome=f"{bt['event_count']} decline events",
        confidence=conf, source="decline_backtest", agent=agent,
    )
    return {"ok": True, "instrument_code": instrument_code, "candidate": res,
            "top_leading_signals": top, "confidence": conf}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--decline-pct", type=float, default=DEFAULT_DECLINE_PCT)
    ap.add_argument("--accumulate", action="store_true", help="결과를 lessons 후보로 누적")
    args = ap.parse_args()
    try:
        bt = backtest(args.code, decline_pct=args.decline_pct)
        out = bt
        if args.accumulate and bt.get("ok"):
            out = {"backtest": bt, "knowhow": accumulate_knowhow(args.code, bt)}
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "error": f"내부 오류: {e}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
