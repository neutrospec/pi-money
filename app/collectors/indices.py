"""Global stock index collectors via Yahoo Finance (free, no key).

Covers Korea, US, China, Japan, Europe, Asia, and other major markets.
Full history uses explicit timestamps to preserve daily granularity; refreshes
request only recent sessions.
"""
from __future__ import annotations

import httpx

from app.collectors import yahoo
from app.timeutil import utc_now

YAHOO = "https://query1.finance.yahoo.com"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# (symbol, region, name)
INDICES = [
    # 한국
    ("^KS11", "한국", "코스피"),
    ("^KQ11", "한국", "코스닥"),
    # 미국
    ("^GSPC", "미국", "S&P 500"),
    ("^IXIC", "미국", "나스닥 종합"),
    ("^DJI", "미국", "다우존스"),
    ("^RUT", "미국", "러셀 2000"),
    ("^VIX", "미국", "VIX 변동성"),
    # 중국
    ("000001.SS", "중국", "상하이 종합"),
    ("399001.SZ", "중국", "선전 성분"),
    ("^HSI", "홍콩", "항셍"),
    ("^HSCE", "홍콩", "항셍 중국기업 (H)"),
    # 일본
    ("^N225", "일본", "닛케이 225"),
    # Yahoo does not currently expose a reliable TOPIX index ticker. Track a
    # liquid TOPIX ETF as an explicitly labelled proxy instead of a dead symbol.
    ("1306.T", "일본", "TOPIX ETF (proxy)"),
    # 유럽
    ("^STOXX50E", "유럽", "유로스톡스 50"),
    ("^GDAXI", "독일", "DAX"),
    ("^FTSE", "영국", "FTSE 100"),
    ("^FCHI", "프랑스", "CAC 40"),
    ("^BFX", "벨기에", "BEL 20"),
    # 아시아
    ("^TWII", "대만", "가권지수"),
    ("^AXJO", "호주", "ASX 200"),
    ("^BSESN", "인도", "센섹스"),
    ("^NSEI", "인도", "Nifty 50"),
    # 기타
    ("^BVSP", "브라질", "Bovespa"),
    ("^GSPTSE", "캐나다", "S&P/TSX"),
]

# 지역 설명 (사용성을 해치지 않는 선에서)
REGION_DESC = {
    "한국": "국내 주식시장. 외국인 수급·반도체에 민감.",
    "미국": "세계 금융시장의 중심. 다른 시장을 주도하는 경향.",
    "중국": "중국 본토 시장. 세계 제조업·소비에 영향.",
    "홍콩": "중국 기업의 국제 자금 조달 창구.",
    "일본": "아시아 주요 시장. 세계 3위 경제.",
    "유럽": "유로존·유럽 주요 시장.",
    "독일": "유럽 최대 경제국.",
    "영국": "유럽 금융 중심지.",
    "프랑스": "유럽 주요 경제국.",
    "벨기에": "유럽 소형 시장.",
    "대만": "반도체 중심 시장 (TSMC 등).",
    "호주": "원자재 중심 시장.",
    "인도": "고성장 신흥 시장.",
    "브라질": "남미 최대 시장.",
    "캐나다": "북미 원자재 시장.",
}


# 개별 지수 설명 (중요한 것만)
INDEX_DESC = {
    "^KS11": "코스피 — 한국 대표 지수, 시가총액 상위 기업.",
    "^GSPC": "S&P 500 — 미국 대형주 500개, 세계 주식의 기준.",
    "^IXIC": "나스닥 — 미국 기술주 중심.",
    "^N225": "닛케이 225 — 일본 대표 지수.",
    "^HSI": "항셍 — 홍콩 대표 지수.",
    "^VIX": "VIX — 시장 공포 지수, 높으면 위험 회피.",
    "1306.T": "TOPIX 직접 지수가 아닌 NEXT FUNDS TOPIX ETF 대용치.",
}


def region_description(region: str) -> str:
    return REGION_DESC.get(region, "")


def index_description(symbol: str) -> str:
    if symbol in INDEX_DESC:
        return INDEX_DESC[symbol]
    for idx in INDICES:
        if idx[0] == symbol:
            return REGION_DESC.get(idx[1], "")
    return ""


def index_list() -> list[dict]:
    return [{"symbol": s, "region": r, "name": n} for s, r, n in INDICES]


def _get(symbol: str, params: dict) -> dict:
    r = httpx.get(f"{YAHOO}/v8/finance/chart/{symbol}", params=params, headers=HEADERS, timeout=40)
    r.raise_for_status()
    return r.json()


def _points(payload: dict) -> list[dict]:
    points = yahoo.settled_points(payload)
    if not points:
        raise ValueError("Yahoo returned no usable close values")
    return points


def full_history(symbol: str, years: int = 20) -> list[dict]:
    """Fetch explicit daily history.

    Yahoo may silently downsample ``range=max`` to monthly or quarterly data.
    Explicit Unix bounds preserve the requested daily interval.
    """
    from datetime import timedelta

    end = utc_now()
    start = end - timedelta(days=365 * max(1, min(years, 30)))
    return _points(_get(symbol, {
        "period1": int(start.timestamp()),
        "period2": int(end.timestamp()),
        "interval": "1d",
        "events": "history",
    }))


def recent_history(symbol: str, days: int = 30) -> list[dict]:
    """Fetch recent daily history (for incremental refresh)."""
    return _points(_get(symbol, {"range": f"{max(5, min(days, 365))}d", "interval": "1d"}))


def quote(symbol: str) -> dict:
    """Fetch the current quote plus the provider's last settled session."""
    payload = _get(symbol, {"range": "5d", "interval": "1d"})
    return {"symbol": symbol, **yahoo.quote_fields(payload)}
