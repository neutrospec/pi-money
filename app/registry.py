"""Collector registry with cycle-aware freshness and explicit outcomes."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, timedelta

from app import db, history_recovery
from app.collectors import curated, indices, indicators, krx, quotes, watchlist
from app.scheduler import make_collector
from app.timeutil import instant_age_seconds, kst_today


log = logging.getLogger("money")


QUOTE_INTERVAL = int(os.environ.get("QUOTE_INTERVAL", "300"))
INDEX_QUOTE_INTERVAL = int(os.environ.get("INDEX_QUOTE_INTERVAL", "900"))
INDEX_HISTORY_INTERVAL = int(os.environ.get("INDEX_HISTORY_INTERVAL", "21600"))
IND_DAILY_INTERVAL = int(os.environ.get("IND_DAILY_INTERVAL", "21600"))
IND_MONTHLY_INTERVAL = int(os.environ.get("IND_MONTHLY_INTERVAL", "86400"))
IND_QUARTERLY_INTERVAL = int(os.environ.get("IND_QUARTERLY_INTERVAL", "86400"))
EVENT_INTERVAL = int(os.environ.get("EVENT_INTERVAL", "21600"))
KRX_MARKET_INTERVAL = int(os.environ.get("KRX_MARKET_INTERVAL", "86400"))


def _sync_catalog() -> None:
    catalog = indicators.catalog()
    db.upsert_series_catalog([
        {
            "key": key,
            "label": spec["label"],
            "unit": spec["unit"],
            "category": spec["category"],
            "source": spec["source"],
            "series": spec["series"],
            "frequency": indicators.cycle_of(key),
            "date_kind": spec["date_kind"],
            "max_age_days": spec["max_age_days"],
            "analysis_group": spec["analysis_group"],
            "priority": spec["priority"],
            "is_proxy": int(spec["proxy"]),
            "source_url": spec["source_url"],
            "source_options": spec["source_options"],
        }
        for key, spec in catalog.items()
    ])


def _indicator_deficits(cycles: set[str]) -> list[str]:
    """Return missing/stale keys using each series' publication-aware age."""
    missing = []
    today = kst_today()
    for key in indicators.catalog():
        cycle = indicators.cycle_of(key)
        if cycle not in cycles or indicators.is_collector_fed(key):
            continue
        points = db.get_indicator_points(key)
        if not points:
            missing.append(key)
            continue
        try:
            age_days = (today - date.fromisoformat(points[-1]["date"])).days
        except (KeyError, TypeError, ValueError):
            missing.append(key)
            continue
        if age_days > indicators.freshness_days(key):
            missing.append(key)
    return missing


def _indicators_fresh(cycles: set[str]) -> bool:
    return not _indicator_deficits(cycles)


def _run_indicator_keys(keys: list[str]) -> dict:
    db.init_db()
    _sync_catalog()
    results = indicators.fetch_keys_into_db(keys)
    errors = {key: value for key, value in results.items() if not isinstance(value, int)}
    ok = len(results) - len(errors)
    if ok:
        db.set_meta("last_collect", db.utc_now())
    return {"ok": ok, "total": len(results), "errors": errors}


def _run_indicators(cycles: set[str]) -> dict:
    # Series another collector writes are catalogued for discovery but cannot
    # be fetched one key at a time; requesting them here would fail every run.
    keys = [
        key for key in indicators.catalog()
        if indicators.cycle_of(key) in cycles and not indicators.is_collector_fed(key)
    ]
    return _run_indicator_keys(keys)


def _repair_indicators(cycles: set[str]) -> dict:
    keys = _indicator_deficits(cycles)
    result = _run_indicator_keys(keys)
    errors = dict(result.get("errors") or {})
    for key in _indicator_deficits(cycles):
        if key in keys and key not in errors:
            errors[key] = "source returned data but coverage is still stale or missing"
    return {**result, "errors": errors}


