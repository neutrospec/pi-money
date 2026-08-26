"""Cache-only coverage audit: what we should hold against what we hold.

Staleness alone cannot answer "is this evidence complete?".  A series whose
last observation is recent can still be missing days in the middle, and a
series that looks stale may simply be one its provider has not published yet.
This module separates those cases so the collector can repair what is
repairable and the interface can disclose what is not.

Expected coverage comes from whichever source is authoritative for the series:

``period_start`` (weekly, monthly, quarterly, annual)
    Derivable exactly.  Providers stamp these with the start of the period
    they describe and publish on a fixed stride, so every expected date
    between the first and last observation can be enumerated locally.

``calendar_day`` (standing policy rates)
    Derivable exactly.  The rate is in effect every day, so every date in
    range is expected.

``trading_day`` (traded prices and market yields)
    Not derivable without a trading calendar, which would be a new
    dependency and would still disagree with the provider at the margins.
    The provider's own session list is used instead: the recovery ledger's
    manifest for interior dates, and the last settled session for the tail.
"""
from __future__ import annotations

from datetime import date, timedelta

from app import db, history_recovery
from app.collectors import indicators, indices
from app.timeutil import kst_today


# A gap only becomes actionable once we know the provider has the data.
CONFIRMED = "confirmed"      # provider manifest says it exists; we lack it
CANDIDATE = "candidate"      # the series' own cadence implies it; unverified
UNVERIFIABLE = "unverifiable"  # needs a provider session list we do not hold


def _month_starts(first: date, last: date) -> list[str]:
    out, year, month = [], first.year, first.month
    while (year, month) <= (last.year, last.month):
        out.append(f"{year:04d}-{month:02d}-01")
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return out


