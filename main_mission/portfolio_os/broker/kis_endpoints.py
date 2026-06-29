"""KIS 엔드포인트 / tr_id 상수 (SSOT).

출처: docs/portfolio/api_adapter.md §3, §7 (Wave 1 검증, 2026-06-19).
임의 추측 금지(§9). 값 변경은 KIS 공식 문서 재검증 후 본 파일에서만.

paper(모의) vs live(실전)은 도메인 + tr_id prefix 가 다르다.
호출측은 mode 만 넘기고, 분기는 여기서 책임.
"""
from __future__ import annotations

from typing import Literal

Mode = Literal["paper", "live"]

# --- REST 도메인 (✅ 검증) ---
PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"
LIVE_BASE_URL = "https://openapi.koreainvestment.com:9443"

# --- WebSocket 도메인 (참고, 실시간 체결통보용) ---
PAPER_WS_URL = "ops.koreainvestment.com:31000"
LIVE_WS_URL = "ops.koreainvestment.com:21000"

# --- 초당 호출 한도 (✅ 검증 — 토큰버킷에 사용) ---
RATE_LIMIT_PER_SEC = {"paper": 5, "live": 20}

# --- 공통 경로 ---
PATH_TOKEN = "/oauth2/tokenP"
PATH_HASHKEY = "/uapi/hashkey"

# --- 국내 시세 ---
PATH_DOMESTIC_PRICE = "/uapi/domestic-stock/v1/quotations/inquire-price"
TRID_DOMESTIC_PRICE = "FHKST01010100"  # ✅ mode 무관

# --- 국내 기간별시세(일/주/월/년) — 일봉(OHLCV) ---
# 출처: KIS Developers 공식 endpoint(apiportal /uapi/.../inquire-daily-itemchartprice),
#       wikidocs 239682, kis-client/zerohertzLib 등 다수 독립 소스로 교차확인(2026-06-21).
#       tr_id 는 inquire-price 와 동일하게 mode 무관(현재가 패턴과 동일).
# 요청 FID: FID_COND_MRKT_DIV_CODE="J"(주식/ETF/ETN), FID_INPUT_ISCD=종목6자리,
#          FID_INPUT_DATE_1/2="YYYYMMDD"(시작/종료), FID_PERIOD_DIV_CODE="D"(일),
#          FID_ORG_ADJ_PRC="0"(수정주가)/"1"(원주가).
# 응답: output2[] 에 일봉 — stck_bsop_date/stck_oprc/stck_hgpr/stck_lwpr/stck_clpr/acml_vol.
#       1회 최대 100건 → 날짜 윈도우로 페이징(과거로 이동).
PATH_DOMESTIC_DAILY_CHART = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
TRID_DOMESTIC_DAILY_CHART = "FHKST03010100"  # ✅ 다수 소스 교차확인, mode 무관
DAILY_CHART_MAX_PER_CALL = 100               # KIS 1회 응답 상한(페이징 단위)

# --- 국내 종목별 투자자 매매동향 (외국인/기관/개인 순매수) — 분산축(distribution) ---
# 출처: KIS Developers 공식 open-trading-api(examples_llm/domestic_stock/inquire_investor),
#       wikidocs 163499 등 다수 독립 소스로 교차확인(2026-06-22).
# endpoint: /uapi/domestic-stock/v1/quotations/inquire-investor, tr_id FHKST01010900 (mode 무관).
# 요청 FID: FID_COND_MRKT_DIV_CODE="J"(주식/ETF/ETN), FID_INPUT_ISCD=종목6자리.
# 응답 output[]: 일자별(최신→과거) 행 —
#   stck_bsop_date  주식 영업 일자(YYYYMMDD)
#   prsn_ntby_qty   개인(retail)   순매수 수량  (순매도면 음수)
#   frgn_ntby_qty   외국인(foreign) 순매수 수량
#   orgn_ntby_qty   기관계(institution) 순매수 수량
#   prsn/frgn/orgn_shnu_vol  각 주체 매수 거래량 (합으로 시장 거래량 근사)
# ⚠️ 본 TR 은 외국인/기관/개인 3주체만 제공. 연기금/프로그램 등 세부 주체는 본 TR 에 없음
#    → 만들지 않는다(정직). 별도 세부 TR 은 추후 공식 확인 후 확장.
PATH_DOMESTIC_INVESTOR = "/uapi/domestic-stock/v1/quotations/inquire-investor"
TRID_DOMESTIC_INVESTOR = "FHKST01010900"  # ✅ 공식 다수 소스 교차확인, mode 무관

