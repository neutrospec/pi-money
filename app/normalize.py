"""Where a value sits in its own distribution — one definition, one home.

A number on its own does not support a judgement.  4.28% is not high or low
until you know what this series usually does, and a fixed threshold cannot
answer that for a series whose normal level moves: a 0.688%p corporate spread
looks narrow next to the US investment-grade rule of thumb and sits at the
87th percentile of its own recent range.

Two modules used to implement this scoring separately and identically, and
each picked its observation window at the call site.  That is how the same
VKOSPI reading came to be scored against a trailing year in one place and the
whole record in another.  The window is a property of the series, so it is
declared next to the series rather than passed in by whoever happens to call.

The contract mirrors the rest of the project: a reading that cannot be made
is reported with its reason, never estimated.
"""
from __future__ import annotations

from app import db
from app.collectors import indicators


# A trailing year is the default because most levels here drift with a cycle —
# a policy rate or a credit spread has no fixed "normal", so its own recent
# range is the honest baseline.  Series that mean revert are the exception and
# say so; see ``indicators.MEAN_REVERTING_LEVELS``.
TRAILING_WINDOW = 250
FULL_HISTORY = None

# Enough observations to call something a distribution. Below this the
# percentile is arithmetic, not evidence.
MIN_OBSERVATIONS = 60


def percentile(value: float, history: list[float], *, invert: bool = False) -> float:
    """Position of ``value`` within ``history``, 0-100.

    ``invert`` flips the orientation so that 100 reads as risk-on rather than
    as a high value.  An empty history returns the midpoint: a component that
    holds a value but no distribution yet is undecided, not absent — absence
    is ``position``'s job to report.
    """
    if not history:
        return 50.0
    below = sum(1 for item in history if item < value)
    share = below / len(history) * 100
    return round(100 - share if invert else share, 1)


def window_for(key: str) -> int | None:
    """The observation window this series should be judged against.

    Full history for a mean-reverting level, a trailing year otherwise.  The
    distinction is not cosmetic: a one-year window that contains a crisis lets
    the crisis define what normal means and scores itself as unremarkable.
    """
    return (
        FULL_HISTORY if key in indicators.MEAN_REVERTING_LEVELS
        else TRAILING_WINDOW
    )


def window_label(window: int | None, observations: int) -> str:
    if window is None:
        return f"전체 {observations}거래일"
    return f"최근 {min(window, observations)}거래일"


def position(
    points: list[dict] | None,
    *,
    direction: str | None = None,
    window: int | None = TRAILING_WINDOW,
    minimum: int = MIN_OBSERVATIONS,
    subject: str = "이 계열",
) -> dict:
    """Place the latest observation in its own distribution.

    Always returns a dict.  ``available`` is False with a reason when the
    reading cannot honestly be made, which keeps the caller from having to
    distinguish "no data" from "zero".
    """
    observations = list(points or [])
    values = [point["value"] for point in observations]
    if len(values) < minimum:
        return {
            "available": False,
            "reason": f"{subject} 이력이 {minimum}개 이상 쌓이면 분포 판정이 "
                      f"가능합니다 (현재 {len(values)})",
            "observations": len(values),
        }
    scope = values if window is None else values[-window:]
    plain = percentile(values[-1], scope)
    # Two declared directions, mirrored: a series whose rise tightens reads
    # inverted, one whose rise supports reads straight through. A series
    # nobody classified gets neither rather than a guessed orientation.
    if direction == indicators.RISK:
        oriented = percentile(values[-1], scope, invert=True)
    elif direction == indicators.SUPPORT:
        oriented = plain
    else:
        oriented = None
    return {
        "available": True,
        "value": values[-1],
        "as_of": observations[-1].get("date"),
        "observations": len(values),
        # ``percentile`` is the plain reading: 100 means the highest this
        # series has been in the window. ``risk_percentile`` re-orients it so
        # 100 means risk-on, and exists only where a rise has an agreed
        # meaning — a series nobody has classified gets no risk framing rather
        # than a guessed one.
        "percentile": plain,
        "risk_percentile": oriented,
        "direction": direction,
        "window": window,
        "window_label": window_label(window, len(values)),
        "method": (
            f"{window_label(window, len(values))} 분포에서의 위치. "
            "임계값이 아니라 계열 자신의 분포를 기준으로 삼습니다."
        ),
    }


LOOKBACK_DAYS = 7


def movement(
    points: list[dict] | None,
    *,
    window: int | None = TRAILING_WINDOW,
    lookback_days: int = LOOKBACK_DAYS,
    minimum: int = MIN_OBSERVATIONS,
) -> dict | None:
    """How far this series moved within its own distribution, in points.

    The lookback is calendar days, not observations: five observations is a
    week for a daily series and five weeks for a weekly one, which would put
    last month's news beside this week's on the same list.

    Both ends are measured against the window *as it stood then*, not today's
    window with an old value dropped into it. Reusing one window makes the two
    readings share almost all their history and understates every move.

    A level that only travels one way barely moves in distribution terms, so
    saturated series fall off this measure without needing a rule.
    """
    from datetime import date, timedelta

    observations = list(points or [])
    if len(observations) < minimum + 1:
        return None
    try:
        cutoff = (
            date.fromisoformat(observations[-1]["date"])
            - timedelta(days=lookback_days)
        ).isoformat()
    except (KeyError, TypeError, ValueError):
        return None
    earlier = [item for item in observations[:-1] if item["date"] <= cutoff]
    if len(earlier) < minimum:
        return None
    values = [item["value"] for item in observations]
    past = [item["value"] for item in earlier]
    now = percentile(values[-1], values if window is None else values[-window:])
    then = percentile(past[-1], past if window is None else past[-window:])
    return {
        "now": now,
        "then": then,
        "change": round(now - then, 1),
        "lookback_days": lookback_days,
        "as_of": observations[-1]["date"],
        "from_date": earlier[-1]["date"],
    }


def movement_for(key: str, *, lookback_days: int = LOOKBACK_DAYS) -> dict | None:
    return movement(
        db.get_indicator_points(key),
        window=window_for(key),
        lookback_days=lookback_days,
    )


def position_for(key: str) -> dict:
    """The distribution reading for a catalogued indicator."""
    catalog = indicators.catalog()
    if key not in catalog:
        return {"available": False, "reason": f"알 수 없는 지표: {key}"}
    spec = catalog[key]
    reading = position(
        db.get_indicator_points(key),
        direction=indicators.risk_direction(key),
        window=window_for(key),
        subject=spec["label"],
    )
    if reading["available"] and key in indicators.SATURATED_LEVELS:
        # Being at the top of the range is usually the signal — a policy rate
        # at a one-year high is exactly that. For these few the level only
        # goes one way, so the percentile is arithmetic rather than news.
        reading["caveat"] = (
            "이 계열은 수준이 한 방향으로만 움직여 분포상 위치가 거의 항상 "
            "극단입니다. 위치보다 변화폭을 보세요."
        )
    return reading
