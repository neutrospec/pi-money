"""Watchlist configuration — edit this to track your own tickers.

Symbols: Korean stocks use `.KS` (KOSPI) / `.KQ` (KOSDAQ) suffix.
US stocks, ETFs, and indices use plain tickers (QQQ, AAPL, ^VIX, ^KS11).

Each entry: (symbol, category, label)
Category is used to filter in the UI (e.g. 연금ETF, 해외, 채권, 원자재).
"""
from __future__ import annotations

# (symbol, category, label)
WATCHLIST = [
    # ===== 한국 지수 =====
    ("^KS11", "지수", "코스피"),
    ("^KQ11", "지수", "코스닥"),
    # ===== 한국 대형주 =====
    ("005930.KS", "한국주식", "삼성전자"),
    ("000660.KS", "한국주식", "SK하이닉스"),
    ("373220.KS", "한국주식", "LG에너지솔루션"),
    ("005380.KS", "한국주식", "현대차"),
    ("068270.KS", "한국주식", "셀트리온"),
    # ===== 미국 지수·ETF =====
    ("^GSPC", "지수", "S&P 500"),
    ("^IXIC", "지수", "나스닥"),
    ("^DJI", "지수", "다우"),
    ("^VIX", "지수", "VIX"),
    ("QQQ", "해외ETF", "나스닥 100 ETF"),
    ("SPY", "해외ETF", "S&P 500 ETF"),
    # ===== 미국 주요주 =====
    ("AAPL", "해외주식", "애플"),
    ("MSFT", "해외주식", "마이크로소프트"),
    ("NVDA", "해외주식", "엔비디아"),
    ("TSLA", "해외주식", "테슬라"),
    # ===== 연금 ETF: 국내 지수형 =====
    ("069500.KS", "연금ETF-국내", "KODEX 코스피200"),
    ("102110.KS", "연금ETF-국내", "TIGER 코스피200"),
    ("229200.KS", "연금ETF-국내", "TIGER 코스닥150"),
    # ===== 연금 ETF: 해외 지수형 =====
    ("379810.KS", "연금ETF-해외", "KODEX 미국S&P500"),
    ("360750.KS", "연금ETF-해외", "TIGER 미국S&P500"),
    ("379800.KS", "연금ETF-해외", "KODEX 미국나스닥100"),
    ("133690.KS", "연금ETF-해외", "TIGER 미국나스닥100"),
    ("458750.KS", "연금ETF-해외", "TIGER 미국배당다우존스"),
    ("363580.KS", "연금ETF-해외", "TIGER 미국S&P500배당귀족"),
    # ===== 연금 ETF: 배당형 =====
    ("161510.KS", "연금ETF-배당", "KODEX 배당성장"),
    # ===== 연금 ETF: 채권형 =====
    ("157450.KS", "연금ETF-채권", "TIGER 단기채권"),
    ("153130.KS", "연금ETF-채권", "KODEX 단기채권"),
    ("148070.KS", "연금ETF-채권", "ACE 국고채10년"),
    # ===== 연금 ETF: 원자재 =====
    ("319640.KS", "연금ETF-원자재", "TIGER KRX금현물"),
    ("132030.KS", "연금ETF-원자재", "KODEX 골드선물"),
    # ===== 연금 ETF: 섹터 =====
    ("305080.KS", "연금ETF-섹터", "TIGER 반도체"),
    ("305720.KS", "연금ETF-섹터", "TIGER 2차전지"),
]


def watchlist() -> list[dict]:
    return [{"symbol": s, "group": g, "label": l} for s, g, l in WATCHLIST]


# 카테고리 설명 (투자 성격·대상)
CATEGORY_DESC = {
    "지수": "대표 주가지수 (코스피, S&P 500, VIX 등)",
    "한국주식": "한국 대형주 개별 종목",
    "해외ETF": "미국 상장 ETF (나스닥 100, S&P 500)",
    "해외주식": "미국 주요주 개별 종목",
    "연금ETF-국내": "국내 지수 추종 ETF (코스피200, 코스닥150) — 연금계좌 투자 가능",
    "연금ETF-해외": "해외 지수 추종 ETF (미국 S&P, 나스닥, 배당) — 연금계좌 투자 가능",
    "연금ETF-배당": "배당 중심 ETF — 연금계좌 투자 가능",
    "연금ETF-채권": "채권형 ETF (단기채권, 국고채) — 안전자산, 연금계좌 투자 가능",
    "연금ETF-원자재": "원자재 ETF (금, 원유 등) — 연금계좌 투자 가능",
    "연금ETF-섹터": "특정 섹터 ETF (반도체, 2차전지) — 연금계좌 투자 가능",
}


def category_description(category: str) -> str:
    return CATEGORY_DESC.get(category, "")


def categories() -> list[str]:
    """Distinct categories for UI filtering."""
    seen = []
    for w in watchlist():
        if w["group"] not in seen:
            seen.append(w["group"])
    return seen