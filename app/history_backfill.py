"""Reach back past what the collectors' default window fetched.

The collectors ask each provider for three years, and for daily operation that
is right — more would re-download a decade every cycle to learn nothing. But it
silently set the ceiling on what could ever be *verified*: the Korean regime
classifier could only be replayed from 2023-12-18, so its backtest window
contained no sustained bear market and the result could not distinguish a
regularity from a property of one rising stretch.

The providers hold far more than three years, and this module asks for it once.

What can be reached, measured rather than assumed:

===================  ==============  =========================================
series               reaches back    limit
===================  ==============  =========================================
kr_treasury_3y       2006-09         ECOS raw table
kr_corp_bond_3y      2006-09         ECOS raw table
kr_cd_91d            2006-09         ECOS raw table
kr_cp_91d            2006-09         ECOS raw table
kr_vkospi            2010-01         KRX Open API serves the index table from
                                     2010; earlier days return zero rows
us_ig_spread         2023-08         **hard limit.** FRED itself reports
                                     observation_start 2023-08-29 for
                                     BAMLC0A0CM — ICE licenses only a rolling
                                     window. Nothing here can extend it, so
                                     the US classifier's window cannot move.
===================  ==============  =========================================

Why an observed-mode replay over backfilled history is legitimate at all: these
particular series are effectively unrevised at publication. An exchange-computed
volatility index and money-market yields are printed once and stand; today's
2011 value is 2011's 2011 value. The same argument would **not** hold for GDP or
CPI, which are revised for years, and a backfill of those must not be used this
way. That is the whole licence for what this module does, and it is narrow.

One consequence that is not a side effect but the point: ``kr_vkospi`` is a
mean-reverting level, so ``normalize`` scores it against its *full* history.
Extending that history from 2.5 years to 16 changes today's live reading. The
2026 volatility cut fired on every single trading day of the year because the
"full history" it was measured against was shorter than the level shift inside
it. More data is the honest fix for that; changing the cut would have been
tuning.
"""
from __future__ import annotations

from datetime import date, timedelta

from app import db
from app.collectors import indicators, krx


# ECOS raw table and item code for series whose curated feed is capped at the
# collector's three-year default. Verified against the existing points: 736
# overlapping days, zero disagreements, so these address the same series.
ECOS_DEEP = {
    "kr_treasury_3y": "817Y002/010200000",
    "kr_corp_bond_3y": "817Y002/010300000",
    "kr_cd_91d": "817Y002/010502000",
    "kr_cp_91d": "817Y002/010503000",
}

# KRX serves the derivative index table from here. Earlier days return zero
# rows rather than an error, so the boundary is measured, not documented.
KRX_INDEX_FROM = "2010-01-01"

DEEP_YEARS = 20

# How much overlap disagreement means "this is a different series" rather than
# "this series was revised". The distinction matters because the two need
# opposite handling: a different series must never be written, and a revision
# is exactly what `save_indicator_points` records as a vintage. Refusing on any
# disagreement gets first contact right and everything after it wrong — one
# revised basis point would block a five-thousand-point refetch forever.
CLASH_SHARE_LIMIT = 0.05


def deepen_ecos(key: str, *, years: int = DEEP_YEARS) -> dict:
    """Refetch one ECOS series over its full available history.

    Values that already exist are written back unchanged, so this is an
    extension rather than a replacement — and because ``save_indicator_points``
    only records a vintage when a value *differs*, an unchanged value costs
    nothing in the ledger and a changed one is recorded as a revision.

    That last part is why the disagreement guard is a share and not a count. A
    handful of differing overlap dates is a revision, which is the thing the
    vintage ledger exists to capture; refusing on any disagreement would make
    the second call to this function block on the first real revision and never
    write again. Wholesale disagreement means something else entirely — a
    different item code returning plausible numbers — and that must never be
    written over a live series.
    """
    series = ECOS_DEEP.get(key)
    if not series:
        raise ValueError(f"no deep ECOS mapping for {key}")
    before = db.get_indicator_points(key)
    points = indicators.ecos_raw_series(series, years=years)
    if not points:
        return {"key": key, "added": 0, "reason": "공급자가 아무것도 반환하지 않았습니다"}
    # Disagreement on an overlapping date would mean this is a different
    # series, not a deeper one. Refuse rather than overwrite.
    held = {item["date"]: item["value"] for item in before}
    fetched = {item["date"]: item["value"] for item in points}
    overlap = held.keys() & fetched.keys()
    clashes = [day for day in overlap if abs(held[day] - fetched[day]) > 1e-9]
    share = len(clashes) / len(overlap) if overlap else 0.0
    if share > CLASH_SHARE_LIMIT:
        return {"key": key, "added": 0, "clashes": len(clashes),
                "overlap": len(overlap),
                "reason": f"겹치는 {len(overlap)}일 중 {len(clashes)}일의 값이 "
                          f"다릅니다 ({share:.0%}) — 같은 계열이 아닐 수 있어 "
                          f"쓰지 않았습니다"}
    db.save_indicator_points(key, points, source="ecos_raw")
    after = db.get_indicator_points(key)
    return {
        "key": key,
        "before": len(before), "after": len(after),
        "added": len(after) - len(before),
        # Overlap dates whose value changed: revisions, now in the ledger.
        "revisions": len(clashes),
        "earliest": after[0]["date"] if after else None,
    }


