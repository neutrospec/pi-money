"""Second-line historical coverage audit with finite provider attempts.

The first-line scheduler keeps the latest cache usable.  This module performs
one bounded provider snapshot per configured history target, records the dates
the provider actually exposes, and subsequently audits SQLite against that
manifest.  A fixed target never calls a provider forever: it ends as complete,
verified_empty, blocked, or exhausted until its source fingerprint changes or
an operator explicitly resets it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta

from app import db
from app.collectors import indicators, indices, krx
from app.timeutil import kst_today, parse_instant, utc_now


log = logging.getLogger("money")

LAYER = "historical"
# Bumped when the meaning of a stored observation date changes.  A manifest
# recorded under a different convention is not evidence of anything, so the
# fingerprint mismatch re-arms every target and clears it.  v2: provider
# epochs are resolved in the exchange's own timezone rather than UTC.
POLICY_VERSION = "historical-v2"
HISTORY_INTERVAL = max(900, int(os.environ.get("HISTORY_RECOVERY_INTERVAL", "21600")))
MAX_ATTEMPTS = max(1, int(os.environ.get("HISTORY_RECOVERY_MAX_ATTEMPTS", "3")))
RETRY_BACKOFF = max(60, int(os.environ.get("HISTORY_RECOVERY_BACKOFF", "21600")))
MAX_BACKOFF = max(RETRY_BACKOFF, int(os.environ.get("HISTORY_RECOVERY_MAX_BACKOFF", "604800")))
EMPTY_CONFIRMATIONS = max(1, int(os.environ.get("HISTORY_EMPTY_CONFIRMATIONS", "2")))
INDICATOR_CALL_BUDGET = max(0, int(os.environ.get("HISTORY_INDICATOR_CALLS_PER_RUN", "6")))
INDEX_CALL_BUDGET = max(0, int(os.environ.get("HISTORY_INDEX_CALLS_PER_RUN", "3")))
KRX_CALL_BUDGET = max(0, int(os.environ.get("HISTORY_KRX_CALLS_PER_RUN", "3")))
INDEX_YEARS = max(1, min(30, int(os.environ.get("HISTORY_INDEX_YEARS", "20"))))
KRX_BUSINESS_DAYS = max(1, min(20, int(os.environ.get("HISTORY_KRX_BUSINESS_DAYS", "20"))))
KRX_ROW_BUDGET = max(1, int(os.environ.get("HISTORY_KRX_MAX_ROWS_PER_RUN", "200000")))

ACTIVE = {"pending", "retryable", "running"}
TERMINAL = {"complete", "verified_empty", "blocked", "exhausted"}


class ResourceBudgetExceeded(RuntimeError):
    """This run's own row guard stopped the item; the provider did not fail.

    It must stay distinct from a provider error. Attempts are finite, so
    charging a self-imposed budget skip against them would retire a day that
    is perfectly recoverable — a large dataset that keeps landing at the tail
    of a run would be declared exhausted after three sweeps.
    """


def _fingerprint(*parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _credential_fingerprint(source: str) -> str:
    variable = {
        "fred": "FRED_API_KEY",
        "ecos": "ECOS_API_KEY",
        "ecos_raw": "ECOS_API_KEY",
        "krx": "KRX_API_KEY",
    }.get(source)
    if not variable:
        return "public"
    value = os.environ.get(variable, "").strip()
    return _fingerprint(source, value) if value else "missing"


def _target_fingerprint(kind: str, target: str, source_spec: object) -> str:
    source = (
        source_spec.get("source") if isinstance(source_spec, dict)
        else "yahoo" if kind == "index_history" else "krx"
    )
    return _fingerprint(
        POLICY_VERSION, kind, target, source_spec,
        _credential_fingerprint(str(source)),
    )


def _is_due(row: dict, now: datetime | None = None) -> bool:
    if row["status"] not in {"pending", "retryable"}:
        return False
    next_attempt = parse_instant(row.get("next_attempt_at"))
    return next_attempt is None or next_attempt <= (now or utc_now())


def _manifest_dates(row: dict) -> set[str]:
    return {str(value) for value in row.get("manifest") or [] if value}


def _ensure_indicator_targets() -> None:
    today = kst_today()
    start = (today - timedelta(days=365 * 3)).isoformat()
    enrolled: set[str] = set()
    for key, spec in indicators.catalog().items():
        # A series another collector writes has no single-key provider call
        # behind it. Enrolling it spent every attempt on a request that
        # cannot exist, then parked the row at "exhausted", which the ledger
        # reports as needing attention forever.
        if indicators.is_collector_fed(key):
            continue
        enrolled.add(key)
        source_spec = {
            "source": spec["source"],
            "series": spec["series"],
            "frequency": indicators.cycle_of(key),
            "window": "collector-default-3y",
        }
        db.ensure_recovery_target(
            layer=LAYER,
            kind="indicator_history",
            target=key,
            scope="provider_snapshot",
            fingerprint=_target_fingerprint("indicator_history", key, source_spec),
            coverage_start=start,
            coverage_end=today.isoformat(),
        )
    # Rows left by a series that has since been de-catalogued or handed to a
    # collector describe work no attempt can finish, so they are removed
    # rather than re-armed.
    stale = {
        row["target"] for row in db.list_recovery_targets(
            layer=LAYER, kind="indicator_history", manifest=False
        )
    } - enrolled
    if stale:
        db.delete_recovery_targets(
            layer=LAYER, kind="indicator_history", targets=stale
        )


def _ensure_index_targets() -> None:
    today = kst_today()
    start = (today - timedelta(days=365 * INDEX_YEARS)).isoformat()
    for spec in indices.index_list():
        source_spec = {
            "source": "yahoo",
            "symbol": spec["symbol"],
            "years": INDEX_YEARS,
        }
        db.ensure_recovery_target(
            layer=LAYER,
            kind="index_history",
            target=spec["symbol"],
            scope=f"{INDEX_YEARS}y",
            fingerprint=_target_fingerprint(
                "index_history", spec["symbol"], source_spec
            ),
            coverage_start=start,
            coverage_end=today.isoformat(),
        )


def _ensure_krx_targets() -> None:
    if not krx.enabled():
        return
    initial_days = krx.catchup_dates(min(20, KRX_BUSINESS_DAYS))
    for spec in krx.dataset_specs():
        source_spec = {
            "source": "krx",
            "dataset": spec["dataset"],
            "path": spec["path"],
        }
        fingerprint = _target_fingerprint(
            "krx_history", spec["dataset"], source_spec
        )
        access = db.ensure_recovery_target(
            layer=LAYER,
            kind="krx_access",
            target=spec["dataset"],
            scope="authorization",
            fingerprint=fingerprint,
        )
        existing_days = [
            item["scope"] for item in db.list_recovery_targets(
                layer=LAYER, kind="krx_history", manifest=False
            )
            if item["target"] == spec["dataset"]
        ]
        # The generation is anchored at its own newest day, never at today.
        # New sessions stay the first-line collector's job, so this queue
        # cannot grow forward; raising a dataset's configured depth extends
        # it backwards only, which is the one direction a backfill means.
        depth = int(spec.get("history_days") or len(initial_days))
        anchor = max(existing_days) if existing_days else initial_days[-1]
        days = sorted(set(existing_days) | set(krx.history_dates(depth, anchor)))
        authorized = False
        for day in days:
            row = db.ensure_recovery_target(
                layer=LAYER,
                kind="krx_history",
                target=spec["dataset"],
                scope=day,
                fingerprint=_fingerprint(fingerprint, day),
                coverage_start=day,
                coverage_end=day,
            )
            run_status = db.market_run_status("krx", spec["dataset"], day)
            settled = run_status == "success" or (
                run_status == "empty" and krx.empty_is_final(day)
            )
            if run_status in {"success", "empty"}:
                authorized = True
            if settled:
                db.update_recovery_target(
                    LAYER, "krx_history", spec["dataset"], day,
                    status="complete" if run_status == "success" else "verified_empty",
                    completed_at=row.get("completed_at") or db.utc_now(),
                    next_attempt_at=None,
                    reason="market_run_" + run_status,
                    manifest=[day] if run_status == "success" else [],
                )
            elif access["status"] == "blocked":
                db.update_recovery_target(
                    LAYER, "krx_history", spec["dataset"], day,
                    status="blocked",
                    completed_at=row.get("completed_at") or db.utc_now(),
                    next_attempt_at=None,
                    reason="krx_dataset_authorization_blocked",
                    details={"inherited_from": "krx_access"},
                )
        # A dataset the first-line collector already fetched has proved its
        # authorization. Only _recover_krx used to record that, so a dataset
        # whose every day was completed by first-line collection left its
        # access row pending with no attempt left to make — and one such row
        # is enough to keep the historical layer from ever settling.
        if authorized and access["status"] not in {"complete", "blocked"}:
            db.update_recovery_target(
                LAYER, "krx_access", spec["dataset"], "authorization",
                status="complete",
                next_attempt_at=None,
                completed_at=access.get("completed_at") or db.utc_now(),
                reason="market_run_authorization_verified",
                details={"verified_from": "first_line_market_run"},
            )


def store_krx_aggregates(
    spec: dict,
    raw_rows: list[dict],
    day: str,
    errors: dict[str, str] | None = None,
    key: str | None = None,
) -> int:
    """Persist the market-wide series a bulk KRX table implies.

    Both collection layers call this. A failure here is reported without
    disowning the rows it was derived from: the caller has already stored a
    complete table, and re-fetching it because a summariser broke would make
    the recovery ledger call a stored day a gap.
    """
    try:
        points = krx.derive_aggregate_points(spec, raw_rows, day)
        for point in points:
            db.save_indicator_points(
                point["indicator"],
                [{"date": point["date"], "value": point["value"]}],
                source="krx",
            )
        return len(points)
    except Exception as exc:  # the rows this was derived from remain valid
        if errors is not None and key:
            errors[f"{key}#aggregate"] = f"{type(exc).__name__}: {exc}"
        log.warning("KRX aggregate failed for %s %s: %s", spec["dataset"], day, exc)
        return 0


def _barren_days() -> dict:
    """Stored days that yielded no aggregate, so they are attempted once."""
    try:
        value = json.loads(db.get_meta("krx_aggregate_barren_days") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def rebuild_krx_aggregates(limit_per_dataset: int = 40) -> dict:
    """Derive missing market-wide series from rows we already hold.

    Cache-only, so it belongs in the audit: ``market_daily`` keeps each row's
    original payload, and a day whose table is stored but whose derived series
    is missing needs no provider at all. Only the first-line collector used to
    derive these, so every day the second-line layer recovered stored a
    complete table and silently no series — VKOSPI had five observations
    standing behind twenty days of rows.
    """
    barren = _barren_days()
    repaired: dict[str, int] = {}
    for spec in krx.dataset_specs():
        keys = krx.aggregate_indicator_keys(spec)
        if not keys:
            continue
        dataset = spec["dataset"]
        held = set().union(*(db.indicator_dates(key) for key in keys))
        skip = set(barren.get(dataset) or [])
        gaps = [
            day for day in db.market_dataset_days("krx", dataset)
            if day not in held and day not in skip
        ]
        for day in gaps[:max(1, limit_per_dataset)]:
            rows = db.market_dataset_raw_rows("krx", dataset, day)
            stored = store_krx_aggregates(spec, rows, day)
            if stored:
                repaired[dataset] = repaired.get(dataset, 0) + stored
            else:
                # The table is stored and implies nothing. Recording it stops
                # the audit from re-parsing the same rows every five minutes.
                barren.setdefault(dataset, [])
                if day not in barren[dataset]:
                    barren[dataset].append(day)
    if barren:
        db.set_meta(
            "krx_aggregate_barren_days",
            json.dumps(barren, ensure_ascii=False, sort_keys=True)[:16000],
        )
    return repaired


def ensure_targets() -> None:
    db.init_db()
    _ensure_indicator_targets()
    _ensure_index_targets()
    _ensure_krx_targets()


def _audit_manifest_row(row: dict, current_dates: set[str]) -> None:
    expected = _manifest_dates(row)
    missing = expected - current_dates
    identity = (row["layer"], row["kind"], row["target"], row["scope"])
    if missing:
        db.update_recovery_target(
            *identity,
            status="pending",
            attempts=0,
            empty_confirmations=0,
            completed_at=None,
            next_attempt_at=None,
            reason="local_manifest_gap",
            details={"missing_count": len(missing), "sample": sorted(missing)[:10]},
        )
        return
    # First-line collection can extend history after the one-time snapshot.
    # Adopt those provider-sourced dates locally without another network call.
    lower_bound = row.get("coverage_start")
    in_scope = {
        value for value in current_dates
        if lower_bound is None or value >= lower_bound
    }
    combined = expected | in_scope
    if combined != expected:
        db.update_recovery_target(
            *identity,
            manifest=sorted(combined),
            coverage_start=min(combined) if combined else row.get("coverage_start"),
            coverage_end=max(combined) if combined else row.get("coverage_end"),
            reason=row.get("reason") or "provider_manifest_verified",
        )


def audit_targets() -> dict:
    """Cache-only historical audit; never invokes a provider."""
    ensure_targets()
    for row in db.list_recovery_targets(
        layer=LAYER, kind="indicator_history", statuses={"complete"}, manifest=True
    ):
        current = {point["date"] for point in db.get_indicator_points(row["target"])}
        _audit_manifest_row(row, current)
    for row in db.list_recovery_targets(
        layer=LAYER, kind="index_history", statuses={"complete"}, manifest=True
    ):
        current = {point["date"] for point in db.get_index_points(row["target"])}
        _audit_manifest_row(row, current)
    _ensure_krx_targets()
    rebuilt = rebuild_krx_aggregates()
    if rebuilt:
        log.info("rebuilt KRX aggregates from stored rows: %s", rebuilt)
    return db.recovery_summary(LAYER)


def is_settled() -> bool:
    summary = audit_targets()
    return not any(status in ACTIVE for status in summary["counts"])


def _set_running(row: dict) -> dict:
    return db.update_recovery_target(
        row["layer"], row["kind"], row["target"], row["scope"],
        status="running",
        last_attempt_at=db.utc_now(),
        next_attempt_at=None,
        reason="provider_attempt_started",
    )


def _complete(
    row: dict,
    *,
    status: str = "complete",
    reason: str,
    manifest: list[str] | None = None,
    details: dict | None = None,
) -> dict:
    dates = sorted(set(manifest or []))
    return db.update_recovery_target(
        row["layer"], row["kind"], row["target"], row["scope"],
        status=status,
        attempts=int(row.get("attempts") or 0) + 1,
        coverage_start=min(dates) if dates else row.get("coverage_start"),
        coverage_end=max(dates) if dates else row.get("coverage_end"),
        completed_at=db.utc_now(),
        next_attempt_at=None,
        reason=reason,
        details=details or {},
        manifest=dates,
    )


def _error_kind(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in (
        "http 401", "http 403", "api_key", "not configured", "인증", "미승인",
    )):
        return "blocked"
    if any(token in lowered for token in (
        "no observations", "no usable", "returned no result", "빈 응답",
    )):
        return "empty"
    return "retryable"


def is_access_error(message: str) -> bool:
    return _error_kind(message) == "blocked"


def _fail(row: dict, exc: Exception) -> dict:
    message = str(exc)
    attempts = int(row.get("attempts") or 0) + 1
    empty_confirmations = int(row.get("empty_confirmations") or 0)
    kind = _error_kind(message)
    if kind == "empty":
        empty_confirmations += 1
    if kind == "blocked":
        status, next_attempt, reason = "blocked", None, "provider_access_blocked"
    elif attempts >= MAX_ATTEMPTS or empty_confirmations >= EMPTY_CONFIRMATIONS:
        status, next_attempt, reason = "exhausted", None, (
            "provider_empty_confirmed" if kind == "empty" else "max_attempts_exhausted"
        )
    else:
        delay = min(MAX_BACKOFF, RETRY_BACKOFF * (2 ** max(0, attempts - 1)))
        status = "retryable"
        next_attempt = (utc_now() + timedelta(seconds=delay)).isoformat()
        reason = "provider_empty_unconfirmed" if kind == "empty" else "provider_error"
    return db.update_recovery_target(
        row["layer"], row["kind"], row["target"], row["scope"],
        status=status,
        attempts=attempts,
        empty_confirmations=empty_confirmations,
        completed_at=db.utc_now() if status in TERMINAL else None,
        next_attempt_at=next_attempt,
        reason=reason,
        details={"error": message},
    )


def _recover_indicator(row: dict) -> dict:
    before = {point["date"] for point in db.get_indicator_points(row["target"])}
    data = indicators.fetch_indicator(row["target"])
    series = data.get("series") or []
    if not series:
        raise ValueError("source returned no observations")
    db.save_indicator_points(row["target"], series, source=data["source"])
    provider_dates = {point["date"] for point in series}
    after = {point["date"] for point in db.get_indicator_points(row["target"])}
    missing = provider_dates - after
    if missing:
        raise RuntimeError(f"local save verification failed for {len(missing)} dates")
    return _complete(
        row,
        reason="historical_gaps_filled" if provider_dates - before else "provider_snapshot_matches",
        manifest=sorted(provider_dates),
        details={
            "provider_points": len(provider_dates),
            "added_points": len(provider_dates - before),
            "latest_observation": max(provider_dates),
        },
    )


def _recover_index(row: dict) -> dict:
    before = {point["date"] for point in db.get_index_points(row["target"])}
    series = indices.full_history(row["target"], years=INDEX_YEARS)
    if not series:
        raise ValueError("Yahoo returned no usable close values")
    db.save_index_points(row["target"], series)
    provider_dates = {point["date"] for point in series}
    after = {point["date"] for point in db.get_index_points(row["target"])}
    missing = provider_dates - after
    if missing:
        raise RuntimeError(f"local save verification failed for {len(missing)} dates")
    return _complete(
        row,
        reason="historical_gaps_filled" if provider_dates - before else "provider_snapshot_matches",
        manifest=sorted(provider_dates),
        details={
            "provider_points": len(provider_dates),
            "added_points": len(provider_dates - before),
            "latest_observation": max(provider_dates),
        },
    )


def _krx_spec(dataset: str) -> dict:
    for spec in krx.dataset_specs():
        if spec["dataset"] == dataset:
            return spec
    raise KeyError(f"unknown KRX dataset: {dataset}")


def _block_krx_dataset(row: dict, message: str) -> None:
    access = db.get_recovery_target(
        LAYER, "krx_access", row["target"], "authorization"
    )
    if access:
        db.update_recovery_target(
            LAYER, "krx_access", row["target"], "authorization",
            status="blocked",
            attempts=int(access.get("attempts") or 0) + 1,
            last_attempt_at=db.utc_now(),
            next_attempt_at=None,
            completed_at=db.utc_now(),
            reason="provider_access_blocked",
            details={"error": message, "reset_required": True},
        )
    for target in db.list_recovery_targets(
        layer=LAYER, kind="krx_history", manifest=False
    ):
        if target["target"] != row["target"] or target["status"] in {"complete", "verified_empty"}:
            continue
        db.update_recovery_target(
            LAYER, "krx_history", target["target"], target["scope"],
            status="blocked",
            completed_at=db.utc_now(),
            next_attempt_at=None,
            reason="krx_dataset_authorization_blocked",
            details={"inherited_from": "krx_access"},
        )


def _recover_krx(row: dict, remaining_rows: int) -> tuple[dict, int]:
    spec = _krx_spec(row["target"])
    raw_rows = krx.fetch_dataset(spec, row["scope"])
    if len(raw_rows) > remaining_rows:
        raise ResourceBudgetExceeded(
            f"historical KRX row budget exceeded: {len(raw_rows)} > {remaining_rows}"
        )
    normalized = krx.normalize_rows(spec, raw_rows, row["scope"])
    db.save_market_batch("krx", spec["dataset"], row["scope"], normalized)
    # Store the series the table implies, exactly as first-line collection
    # does. Without this the ledger calls the day complete while the gauge
    # that needs it stays empty.
    store_krx_aggregates(spec, raw_rows, row["scope"])
    access = db.get_recovery_target(
        LAYER, "krx_access", row["target"], "authorization"
    )
    if access:
        db.update_recovery_target(
            LAYER, "krx_access", row["target"], "authorization",
            status="complete",
            attempts=int(access.get("attempts") or 0) + 1,
            last_attempt_at=db.utc_now(),
            next_attempt_at=None,
            completed_at=db.utc_now(),
            reason="provider_access_verified",
            details={"last_verified_day": row["scope"]},
        )
    if not normalized and not krx.empty_is_final(row["scope"]):
        # The exchange may simply not have published this session yet. Calling
        # it verified empty now would retire a trading day for good.
        outcome = db.update_recovery_target(
            row["layer"], row["kind"], row["target"], row["scope"],
            status="pending",
            next_attempt_at=None,
            reason="awaiting_provider_publication",
            details={"rows": 0},
        )
        return outcome, 0
    outcome = _complete(
        row,
        status="complete" if normalized else "verified_empty",
        reason="historical_market_day_filled" if normalized else "provider_verified_empty_day",
        manifest=[row["scope"]] if normalized else [],
        details={"rows": len(normalized)},
    )
    return outcome, len(normalized)


def krx_dataset_blocked(dataset: str) -> dict | None:
    """Return a terminal access gate used by both recovery layers."""
    row = db.get_recovery_target(LAYER, "krx_access", dataset, "authorization")
    return row if row and row.get("status") == "blocked" else None


def mark_krx_dataset_blocked(dataset: str, message: str) -> None:
    """Persist a first-line KRX 401 so neither layer keeps calling it."""
    rows = [
        item for item in db.list_recovery_targets(
            layer=LAYER, kind="krx_history", manifest=False
        )
        if item["target"] == dataset
    ]
    if not rows:
        _ensure_krx_targets()
        rows = [
            item for item in db.list_recovery_targets(
                layer=LAYER, kind="krx_history", manifest=False
            )
            if item["target"] == dataset
        ]
    row = rows[0] if rows else None
    if row is None:
        return
    _block_krx_dataset(row, message)


def _due_rows(kind: str) -> list[dict]:
    rows = db.list_recovery_targets(
        layer=LAYER, kind=kind, statuses={"pending", "retryable"}, manifest=True
    )
    return [row for row in rows if _is_due(row)]


def run() -> dict:
    """Run one resource-bounded historical recovery batch."""
    audit_targets()
    attempted = 0
    ok = 0
    errors: dict[str, str] = {}
    outcomes: list[dict] = []
    remaining_krx_rows = KRX_ROW_BUDGET
    budgets = (
        ("indicator_history", INDICATOR_CALL_BUDGET),
        ("index_history", INDEX_CALL_BUDGET),
        ("krx_history", KRX_CALL_BUDGET),
    )
    for kind, limit in budgets:
        kind_attempts = 0
        for pending in _due_rows(kind):
            if kind_attempts >= limit:
                break
            # A previous item can terminally block other rows in the same
            # dataset. Re-read before every call so the batch snapshot cannot
            # issue duplicate 401 requests.
            current = db.get_recovery_target(
                pending["layer"], pending["kind"],
                pending["target"], pending["scope"], manifest=True,
            )
            if current is None or not _is_due(current):
                continue
            if kind == "krx_history" and krx_dataset_blocked(current["target"]):
                continue
            if kind == "krx_history" and remaining_krx_rows <= 0:
                break
            pending = current
            row = _set_running(pending)
            kind_attempts += 1
            attempted += 1
            key = f"{kind}:{row['target']}@{row['scope']}"
            try:
                if kind == "indicator_history":
                    outcome = _recover_indicator(row)
                elif kind == "index_history":
                    outcome = _recover_index(row)
                else:
                    outcome, used = _recover_krx(row, remaining_krx_rows)
                    remaining_krx_rows -= used
                ok += 1
            except ResourceBudgetExceeded as exc:
                # Hand the row back untouched and stop this kind: the sweep
                # has spent its allowance, not its attempts. The item stays
                # in the pending count, which the caller reports as backlog.
                db.update_recovery_target(
                    row["layer"], row["kind"], row["target"], row["scope"],
                    status="pending",
                    next_attempt_at=None,
                    reason="run_row_budget_deferred",
                    details={"deferred": str(exc)},
                )
                attempted -= 1
                outcomes.append({
                    "kind": kind,
                    "target": row["target"],
                    "scope": row["scope"],
                    "status": "deferred",
                    "reason": "run_row_budget_deferred",
                })
                break
            except Exception as exc:  # one target must not abort the sweep
                outcome = _fail(row, exc)
                if kind == "krx_history" and outcome["status"] == "blocked":
                    _block_krx_dataset(row, str(exc))
                errors[key] = f"{outcome['status']}: {exc}"
            outcomes.append({
                "kind": kind,
                "target": row["target"],
                "scope": row["scope"],
                "status": outcome["status"],
                "reason": outcome.get("reason"),
            })
    summary = audit_targets()
    active_count = sum(summary["counts"].get(status, 0) for status in ACTIVE)
    report = {
        "attempted": attempted,
        "ok": ok,
        "pending": active_count,
        "errors": errors,
        "outcomes": outcomes,
        "summary": summary,
    }
    db.set_meta("last_history_recovery", db.utc_now())
    db.set_meta(
        "last_history_recovery_report",
        json.dumps(report, ensure_ascii=False, sort_keys=True)[:16000],
    )
    return {
        "ok": ok,
        "total": attempted,
        "pending": active_count,
        "errors": errors,
        "report": report,
    }


def status() -> dict:
    summary = db.recovery_summary(LAYER)
    try:
        report = json.loads(db.get_meta("last_history_recovery_report") or "{}")
    except (json.JSONDecodeError, TypeError):
        report = {}
    return {
        **summary,
        "last_run": db.get_meta("last_history_recovery"),
        "policy": {
            "version": POLICY_VERSION,
            "max_attempts": MAX_ATTEMPTS,
            "empty_confirmations": EMPTY_CONFIRMATIONS,
            "interval_seconds": HISTORY_INTERVAL,
            "index_years": INDEX_YEARS,
            "krx_business_days": KRX_BUSINESS_DAYS,
            "provider_call_budget": {
                "indicators": INDICATOR_CALL_BUDGET,
                "indices": INDEX_CALL_BUDGET,
                "krx": KRX_CALL_BUDGET,
            },
        },
        "last_report": report,
        "cached": True,
    }


def reset(*, kind: str | None = None, target: str | None = None) -> int:
    return db.reset_recovery_targets(layer=LAYER, kind=kind, target=target)