def _quarter_starts(first: date, last: date) -> list[str]:
    out, year, month = [], first.year, (first.month - 1) // 3 * 3 + 1
    while (year, month) <= (last.year, (last.month - 1) // 3 * 3 + 1):
        out.append(f"{year:04d}-{month:02d}-01")
        month += 3
        if month > 12:
            month, year = 1, year + 1
    return out


def _stride_days(first: date, last: date, step: int) -> list[str]:
    out, current = [], first
    while current <= last:
        out.append(current.isoformat())
        current += timedelta(days=step)
    return out


def expected_dates(
    held: list[str], *, date_kind: str, frequency: str
) -> list[str] | None:
    """Enumerate the dates this series should hold, or None if underivable."""
    if len(held) < 2:
        return None
    first, last = date.fromisoformat(held[0]), date.fromisoformat(held[-1])
    if date_kind == "calendar_day":
        return _stride_days(first, last, 1)
    if date_kind == "period_start":
        if frequency == "W":
            return _stride_days(first, last, 7)
        if frequency == "M":
            return _month_starts(first, last)
        if frequency == "Q":
            return _quarter_starts(first, last)
        if frequency == "A":
            return [
                f"{year:04d}-01-01" for year in range(first.year, last.year + 1)
            ]
    return None


def _manifest_for(kind: str, target: str) -> set[str]:
    rows = db.list_recovery_targets(
        layer=history_recovery.LAYER, kind=kind, manifest=True
    )
    dates: set[str] = set()
    for row in rows:
        if row["target"] == target:
            dates.update(str(value) for value in row.get("manifest") or [] if value)
    return dates


def _gap_report(held: set[str], expected: set[str], basis: str) -> dict:
    missing = sorted(expected - held)
    return {
        "basis": basis,
        "expected": len(expected),
        "missing_count": len(missing),
        "missing_sample": missing[:12],
        "first_missing": missing[0] if missing else None,
        "last_missing": missing[-1] if missing else None,
    }


def indicator_coverage() -> list[dict]:
    """Audit every catalogued indicator against its own expected shape."""
    catalog = indicators.catalog()
    overview = db.get_indicator_overview()
    manifests = {
        row["target"]: {
            str(value) for value in row.get("manifest") or [] if value
        }
        for row in db.list_recovery_targets(
            layer=history_recovery.LAYER, kind="indicator_history", manifest=True
        )
    }
    today = kst_today()
    report = []
    for key, spec in sorted(catalog.items()):
        held = [point["date"] for point in db.get_indicator_points(key)]
        stored = overview.get(key) or {}
        latest = held[-1] if held else None
        age_days = None
        if latest:
            try:
                age_days = (today - date.fromisoformat(latest)).days
            except ValueError:
                age_days = None
        if not held:
            tail = "missing"
        elif age_days is None:
            tail = "invalid"
        elif age_days > spec["max_age_days"]:
            tail = "stale"
        else:
            tail = "fresh"
        # A freshness override exists to stop the repair loop re-requesting a
        # series its provider has not moved.  It must not also erase the fact
        # that the provider has stalled: a seventeen-month-old CPI is inside
        # its allowance and still something a reader has to be told about.
        normal_age = indicators.DEFAULT_MAX_AGE_DAYS.get(spec["frequency"], 100)
        provider_stalled = bool(
            tail == "fresh"
            and age_days is not None
            and age_days > normal_age
        )

        manifest = manifests.get(key) or set()
        derived = expected_dates(
            held, date_kind=spec["date_kind"], frequency=spec["frequency"]
        )
        if manifest:
            gaps = _gap_report(set(held), manifest, CONFIRMED)
        elif derived is not None:
            gaps = _gap_report(set(held), set(derived), CANDIDATE)
        else:
            gaps = {
                "basis": UNVERIFIABLE,
                "expected": None,
                "missing_count": 0,
                "missing_sample": [],
                "first_missing": None,
                "last_missing": None,
            }
        report.append({
            "key": key,
            "label": spec["label"],
            "source": spec["source"],
            "frequency": spec["frequency"],
            "date_kind": spec["date_kind"],
            "analysis_group": spec["analysis_group"],
            "priority": spec["priority"],
            "observations": len(held),
            "first_date": held[0] if held else None,
            "latest_date": latest,
            "retrieved_at": stored.get("retrieved_at"),
            "age_days": age_days,
            "max_age_days": spec["max_age_days"],
            "tail": tail,
            "provider_stalled": provider_stalled,
            "normal_age_days": normal_age,
            "gaps": gaps,
        })
    return report


def index_coverage() -> list[dict]:
    """Audit index history against the provider's own settled sessions."""
    quotes = db.get_index_quotes()
    report = []
    for spec in indices.index_list():
        symbol = spec["symbol"]
        held = [point["date"] for point in db.get_index_points(symbol)]
        session = (quotes.get(symbol) or {}).get("session_date")
        latest = held[-1] if held else None
        if not held:
            tail = "missing"
        elif session and latest < session:
            tail = "behind_provider"
        elif session:
            tail = "current"
        else:
            tail = "unverified"
        manifest = _manifest_for("index_history", symbol)
        if manifest:
            gaps = _gap_report(set(held), manifest, CONFIRMED)
        else:
            gaps = {
                "basis": UNVERIFIABLE,
                "expected": None,
                "missing_count": 0,
                "missing_sample": [],
                "first_missing": None,
                "last_missing": None,
                "note": "provider session list not yet captured; run historical recovery",
            }
        report.append({
            "symbol": symbol,
            "name": spec["name"],
            "region": spec["region"],
            "observations": len(held),
            "first_date": held[0] if held else None,
            "latest_date": latest,
            "provider_session": session,
            "tail": tail,
            "gaps": gaps,
        })
    return report


def deficits() -> dict:
    """Return the repairable subset, keyed by the collector that owns it."""
    indicator_keys = [
        row["key"] for row in indicator_coverage()
        if row["tail"] in {"missing", "stale", "invalid"}
        or row["gaps"]["basis"] == CONFIRMED and row["gaps"]["missing_count"]
    ]
    index_symbols = [
        row["symbol"] for row in index_coverage()
        if row["tail"] in {"missing", "behind_provider"}
        or row["gaps"]["basis"] == CONFIRMED and row["gaps"]["missing_count"]
    ]
    return {"indicators": indicator_keys, "indices": index_symbols}


def audit() -> dict:
    """Summarize collection completeness for humans and agents alike."""
    series = indicator_coverage()
    index_rows = index_coverage()

    def tally(rows: list[dict], field: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            counts[row[field]] = counts.get(row[field], 0) + 1
        return counts

    confirmed_series = [
        row for row in series
        if row["gaps"]["basis"] == CONFIRMED and row["gaps"]["missing_count"]
    ]
    candidate_series = [
        row for row in series
        if row["gaps"]["basis"] == CANDIDATE and row["gaps"]["missing_count"]
    ]
    confirmed_index = [
        row for row in index_rows
        if row["gaps"]["basis"] == CONFIRMED and row["gaps"]["missing_count"]
    ]
    behind = [row for row in index_rows if row["tail"] == "behind_provider"]
    stalled = [row for row in series if row.get("provider_stalled")]
    core_ready = sum(
        1 for row in series
        if row["priority"] == "core" and row["tail"] == "fresh"
        and not (row["gaps"]["basis"] == CONFIRMED and row["gaps"]["missing_count"])
    )
    core_total = sum(1 for row in series if row["priority"] == "core")
    unresolved = (
        len(confirmed_series) + len(confirmed_index) + len(behind)
        + sum(1 for row in series if row["tail"] in {"missing", "invalid"})
    )
    return {
        "status": "ok" if unresolved == 0 else "incomplete",
        "as_of": kst_today().isoformat(),
        "indicators": {
            "total": len(series),
            "tail": tally(series, "tail"),
            "confirmed_gap_series": len(confirmed_series),
            "candidate_gap_series": len(candidate_series),
            "provider_stalled_series": len(stalled),
            "unverifiable_series": sum(
                1 for row in series if row["gaps"]["basis"] == UNVERIFIABLE
            ),
        },
        "indices": {
            "total": len(index_rows),
            "tail": tally(index_rows, "tail"),
            "confirmed_gap_symbols": len(confirmed_index),
        },
        "core": {"ready": core_ready, "total": core_total},
        "core_ready_pct": (
            round(core_ready / core_total * 100, 1) if core_total else 0.0
        ),
        "unresolved": unresolved,
        "attention": {
            "indicator_gaps": [
                {
                    "key": row["key"],
                    "missing": row["gaps"]["missing_count"],
                    "sample": row["gaps"]["missing_sample"][:5],
                }
                for row in confirmed_series[:20]
            ],
            "indicator_candidates": [
                {
                    "key": row["key"],
                    "missing": row["gaps"]["missing_count"],
                    "sample": row["gaps"]["missing_sample"][:5],
                }
                for row in candidate_series[:20]
            ],
            "index_gaps": [
                {
                    "symbol": row["symbol"],
                    "missing": row["gaps"]["missing_count"],
                    "sample": row["gaps"]["missing_sample"][:5],
                }
                for row in confirmed_index[:20]
            ],
            "indices_behind_provider": [
                {
                    "symbol": row["symbol"],
                    "local": row["latest_date"],
                    "provider": row["provider_session"],
                }
                for row in behind
            ],
            "provider_stalled": [
                {
                    "key": row["key"],
                    "latest": row["latest_date"],
                    "age_days": row["age_days"],
                    "normal_age_days": row["normal_age_days"],
                    "allowance": row["max_age_days"],
                }
                for row in stalled
            ],
            "stale_series": [
                {
                    "key": row["key"],
                    "latest": row["latest_date"],
                    "age_days": row["age_days"],
                    "allowance": row["max_age_days"],
                }
                for row in series if row["tail"] in {"stale", "missing", "invalid"}
            ][:20],
        },
        "method": (
            "period and calendar series are audited against their own derived "
            "cadence; traded series are audited against the provider's session "
            "list, never against a weekday heuristic. provider_stalled marks a "
            "series inside its widened allowance whose provider has stopped "
            "publishing at the normal cadence — not a local collection fault, "
            "but still a limit on the evidence"
        ),
        "cached": True,
    }