def _quote_deficits() -> list[str]:
    stored = {quote["symbol"]: quote for quote in db.get_quotes()}
    missing = []
    for item in watchlist.watchlist():
        quote = stored.get(item["symbol"])
        if not quote or instant_age_seconds(quote.get("updated")) >= QUOTE_INTERVAL:
            missing.append(item["symbol"])
    return missing


def _quotes_fresh() -> bool:
    return not _quote_deficits()


def _run_quotes(symbols: list[str] | None = None) -> dict:
    db.init_db()
    ok = 0
    errors: dict[str, str] = {}
    allowed = set(symbols) if symbols is not None else None
    items = [
        item for item in watchlist.watchlist()
        if allowed is None or item["symbol"] in allowed
    ]
    for item in items:
        try:
            quote = quotes.quote(item["symbol"])
            if quote is None or quote.get("price") is None:
                raise ValueError("source returned no current price")
            db.save_quote({
                "symbol": item["symbol"],
                "label": item["label"],
                "group_name": item["group"],
                "price": quote["price"],
                "prev_close": quote.get("prev_close"),
                "currency": quote.get("currency"),
                "updated": db.utc_now(),
            })
            ok += 1
        except Exception as exc:  # one symbol must not abort the batch
            errors[item["symbol"]] = str(exc)
    return {"ok": ok, "total": len(items), "errors": errors}


def _repair_quotes() -> dict:
    return _run_quotes(_quote_deficits())


def _index_quote_deficits() -> list[str]:
    stored = db.get_index_quotes()
    missing = []
    for item in indices.index_list():
        quote = stored.get(item["symbol"])
        if not quote or instant_age_seconds(quote.get("updated_at")) >= INDEX_QUOTE_INTERVAL:
            missing.append(item["symbol"])
    return missing


def _index_quotes_fresh() -> bool:
    return not _index_quote_deficits()


def _run_index_quotes(symbols: list[str] | None = None) -> dict:
    db.init_db()
    ok = 0
    errors: dict[str, str] = {}
    allowed = set(symbols) if symbols is not None else None
    items = [
        item for item in indices.index_list()
        if allowed is None or item["symbol"] in allowed
    ]
    for item in items:
        symbol = item["symbol"]
        try:
            quote = indices.quote(symbol)
            if quote is None or quote.get("price") is None:
                raise ValueError("source returned no current price")
            db.save_index_quote({
                "symbol": symbol,
                "price": quote["price"],
                "prev_close": quote.get("prev_close"),
                "currency": quote.get("currency"),
                "session_date": quote.get("session_date"),
                "updated_at": db.utc_now(),
            })
            ok += 1
        except Exception as exc:
            errors[symbol] = str(exc)
    return {"ok": ok, "total": len(items), "errors": errors}


def _repair_index_quotes() -> dict:
    return _run_index_quotes(_index_quote_deficits())


def _history_is_healthy(symbol: str, session: str | None) -> bool:
    """Complete means we hold the provider's latest session, not "recent".

    Wall-clock age cannot separate a market holiday from a failed collection,
    so a day-count tolerance either forgives real gaps or chases holidays
    forever.  The quote collector already records the session the provider
    itself last published; matching it is exact and costs nothing.  The
    tolerance below is only the fallback for a symbol whose quote has not
    been collected yet.
    """
    two_years_ago = (kst_today() - timedelta(days=730)).isoformat()
    latest = db.index_latest_date(symbol)
    if not latest or db.index_point_count(symbol, start=two_years_ago) < 250:
        return False
    if session:
        return latest >= session
    try:
        return (kst_today() - date.fromisoformat(latest)).days <= 5
    except ValueError:
        return False


def _index_history_deficits() -> list[str]:
    sessions = db.get_index_quotes()
    return [
        item["symbol"] for item in indices.index_list()
        if not _history_is_healthy(
            item["symbol"], (sessions.get(item["symbol"]) or {}).get("session_date")
        )
    ]


