"""KIS 연결 테스트 CLI (멀티계좌 지원).

  python -m main_mission.portfolio_os.broker.kis_check

.env 에 KIS_ACCOUNT_{n}_* 계좌가 있으면 각 계좌를 순회 검증.
없으면 primary(KIS_APP_KEY 등) 단일 검증.
각 계좌: 토큰 → 현재가(005930) → 잔고. KIS 응답코드 노출. 자격증명 마스킹. 주문 실행 없음.
"""
from __future__ import annotations

import os
import sys

from .kis_client import KisHttpClient, KisConfigError, mask, _load_env
from .kis_adapter import KisPaperAdapter, KisLiveAdapter, domestic_instrument
from .port import Account


def _discover_account_indices() -> list[int]:
    found = []
    for n in range(1, 51):
        if os.getenv(f"KIS_ACCOUNT_{n}_APP_KEY", "").strip():
            found.append(n)
    return found


def _check(client: KisHttpClient, label: str) -> bool:
    summary = client.credential_summary()
    print(f"\n[{label}] mode={summary['mode']} | app_key={summary['app_key']} | "
          f"account={summary['account_no']}-{summary['account_prod']}")
    try:
        client.require_credentials()
    except KisConfigError as e:
        print(f"  ✗ 설정: {e}")
        return False
    try:
        token = client.ensure_token()
        print(f"  ✓ 토큰: {mask(token, keep=6)}")
    except Exception as e:
        print(f"  ✗ 토큰 발급 실패: {e}")
        return False

    adapter = KisLiveAdapter(client) if summary["mode"] == "live" else KisPaperAdapter(client)
    account = Account(id=1, mode=summary["mode"])  # type: ignore
    try:
        q = adapter.get_quote(domestic_instrument("005930", "삼성전자"))
        print(f"  ✓ 현재가 005930: {q.price} KRW" + (" (값 0 — 확인 필요)" if q.is_stale else ""))
    except Exception as e:
        print(f"  ✗ 현재가 조회 실패: {e}")
        return False
    try:
        lines = adapter.get_balance(account)
        cash = adapter.get_cash_krw(account)
        print(f"  ✓ 잔고: 보유 {len(lines)}건, 예수금 {cash} KRW")
    except Exception as e:
        print(f"  ✗ 잔고 조회 실패: {e}")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="KIS 연결 테스트(읽기 전용 — 주문 없음)")
    ap.add_argument("--mode", choices=["paper", "live"],
                    help="모드 강제(계좌 설정 무시). paper 검증 시 --mode paper (PAPER_ 자격증명 사용)")
    ap.add_argument("--account", type=int, help="특정 계좌 index 만 검증")
    a = ap.parse_args(argv)

    print("=== KIS 연결 테스트 ===" + (f" (강제 mode={a.mode})" if a.mode else ""))
    _load_env()
    indices = _discover_account_indices()
    if a.account is not None:
        indices = [a.account]  # 명시 계좌만(미발견이어도 시도 → 명확 오류)

    if indices:
        print(f"검증 대상 계좌 {len(indices)}건 → 각 계좌 검증")
        ok = 0
        for n in indices:
            alias = os.getenv(f"KIS_ACCOUNT_{n}_ALIAS", f"계좌 {n}")
            acct_mode = (os.getenv(f"KIS_ACCOUNT_{n}_MODE", "paper")).strip().lower()
            test_mode = a.mode or ("live" if acct_mode == "live" else "paper")
            try:
                client = KisHttpClient(test_mode, account_index=n)  # type: ignore[arg-type]
                print(f"  ({alias}: 자격증명 prefix={getattr(client,'cred_prefix','?')})")
            except KisConfigError as e:
                print(f"\n[{alias}] 설정 오류: {e}")
                continue
            if _check(client, alias):
                ok += 1
        print(f"\n✅ {ok}/{len(indices)} 계좌 연결 정상.")
        return 0 if ok == len(indices) else 1

    # primary 단일
    env_mode = os.getenv("KIS_MODE", "paper").strip().lower()
    test_mode = a.mode or ("live" if env_mode == "live" else "paper")
    if env_mode not in ("paper", "live"):
        print(f"   (KIS_MODE={env_mode!r} → paper 기준 점검. 계좌는 웹 /accounts/new 에서 추가)")
    try:
        client = KisHttpClient(test_mode)
    except KisConfigError as e:
        print(f"[설정 오류] {e}")
        return 2
    ok = _check(client, "primary")
    if not ok:
        print("\n→ docs/portfolio/kis_onboarding.md 참고하여 .env 를 채우거나 웹에서 계좌를 연결하세요.")
    else:
        print("\n✅ 연결 정상. broker-chief 가 '제안+승인' 방식으로 관리 준비 완료.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
