"""The single gateway for every time value in this system.

Three kinds of time exist here and must never be confused:

``instant``
    When *we* retrieved something.  Stored as UTC ISO-8601 with an explicit
    offset so a value can never be reinterpreted against the host timezone.

``observation date``
    When the market or statistic *happened*.  Stored as the local business
    date of the market that produced it, because that is the identity the
    provider, the exchange, and every other data source agree on.

``presentation``
    What the user reads.  KST.

Converting a provider epoch without its exchange timezone silently shifts
observations across the date line: an ASX session opening 10:00 in Sydney is
23:00 UTC the previous day for the five months Australia observes daylight
saving.  Every conversion therefore goes through this module.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC = timezone.utc
KST = ZoneInfo("Asia/Seoul")


# --------------------------------------------------------------------------
# Instants — when we collected something
# --------------------------------------------------------------------------
def utc_now() -> datetime:
    """Current instant as an aware UTC datetime."""
    return datetime.now(UTC)


def utc_now_iso() -> str:
    """Current instant as the canonical stored string."""
    return utc_now().isoformat(timespec="microseconds")


def parse_instant(value: str | None) -> datetime | None:
    """Parse a stored instant into aware UTC, or None if unusable.

    Values written before the storage convention was enforced carry no
    offset.  They were produced by UTC clocks, so they are read as UTC
    rather than as host-local time, which would shift them by the
    deployment's timezone.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def instant_age_seconds(value: str | None, *, now: datetime | None = None) -> float:
    """Seconds elapsed since a stored instant; infinite when unknown."""
    parsed = parse_instant(value)
    if parsed is None:
        return float("inf")
    return ((now or utc_now()) - parsed).total_seconds()


def instant_epoch(value: str | None) -> float:
    """Epoch seconds for a stored instant; 0.0 when unknown."""
    parsed = parse_instant(value)
    return parsed.timestamp() if parsed else 0.0


# --------------------------------------------------------------------------
# Observation dates — when the market moved
# --------------------------------------------------------------------------
@lru_cache(maxsize=64)
def _zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def exchange_date(
    epoch: int | float,
    tz_name: str | None = None,
    gmt_offset: int | None = None,
) -> str:
    """Convert a provider epoch to the exchange's local business date.

    ``tz_name`` is preferred because it accounts for daylight saving across
    the whole history.  ``gmt_offset`` is the provider's current offset and
    is only a fallback for a zone this platform does not know.  Refusing to
    guess is deliberate: a silently wrong date corrupts every date-aligned
    correlation downstream, whereas a raised error fails one symbol loudly.
    """
    moment = datetime.fromtimestamp(int(epoch), UTC)
    zone = _zone(tz_name) if tz_name else None
    if zone is not None:
        return moment.astimezone(zone).date().isoformat()
    if gmt_offset is not None:
        return (moment + timedelta(seconds=int(gmt_offset))).date().isoformat()
    raise ValueError(
        f"cannot resolve an observation date for epoch {epoch}: "
        f"exchange timezone {tz_name!r} is unknown and no gmtoffset was given"
    )


def kst_today() -> date:
    """Today in the presentation timezone."""
    return utc_now().astimezone(KST).date()


def to_kst(value: str | None) -> datetime | None:
    """Render a stored instant in the presentation timezone."""
    parsed = parse_instant(value)
    return parsed.astimezone(KST) if parsed else None