def deepen_krx_index(key: str = "kr_vkospi", *, start: str = KRX_INDEX_FROM,
                     end: str | None = None, progress=None) -> dict:
    """Walk the KRX index table day by day and lift one named index out of it.

    Straight into ``indicator_points`` rather than through ``market_daily``.
    The bulk path would store ~290 rows a day for fourteen years — near a
    million rows — to reach one number per day, and nothing else in the project
    reads those older rows. The extraction uses ``krx.extract_named_indices``,
    the same exact-name matcher the live aggregation uses, so a backfilled
    point is provably the same row as a derived one rather than a lookalike.
    """
    spec = next(item for item in krx.dataset_specs()
                if item["dataset"] == "drvprod_dd_trd")
    held = db.indicator_dates(key)
    first = date.fromisoformat(start)
    last = date.fromisoformat(end) if end else (
        date.fromisoformat(min(held)) if held else date.today()
    )
    points, empty, failed = [], 0, 0
    day = first
    total = (last - first).days
    while day < last:
        # Read the cursor's own weekday before advancing it. Advancing first
        # and then testing ``day.weekday()`` tests *tomorrow*, which skips
        # every Friday (Saturday is 5) and requests every Sunday instead. That
        # is not a small loss: it took out 832 sessions and left a systematic
        # day-of-week hole that no total-count check would reveal.
        iso, weekday = day.isoformat(), day.weekday()
        day += timedelta(days=1)
        if weekday >= 5 or iso in held:
            continue
        try:
            rows = krx.fetch_dataset(spec, iso)
        except Exception:
            failed += 1
            continue
        if not rows:
            # A holiday or a pre-publication day. Recorded as nothing, which
            # is what it is — never as a zero.
            empty += 1
            continue
        found = [item for item in krx.extract_named_indices(rows, iso)
                 if item["indicator"] == key]
        points.extend({"date": item["date"], "value": item["value"]}
                      for item in found)
        if progress and len(points) % 100 == 0 and found:
            progress(len(points), total)
    if points:
        db.save_indicator_points(key, points, source="krx")
    after = db.get_indicator_points(key)
    return {
        "key": key, "added": len(points), "empty_days": empty,
        "failed_days": failed, "after": len(after),
        "earliest": after[0]["date"] if after else None,
    }


def volatility_coherence(symbol: str = "^KS11", key: str = "kr_vkospi") -> dict:
    """Cross-check a backfilled volatility index against realised volatility.

    A backfill has no vintage record — the rows are stamped today — so nothing
    inside this repository proves the old values are the values that were
    published then. What *can* be checked is whether two providers agree: KRX
    computes the volatility index, Yahoo supplies the index prices, and an
    implied-volatility level should track the realised volatility of the thing
    it is written on. Seventeen years of ratios near one is not proof, but a
    year that broke the pattern would be a strong signal to stop.

    This exists because 2026 looked wrong at first sight — VKOSPI median 60.6
    against roughly 15-20 in every prior year, a KOSPI running +167%, and the
    largest single-day move in the twenty-year series. It turned out that
    2026's realised volatility was 63.8%, so the two independent sources agree
    at a ratio of 0.95. An extraordinary year, coherently recorded.
    """
    from statistics import median, pstdev

    prices = db.get_index_points(symbol)
    implied: dict[str, list[float]] = {}
    for point in db.get_indicator_points(key):
        implied.setdefault(point["date"][:4], []).append(point["value"])
    returns: dict[str, list[float]] = {}
    for earlier, later in zip(prices, prices[1:]):
        returns.setdefault(later["date"][:4], []).append(
            (later["value"] / earlier["value"] - 1) * 100
        )
    rows = []
    for year in sorted(implied):
        moves = returns.get(year) or []
        if len(moves) < 60 or not implied[year]:
            continue
        realised = pstdev(moves) * (252 ** 0.5)
        level = median(implied[year])
        rows.append({
            "year": year,
            "realised_vol": round(realised, 1),
            "implied_median": round(level, 1),
            "ratio": round(level / realised, 2) if realised else None,
        })
    ratios = [row["ratio"] for row in rows if row["ratio"]]
    return {
        "symbol": symbol, "key": key, "years": rows,
        "ratio_range": [min(ratios), max(ratios)] if ratios else None,
        # A year outside the range the other years establish is the thing to
        # look at. Named rather than left for the reader to spot.
        "outliers": [
            row["year"] for row in rows
            if row["ratio"] and (row["ratio"] < 0.5 or row["ratio"] > 2.5)
        ],
        "note": (
            "두 공급자가 서로를 검증합니다 — 변동성지수는 KRX, 지수 가격은 "
            "Yahoo. 비율이 1 근처로 유지되면 백필된 값이 그 해의 실제 시장과 "
            "정합한다는 뜻입니다. 증명은 아니지만, 어긋나면 멈출 신호입니다."
        ),
    }
