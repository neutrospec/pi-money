"""Shared parsing for Yahoo chart payloads.

Indices, watchlist quotes, and the Yahoo-sourced commodity series all read the
same endpoint, so the rules for reading it live here once: how a bar becomes a
dated observation, which session counts as settled, and what "previous close"
means.

Two provider quirks make ad-hoc parsing unsafe:

``timestamp`` marks the session open in UTC
    Reading it as a UTC date moves any session that opens on the other side of
    midnight UTC onto the wrong day.  The payload carries its own
    ``exchangeTimezoneName``; that is the authority.

``chartPreviousClose`` is relative to the requested window, not the session
    It changes with ``range`` and matches neither the last settled close nor
    the one before it, so a change percentage derived from it can come out
    with the wrong sign.  The settled bars in the same payload are exact.
"""
from __future__ import annotations

from app.timeutil import exchange_date


def result(payload: dict) -> dict:
    """Return the single chart result, or raise with the provider's error."""
    results = payload.get("chart", {}).get("result") or []
    if not results:
        error = payload.get("chart", {}).get("error")
        raise ValueError(f"Yahoo returned no result: {error}")
    return results[0]


def settled_points(payload: dict) -> list[dict]:
    """Return every bar that has a close, dated in exchange-local time.

    Bars without a close are the provider's placeholder for a session it has
    not settled yet.  They are not observations and must not be stored.
    """
    res = result(payload)
    meta = res.get("meta", {})
    tz_name = meta.get("exchangeTimezoneName")
    gmt_offset = meta.get("gmtoffset")
    closes = res.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    return [
        {"date": exchange_date(timestamp, tz_name, gmt_offset), "value": float(close)}
        for timestamp, close in zip(res.get("timestamp", []), closes)
        if close is not None
    ]


def settled_session(payload: dict) -> str | None:
    """Return the last session the provider has settled a close for.

    This is the ground truth for "is our daily history complete?" and needs no
    trading calendar.  ``regularMarketTime`` is deliberately not used: it
    advances into a session whose bar still carries a null close, so treating
    it as expected coverage would leave completeness permanently unreachable.
    """
    try:
        points = settled_points(payload)
    except ValueError:
        return None
    return points[-1]["date"] if points else None


def current_session(payload: dict) -> str | None:
    """Return the session the live price belongs to, settled or not."""
    meta = result(payload).get("meta", {})
    moment = meta.get("regularMarketTime")
    if not moment:
        return None
    return exchange_date(
        moment, meta.get("exchangeTimezoneName"), meta.get("gmtoffset")
    )


def quote_fields(payload: dict) -> dict:
    """Return the live price with a previous close from the same payload.

    The previous close is whichever settled bar precedes the session the live
    price belongs to, so the pair always describes one session's move.  This
    needs at least two settled bars in the window; callers request five days.
    """
    meta = result(payload).get("meta", {})
    price = meta.get("regularMarketPrice")
    points = settled_points(payload)
    latest = points[-1]["date"] if points else None
    live_session = current_session(payload)
    if latest is not None and live_session == latest:
        # The live price is the settled bar; compare against the one before.
        previous = points[-2]["value"] if len(points) >= 2 else None
    else:
        # The live session has not settled; the last settled bar is previous.
        previous = points[-1]["value"] if points else None
    return {
        "price": price,
        "prev_close": previous,
        "currency": meta.get("currency"),
        "session_date": latest,
        "live_session_date": live_session,
    }