# --- 국내 잔고 ---
PATH_DOMESTIC_BALANCE = "/uapi/domestic-stock/v1/trading/inquire-balance"
TRID_DOMESTIC_BALANCE = {
    "live": "TTTC8434R",   # ✅ 검증
    "paper": "VTTC8434R",  # ⚠️ 모의 코드 — 공식 재검증 권장
}

# --- 국내 현금주문 (매수/매도) ---
PATH_DOMESTIC_ORDER = "/uapi/domestic-stock/v1/trading/order-cash"
TRID_DOMESTIC_ORDER = {
    # (mode, side) → tr_id.  실전 ✅ / 모의 V-prefix ⚠️ 재검증
    ("live", "buy"): "TTTC0802U",
    ("live", "sell"): "TTTC0801U",
    ("paper", "buy"): "VTTC0802U",
    ("paper", "sell"): "VTTC0801U",
}

# --- 미국주식 (해외) ---
PATH_OVERSEAS_BALANCE = "/uapi/overseas-stock/v1/trading/inquire-balance"
PATH_OVERSEAS_PRICE = "/uapi/overseas-price/v1/quotations/price"
PATH_OVERSEAS_ORDER = "/uapi/overseas-stock/v1/trading/order"

# 미국 주문 tr_id (mode, side) → tr_id.
#   ⚠️⚠️ 미검증 — KIS 개발자센터 공식 문서로 **전수 재검증 필요**(출처 엇갈림: TTTT1002U vs JTTT1002U).
#   실주문 전 반드시 확인하거나 소액 1주 테스트로 검증할 것. 잘못된 코드면 KIS 가 거부(rt_cd≠0).
TRID_OVERSEAS_ORDER = {
    ("live", "buy"): "TTTT1002U",    # 미국 매수(실전) — ⚠️ 미검증
    ("live", "sell"): "TTTT1006U",   # 미국 매도(실전) — ⚠️ 미검증
    ("paper", "buy"): "VTTT1002U",   # 미국 매수(모의) — ⚠️ 미검증
    ("paper", "sell"): "VTTT1001U",  # 미국 매도(모의) — ⚠️ 미검증
}

# 우리 market/거래소 라벨 → KIS 해외 거래소 코드(OVRS_EXCG_CD).
#   NASD=나스닥, NYSE=뉴욕, AMEX=NYSE Arca/American 등. "US"(미상)는 매핑 없음 → 주문측에서 거부.
_KIS_US_EXCH = {
    "NASDAQ": "NASD", "NASD": "NASD",
    "NYSE": "NYSE",
    "AMEX": "AMEX", "ARCA": "AMEX", "NYSEARCA": "AMEX", "PCX": "AMEX", "BATS": "AMEX", "BATSGLOBAL": "AMEX",
}


def base_url(mode: Mode) -> str:
    return LIVE_BASE_URL if mode == "live" else PAPER_BASE_URL


def domestic_balance_trid(mode: Mode) -> str:
    return TRID_DOMESTIC_BALANCE[mode]


def domestic_order_trid(mode: Mode, side: str) -> str:
    return TRID_DOMESTIC_ORDER[(mode, side)]


def overseas_order_trid(mode: Mode, side: str) -> str:
    return TRID_OVERSEAS_ORDER[(mode, side)]


def kis_overseas_exchange(market: str) -> str:
    """우리 market/거래소 라벨 → KIS OVRS_EXCG_CD(NASD/NYSE/AMEX). 미상이면 빈 문자열."""
    return _KIS_US_EXCH.get((market or "").upper().replace(" ", ""), "")