def _index_history_fresh() -> bool:
    return not _index_history_deficits()


def _run_index_history(symbols: list[str] | None = None) -> dict:
    db.init_db()
    ok = 0
    errors: dict[str, str] = {}
    allowed = set(symbols) if symbols is not None else None
    items = [
        item for item in indices.index_list()
        if allowed is None or item["symbol"] in allowed
    ]
    sessions = db.get_index_quotes()
    for item in items:
        symbol = item["symbol"]
        try:
            session = (sessions.get(symbol) or {}).get("session_date")
            if _history_is_healthy(symbol, session):
                points = indices.recent_history(symbol, days=30)
                db.save_index_points(symbol, points)
            else:
                points = indices.full_history(symbol, years=20)
                if len(points) < 250:
                    raise ValueError(f"daily backfill too short: {len(points)} points")
                db.replace_index_points(symbol, points)
            ok += 1
        except Exception as exc:
            errors[symbol] = str(exc)
    return {"ok": ok, "total": len(items), "errors": errors}


def _repair_index_history() -> dict:
    return _run_index_history(_index_history_deficits())


def _krx_market_fresh() -> bool:
    return all(
        db.market_run_status("krx", spec["dataset"], day) in {"success", "empty"}
        for spec in krx.dataset_specs()
        for day in krx.catchup_dates()
    )


def _run_krx_market() -> dict:
    """Collect provider-wide tables, bounded by endpoint and run row budgets."""
    db.init_db()
    specs = krx.dataset_specs()
    days = krx.catchup_dates()
    maximum = max(1, int(os.environ.get("KRX_MAX_ROWS_PER_RUN", "400000")))
    stored_rows = 0
    ok = 0
    errors: dict[str, str] = {}
    budget_exhausted = False
    for spec in specs:
        dataset_ok = True
        blocked = history_recovery.krx_dataset_blocked(spec["dataset"])
        if blocked:
            errors[f"{spec['dataset']}@blocked"] = (
                blocked.get("reason") or "KRX dataset authorization is blocked"
            )
            continue
        for day in days:
            if db.market_run_status("krx", spec["dataset"], day) in {"success", "empty"}:
                continue
            key = f"{spec['dataset']}@{day}"
            try:
                raw_rows = krx.fetch_dataset(spec, day)
                if stored_rows + len(raw_rows) > maximum:
                    raise RuntimeError(
                        f"KRX run row budget exceeded: "
                        f"{stored_rows + len(raw_rows)} > {maximum}"
                    )
                rows = krx.normalize_rows(spec, raw_rows, day)
                db.save_market_batch("krx", spec["dataset"], day, rows)
                stored_rows += len(rows)
            except Exception as exc:
                message = str(exc)
                db.record_market_run(
                    "krx", spec["dataset"], day,
                    status="error", error=message,
                )
                if history_recovery.is_access_error(message):
                    history_recovery.mark_krx_dataset_blocked(
                        spec["dataset"], message
                    )
                errors[key] = message
                dataset_ok = False
                budget_exhausted = "run row budget exceeded" in message
                break
            # Derived series are a bonus on top of stored rows, so a
            # summariser failure is reported without touching the day's
            # recorded success. Running the error path here would make the
            # collector re-fetch rows it already holds and make the recovery
            # ledger call a stored day a gap.
            _store_krx_aggregates(spec, raw_rows, day, errors, key)
        if dataset_ok:
            ok += 1
        if budget_exhausted:
            break
    if budget_exhausted:
        errors["resource_budget"] = (
            "remaining datasets skipped; raise KRX_MAX_ROWS_PER_RUN only after reviewing DB growth"
        )
    return {
        "ok": ok,
        "total": len(specs),
        "errors": errors,
        "rows": stored_rows,
        "scope": os.environ.get("KRX_MARKET_SCOPE", "balanced"),
    }


