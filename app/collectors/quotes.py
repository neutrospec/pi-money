"""Stock quote collectors via Yahoo Finance (free, no key).

Supports Korean stocks (`.KS`/`.KQ`), US stocks, ETFs, indices.
Returns current price + change + historical series.
"""
from __future__ import annotations

import httpx

from app.collectors import yahoo

YAHOO = "https://query1.finance.yahoo.com"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _get(path: str, params: dict) -> dict:
    r = httpx.get(f"{YAHOO}{path}", params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def quote(symbol: str) -> dict:
    """Fetch current quote for a symbol."""
    payload = _get("/v8/finance/chart/" + symbol, {"range": "5d", "interval": "1d"})
    meta = yahoo.result(payload).get("meta", {})
    return {
        "symbol": symbol,
        "name": meta.get("longName") or meta.get("shortName") or symbol,
        **yahoo.quote_fields(payload),
    }


def history(symbol: str, range_: str = "1y") -> list[dict]:
    """Fetch daily closes dated in the exchange's own local session date."""
    payload = _get("/v8/finance/chart/" + symbol, {"range": range_, "interval": "1d"})
    points = yahoo.settled_points(payload)
    if not points:
        raise ValueError(f"Yahoo returned no history for {symbol}")
    return points