def _store_krx_aggregates(
    spec: dict,
    raw_rows: list[dict],
    day: str,
    errors: dict[str, str] | None = None,
    key: str | None = None,
) -> None:
    """Persist market-wide statistics no individual row carries.

    These land in ``indicator_points`` rather than ``market_daily`` because a
    put/call ratio is a property of the market, not of an instrument.  The
    contracts they are derived from are stored in full by the caller, and a
    failure here is reported without disowning them.
    """
    history_recovery.store_krx_aggregates(spec, raw_rows, day, errors, key)


def _event_version() -> str:
    payload = json.dumps(curated.load(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _event_snapshot(events: list[dict]) -> list[tuple]:
    fields = (
        "date", "time", "country", "title", "impact", "note", "source",
        "source_date", "source_time", "source_timezone", "source_url",
    )
    return sorted(tuple(event.get(field) for field in fields) for event in events)


def _events_fresh() -> bool:
    if db.get_meta("event_catalog_version") != _event_version():
        return False
    with db.get_conn() as conn:
        stored = [dict(row) for row in conn.execute(
            "SELECT date, time, country, title, impact, note, source, "
            "source_date, source_time, source_timezone, source_url "
            "FROM events WHERE source='curated'"
        )]
    return _event_snapshot(stored) == _event_snapshot(curated.load())


def _run_events() -> dict:
    db.init_db()
    events = curated.load()
    db.replace_events(events)
    db.set_meta("event_catalog_version", _event_version())
    return {"ok": len(events), "total": len(events), "errors": {}}


def build_scheduler():
    """Construct all jobs. API handlers never call external data sources."""
    from app.scheduler import Scheduler

    db.init_db()
    _sync_catalog()
    scheduler = Scheduler()
    scheduler.register(make_collector(
        "events", EVENT_INTERVAL, _run_events,
        is_fresh=_events_fresh, repair=_run_events,
    ))
    scheduler.register(make_collector(
        "indicators_daily", IND_DAILY_INTERVAL,
        lambda: _run_indicators({"D", "W"}),
        is_fresh=lambda: _indicators_fresh({"D", "W"}),
        repair=lambda: _repair_indicators({"D", "W"}),
    ))
    scheduler.register(make_collector(
        "indicators_monthly", IND_MONTHLY_INTERVAL,
        lambda: _run_indicators({"M"}),
        is_fresh=lambda: _indicators_fresh({"M"}),
        repair=lambda: _repair_indicators({"M"}),
    ))
    scheduler.register(make_collector(
        "indicators_quarterly", IND_QUARTERLY_INTERVAL,
        lambda: _run_indicators({"Q", "A"}),
        is_fresh=lambda: _indicators_fresh({"Q", "A"}),
        repair=lambda: _repair_indicators({"Q", "A"}),
    ))
    scheduler.register(make_collector(
        "quotes", QUOTE_INTERVAL, _run_quotes,
        is_fresh=_quotes_fresh, repair=_repair_quotes,
    ))
    scheduler.register(make_collector(
        "index_quotes", INDEX_QUOTE_INTERVAL, _run_index_quotes,
        is_fresh=_index_quotes_fresh, repair=_repair_index_quotes,
    ))
    scheduler.register(make_collector(
        "index_history", INDEX_HISTORY_INTERVAL, _run_index_history,
        is_fresh=_index_history_fresh, repair=_repair_index_history,
    ))
    if krx.enabled():
        scheduler.register(make_collector(
            "krx_market", KRX_MARKET_INTERVAL, _run_krx_market,
            is_fresh=_krx_market_fresh, repair=_run_krx_market,
            error_interval=KRX_MARKET_INTERVAL,
        ))
    scheduler.register(make_collector(
        "historical_recovery", history_recovery.HISTORY_INTERVAL,
        history_recovery.run,
        is_fresh=history_recovery.is_settled,
        repair=history_recovery.run,
    ))
    return scheduler
