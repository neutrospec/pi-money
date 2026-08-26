"""SQLite storage layer and idempotent schema migrations."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

from app.timeutil import utc_now_iso


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "money.db"
DB_PATH = Path(os.environ.get("MONEY_DB_PATH", DEFAULT_DB_PATH))
SCHEMA_VERSION = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,            -- YYYY-MM-DD in KST
    time TEXT,                     -- HH:MM in KST, NULL if source time is unknown
    country TEXT NOT NULL,
    title TEXT NOT NULL,
    impact TEXT DEFAULT 'medium' CHECK (impact IN ('high', 'medium', 'low')),
    note TEXT DEFAULT '',
    source TEXT DEFAULT 'curated',
    source_date TEXT,
    source_time TEXT,
    source_timezone TEXT,
    source_url TEXT,
    UNIQUE(date, country, title)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS indicator_points (
    indicator TEXT NOT NULL,
    date TEXT NOT NULL,
    value REAL NOT NULL,
    retrieved_at TEXT,
    source TEXT,
    PRIMARY KEY (indicator, date)
);

CREATE TABLE IF NOT EXISTS indicator_vintages (
    indicator TEXT NOT NULL,
    date TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    value REAL NOT NULL,
    source TEXT,
    PRIMARY KEY (indicator, date, retrieved_at)
);

CREATE TABLE IF NOT EXISTS series_catalog (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    unit TEXT NOT NULL,
    category TEXT NOT NULL,
    source TEXT NOT NULL,
    source_series TEXT NOT NULL,
    frequency TEXT NOT NULL,
    -- What a missing calendar day means for this series.  A traded price has
    -- no Sunday observation and never should; a standing policy rate has one
    -- every day it is in effect.  Auditing both with one weekend rule
    -- produces false gaps for the first kind and hides real ones in the
    -- second, so the distinction is stored rather than inferred.
    date_kind TEXT NOT NULL DEFAULT 'trading_day'
        CHECK (date_kind IN ('trading_day', 'calendar_day', 'period_start')),
    max_age_days INTEGER NOT NULL DEFAULT 100,
    analysis_group TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT 'supporting',
    is_proxy INTEGER NOT NULL DEFAULT 0,
    source_url TEXT,
    source_options_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
    symbol TEXT PRIMARY KEY,
    label TEXT,
    group_name TEXT,
    price REAL,
    prev_close REAL,
    currency TEXT,
    updated TEXT
);

CREATE TABLE IF NOT EXISTS index_prices (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    value REAL NOT NULL,
    retrieved_at TEXT,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS index_quotes (
    symbol TEXT PRIMARY KEY,
    price REAL NOT NULL,
    prev_close REAL,
    currency TEXT,
    -- The provider's own most recent session for this symbol, in exchange
    -- local time.  This is the ground truth for "is our daily history
    -- complete?" and costs no extra call: the quote collector already holds
    -- it.  Wall-clock age cannot answer that question because it cannot tell
    -- a market holiday from a collection failure.
    session_date TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_instruments (
    source TEXT NOT NULL,
    dataset TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    market TEXT,
    currency TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source, dataset, symbol)
);

CREATE TABLE IF NOT EXISTS market_daily (
    source TEXT NOT NULL,
    dataset TEXT NOT NULL,
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    name TEXT NOT NULL,
    close REAL,
    change REAL,
    change_pct REAL,
    open REAL,
    high REAL,
    low REAL,
    volume REAL,
    turnover REAL,
    market_cap REAL,
    raw_json TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (source, dataset, symbol, date),
    FOREIGN KEY (source, dataset, symbol)
        REFERENCES market_instruments(source, dataset, symbol)
);

CREATE TABLE IF NOT EXISTS market_dataset_runs (
    source TEXT NOT NULL,
    dataset TEXT NOT NULL,
    date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'empty', 'error')),
    row_count INTEGER NOT NULL DEFAULT 0,
    retrieved_at TEXT NOT NULL,
    error TEXT,
    PRIMARY KEY (source, dataset, date)
);

CREATE TABLE IF NOT EXISTS collect_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'success',
    ok INTEGER NOT NULL,
    total INTEGER NOT NULL,
    duration_ms INTEGER,
    details TEXT
);

CREATE TABLE IF NOT EXISTS collector_state (
    name TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    status TEXT NOT NULL DEFAULT 'never',
    ok INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    error TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS recovery_ledger (
    layer TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    scope TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    empty_confirmations INTEGER NOT NULL DEFAULT 0,
    coverage_start TEXT,
    coverage_end TEXT,
    last_attempt_at TEXT,
    next_attempt_at TEXT,
    completed_at TEXT,
    reason TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    manifest_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (layer, kind, target, scope)
);

CREATE INDEX IF NOT EXISTS idx_indicator_points_date
    ON indicator_points(date);
CREATE INDEX IF NOT EXISTS idx_index_prices_date
    ON index_prices(date);
CREATE INDEX IF NOT EXISTS idx_collect_log_category_id
    ON collect_log(category, id DESC);
CREATE INDEX IF NOT EXISTS idx_market_daily_date
    ON market_daily(date);
CREATE INDEX IF NOT EXISTS idx_market_daily_symbol_date
    ON market_daily(source, symbol, date DESC);
CREATE INDEX IF NOT EXISTS idx_market_instruments_type
    ON market_instruments(source, asset_type, name);
CREATE INDEX IF NOT EXISTS idx_recovery_ledger_status
    ON recovery_ledger(layer, status, next_attempt_at);
"""


def utc_now() -> str:
    """Canonical stored instant. Defined once in :mod:`app.timeutil`."""
    return utc_now_iso()


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _quarter_start(value: str) -> str | None:
    """Convert legacy YYYY-QN-01 values to an ISO quarter-start date."""
    if len(value) != 10 or value[4:6] != "-Q" or value[7:] != "-01":
        return None
    try:
        quarter = int(value[6])
        if quarter not in (1, 2, 3, 4):
            return None
        return f"{value[:4]}-{(quarter - 1) * 3 + 1:02d}-01"
    except ValueError:
        return None


def _normalize_legacy_instants(conn: sqlite3.Connection) -> int:
    """Stamp an explicit UTC offset onto instants written before the convention.

    These rows came from UTC clocks but were stored without an offset, so a
    reader in any other timezone would silently misread them.  Only the
    offset-less ones are touched, which makes the migration idempotent.
    """
    migrated = 0
    for table, column in (
        ("collect_log", "ts"),
        ("collector_state", "last_attempt_at"),
        ("collector_state", "last_success_at"),
        ("indicator_points", "retrieved_at"),
        ("index_prices", "retrieved_at"),
        ("quotes", "updated"),
        ("index_quotes", "updated_at"),
    ):
        if column not in _columns(conn, table):
            continue
        cursor = conn.execute(
            f"UPDATE {table} SET {column} = {column} || '+00:00' "
            f"WHERE {column} IS NOT NULL AND {column} LIKE '____-__-__T%' "
            f"AND {column} NOT LIKE '%+%' AND {column} NOT LIKE '%Z'"
        )
        migrated += int(cursor.rowcount)
    return migrated


def _migrate_legacy_dates(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT indicator, date, value, retrieved_at, source "
        "FROM indicator_points WHERE date GLOB '????-Q?-01'"
    ).fetchall()
    migrated = 0
    for row in rows:
        new_date = _quarter_start(row["date"])
        if not new_date:
            continue
        conn.execute(
            """INSERT INTO indicator_points
               (indicator, date, value, retrieved_at, source)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(indicator, date) DO UPDATE SET
                 value=excluded.value,
                 retrieved_at=COALESCE(excluded.retrieved_at, indicator_points.retrieved_at),
                 source=COALESCE(excluded.source, indicator_points.source)""",
            (row["indicator"], new_date, row["value"], row["retrieved_at"], row["source"]),
        )
        conn.execute(
            "DELETE FROM indicator_points WHERE indicator=? AND date=?",
            (row["indicator"], row["date"]),
        )
        migrated += 1
    return migrated


def init_db() -> None:
    """Create and migrate the database. Safe to call repeatedly."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _add_column(conn, "indicator_points", "retrieved_at TEXT")
        _add_column(conn, "indicator_points", "source TEXT")
        _add_column(conn, "index_prices", "retrieved_at TEXT")
        _add_column(conn, "collect_log", "status TEXT NOT NULL DEFAULT 'success'")
        _add_column(conn, "events", "source_date TEXT")
        _add_column(conn, "events", "source_time TEXT")
        _add_column(conn, "events", "source_timezone TEXT")
        _add_column(conn, "events", "source_url TEXT")
        _add_column(conn, "series_catalog", "analysis_group TEXT NOT NULL DEFAULT ''")
        _add_column(conn, "series_catalog", "priority TEXT NOT NULL DEFAULT 'supporting'")
        _add_column(conn, "series_catalog", "is_proxy INTEGER NOT NULL DEFAULT 0")
        _add_column(conn, "series_catalog", "source_url TEXT")
        _add_column(conn, "series_catalog", "max_age_days INTEGER NOT NULL DEFAULT 100")
        _add_column(
            conn, "series_catalog", "source_options_json TEXT NOT NULL DEFAULT '{}'"
        )
        _add_column(
            conn, "series_catalog",
            "date_kind TEXT NOT NULL DEFAULT 'trading_day'",
        )
        _add_column(conn, "index_quotes", "session_date TEXT")
        normalized = _normalize_legacy_instants(conn)
        migrated = _migrate_legacy_dates(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        for name, count in (
            ("migrated_quarter_dates", migrated),
            ("normalized_legacy_instants", normalized),
        ):
            if count:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (name, str(count)),
                )


def upsert_event(ev: dict) -> None:
    ev = {
        "source_date": None, "source_time": None,
        "source_timezone": None, "source_url": None,
        **ev,
    }
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO events
               (date, time, country, title, impact, note, source,
                source_date, source_time, source_timezone, source_url)
               VALUES (:date, :time, :country, :title, :impact, :note, :source,
                       :source_date, :source_time, :source_timezone, :source_url)
               ON CONFLICT(date, country, title) DO UPDATE SET
                 time=excluded.time, impact=excluded.impact,
                 note=excluded.note, source=excluded.source,
                 source_date=excluded.source_date, source_time=excluded.source_time,
                 source_timezone=excluded.source_timezone, source_url=excluded.source_url""",
            ev,
        )


def replace_events(events: list[dict]) -> None:
    """Atomically replace all curated events."""
    with get_conn() as conn:
        conn.execute("DELETE FROM events WHERE source='curated'")
        normalized = [{
            "source_date": None, "source_time": None,
            "source_timezone": None, "source_url": None,
            **event,
        } for event in events]
        conn.executemany(
            """INSERT INTO events
               (date, time, country, title, impact, note, source,
                source_date, source_time, source_timezone, source_url)
               VALUES (:date, :time, :country, :title, :impact, :note, :source,
                       :source_date, :source_time, :source_timezone, :source_url)""",
            normalized,
        )


def set_meta(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_meta(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def get_reconciliation_state() -> dict:
    """Return the last persisted coverage audit and unresolved actions."""
    last_run = get_meta("last_reconcile")
    try:
        report = json.loads(get_meta("last_reconcile_report") or "[]")
    except (json.JSONDecodeError, TypeError):
        report = []
    if not isinstance(report, list):
        report = []
    pending = []
    for item in report:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        if action in {"backoff", "audit_error", "busy", "no_audit"}:
            pending.append(item)
        elif action == "repaired" and item.get("status") != "success":
            pending.append(item)
    return {
        "status": "never" if not last_run else ("pending" if pending else "ok"),
        "last_run": last_run,
        "pending": pending,
        "report": report,
    }


def ensure_recovery_target(
    *,
    layer: str,
    kind: str,
    target: str,
    scope: str,
    fingerprint: str,
    coverage_start: str | None = None,
    coverage_end: str | None = None,
) -> dict:
    """Create a persistent recovery target or re-arm it after policy changes."""
    now = utc_now()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM recovery_ledger "
            "WHERE layer=? AND kind=? AND target=? AND scope=?",
            (layer, kind, target, scope),
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO recovery_ledger
                   (layer, kind, target, scope, fingerprint, status,
                    coverage_start, coverage_end, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    layer, kind, target, scope, fingerprint,
                    coverage_start, coverage_end, now,
                ),
            )
        elif row["fingerprint"] != fingerprint:
            conn.execute(
                """UPDATE recovery_ledger SET
                     fingerprint=?, status='pending', attempts=0,
                     empty_confirmations=0, coverage_start=?, coverage_end=?,
                     last_attempt_at=NULL, next_attempt_at=NULL,
                     completed_at=NULL, reason='policy_or_source_changed',
                     details_json='{}', manifest_json='[]', updated_at=?
                   WHERE layer=? AND kind=? AND target=? AND scope=?""",
                (
                    fingerprint, coverage_start, coverage_end, now,
                    layer, kind, target, scope,
                ),
            )
        elif row["coverage_start"] is None and coverage_start is not None:
            conn.execute(
                """UPDATE recovery_ledger SET coverage_start=?, coverage_end=?,
                     updated_at=?
                   WHERE layer=? AND kind=? AND target=? AND scope=?""",
                (
                    coverage_start, coverage_end, now,
                    layer, kind, target, scope,
                ),
            )
        current = conn.execute(
            "SELECT * FROM recovery_ledger "
            "WHERE layer=? AND kind=? AND target=? AND scope=?",
            (layer, kind, target, scope),
        ).fetchone()
    return _decode_recovery_row(current)


def _decode_recovery_row(row: sqlite3.Row | dict | None, *, manifest: bool = True) -> dict:
    if row is None:
        return {}
    item = dict(row)
    for field, default in (("details_json", {}), ("manifest_json", [])):
        try:
            item[field[:-5]] = json.loads(item.pop(field) or json.dumps(default))
        except (json.JSONDecodeError, TypeError):
            item[field[:-5]] = default
    if not manifest:
        item.pop("manifest", None)
    return item


def get_recovery_target(
    layer: str, kind: str, target: str, scope: str,
    *, manifest: bool = True,
) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM recovery_ledger "
            "WHERE layer=? AND kind=? AND target=? AND scope=?",
            (layer, kind, target, scope),
        ).fetchone()
    return _decode_recovery_row(row, manifest=manifest) if row else None


def list_recovery_targets(
    *,
    layer: str | None = None,
    kind: str | None = None,
    statuses: set[str] | None = None,
    manifest: bool = False,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if layer:
        clauses.append("layer=?")
        params.append(layer)
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(sorted(statuses))
    query = "SELECT * FROM recovery_ledger"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY kind, target, scope"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_decode_recovery_row(row, manifest=manifest) for row in rows]


def update_recovery_target(
    layer: str,
    kind: str,
    target: str,
    scope: str,
    **changes,
) -> dict:
    """Update whitelisted recovery fields, including explicit NULL values."""
    allowed = {
        "fingerprint", "status", "attempts", "empty_confirmations",
        "coverage_start", "coverage_end", "last_attempt_at", "next_attempt_at",
        "completed_at", "reason", "details", "manifest",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unknown recovery fields: {', '.join(sorted(unknown))}")
    values = dict(changes)
    if "details" in values:
        values["details_json"] = json.dumps(
            values.pop("details") or {}, ensure_ascii=False, separators=(",", ":")
        )
    if "manifest" in values:
        values["manifest_json"] = json.dumps(
            values.pop("manifest") or [], ensure_ascii=False, separators=(",", ":")
        )
    values["updated_at"] = utc_now()
    assignments = ", ".join(f"{field}=?" for field in values)
    params = list(values.values()) + [layer, kind, target, scope]
    with get_conn() as conn:
        cursor = conn.execute(
            f"UPDATE recovery_ledger SET {assignments} "
            "WHERE layer=? AND kind=? AND target=? AND scope=?",
            params,
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown recovery target: {layer}/{kind}/{target}/{scope}")
        row = conn.execute(
            "SELECT * FROM recovery_ledger "
            "WHERE layer=? AND kind=? AND target=? AND scope=?",
            (layer, kind, target, scope),
        ).fetchone()
    return _decode_recovery_row(row)


def reset_recovery_targets(
    *, layer: str = "historical", kind: str | None = None, target: str | None = None,
) -> int:
    """Explicitly re-arm terminal targets after credentials or policy are fixed."""
    clauses = ["layer=?"]
    params: list = [layer]
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    if target:
        clauses.append("target=?")
        params.append(target)
    now = utc_now()
    with get_conn() as conn:
        cursor = conn.execute(
            # The manifest is cleared with the rest: a provider snapshot kept
            # across a reset would keep asserting coverage the operator just
            # declared untrustworthy.
            "UPDATE recovery_ledger SET status='pending', attempts=0, "
            "empty_confirmations=0, last_attempt_at=NULL, next_attempt_at=NULL, "
            "completed_at=NULL, reason='manual_reset', details_json='{}', "
            "manifest_json='[]', updated_at=? WHERE " + " AND ".join(clauses),
            [now, *params],
        )
    return int(cursor.rowcount)


def recovery_summary(layer: str = "historical", *, item_limit: int = 25) -> dict:
    rows = list_recovery_targets(layer=layer, manifest=False)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    active = {"pending", "retryable", "running"}
    attention = {"blocked", "exhausted"}
    status = (
        "never" if not rows else
        "pending" if any(row["status"] in active for row in rows) else
        "attention" if any(row["status"] in attention for row in rows) else
        "ok"
    )
    priority = {
        "blocked": 0, "exhausted": 1, "retryable": 2,
        "running": 3, "pending": 4, "verified_empty": 5,
    }
    visible = sorted([
        row for row in rows
        if row["status"] in active | attention | {"verified_empty"}
    ], key=lambda row: (
        priority.get(row["status"], 9), row["kind"], row["target"], row["scope"]
    ))[:max(1, item_limit)]
    return {
        "status": status,
        "total": len(rows),
        "counts": counts,
        "items": visible,
        "terminal_statuses": ["complete", "verified_empty", "blocked", "exhausted"],
    }


def get_events(start: str, end: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE date BETWEEN ? AND ? ORDER BY date, time",
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


def save_indicator_points(indicator: str, points: list[dict], source: str | None = None) -> None:
    """Upsert current points and retain only changed values as vintages."""
    if not points:
        return
    retrieved_at = utc_now()
    with get_conn() as conn:
        for point in points:
            old = conn.execute(
                "SELECT value FROM indicator_points WHERE indicator=? AND date=?",
                (indicator, point["date"]),
            ).fetchone()
            if old is None or float(old["value"]) != float(point["value"]):
                conn.execute(
                    """INSERT OR IGNORE INTO indicator_vintages
                       (indicator, date, retrieved_at, value, source)
                       VALUES (?, ?, ?, ?, ?)""",
                    (indicator, point["date"], retrieved_at, point["value"], source),
                )
            conn.execute(
                """INSERT INTO indicator_points
                   (indicator, date, value, retrieved_at, source)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(indicator, date) DO UPDATE SET
                     value=excluded.value, retrieved_at=excluded.retrieved_at,
                     source=excluded.source""",
                (indicator, point["date"], point["value"], retrieved_at, source),
            )


def get_indicator_points(
    indicator: str,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    q = "SELECT date, value FROM indicator_points WHERE indicator=?"
    params: list = [indicator]
    if start:
        q += " AND date >= ?"
        params.append(start)
    if end:
        q += " AND date <= ?"
        params.append(end)
    q += " ORDER BY date"
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_indicator_tails(
    indicators: list[str], limit: int = 2
) -> dict[str, list[dict]]:
    """Return the last N observations per indicator in one query.

    A dashboard needs a latest value and the one before it, not the whole
    series.  Loading full histories for a page of tiles costs more than every
    other part of the render combined.
    """
    if not indicators:
        return {}
    placeholders = ",".join("?" for _ in indicators)
    with get_conn() as conn:
        rows = conn.execute(
            f"""WITH ranked AS (
                    SELECT indicator, date, value,
                           ROW_NUMBER() OVER (
                               PARTITION BY indicator ORDER BY date DESC
                           ) AS position
                    FROM indicator_points
                    WHERE indicator IN ({placeholders})
                )
                SELECT indicator, date, value FROM ranked
                WHERE position <= ?
                ORDER BY indicator, date""",
            [*indicators, max(1, min(limit, 400))],
        ).fetchall()
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row["indicator"], []).append(
            {"date": row["date"], "value": row["value"]}
        )
    return out


def index_freshness_summary() -> dict:
    """Compare each index's stored history against the provider's session."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT q.symbol, q.session_date,
                      (SELECT MAX(p.date) FROM index_prices p
                        WHERE p.symbol = q.symbol) AS latest
               FROM index_quotes q"""
        ).fetchall()
    return {row["symbol"]: dict(row) for row in rows}


def get_indicator_overview() -> dict[str, dict]:
    """Return one collection/coverage row per stored indicator."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT indicator, MAX(date) AS max_date, COUNT(*) AS n, "
            "MAX(retrieved_at) AS retrieved_at "
            "FROM indicator_points GROUP BY indicator"
        ).fetchall()
    return {row["indicator"]: dict(row) for row in rows}


def upsert_series_catalog(items: Iterable[dict]) -> None:
    now = utc_now()
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO series_catalog
               (key, label, unit, category, source, source_series, frequency,
                date_kind, max_age_days, analysis_group, priority, is_proxy,
                source_url, source_options_json, updated_at)
               VALUES (:key, :label, :unit, :category, :source, :series, :frequency,
                       :date_kind, :max_age_days, :analysis_group, :priority,
                       :is_proxy, :source_url, :source_options_json, :updated_at)
               ON CONFLICT(key) DO UPDATE SET
                 label=excluded.label, unit=excluded.unit, category=excluded.category,
                 source=excluded.source, source_series=excluded.source_series,
                 frequency=excluded.frequency, date_kind=excluded.date_kind,
                 max_age_days=excluded.max_age_days,
                 analysis_group=excluded.analysis_group, priority=excluded.priority,
                 is_proxy=excluded.is_proxy, source_url=excluded.source_url,
                 source_options_json=excluded.source_options_json,
                 updated_at=excluded.updated_at""",
            [{
                **item,
                "source_options_json": json.dumps(
                    item.get("source_options", {}),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "updated_at": now,
            } for item in items],
        )


def save_quote(quote: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO quotes (symbol, label, group_name, price, prev_close, currency, updated)
               VALUES (:symbol, :label, :group_name, :price, :prev_close, :currency, :updated)
               ON CONFLICT(symbol) DO UPDATE SET
                 label=excluded.label, group_name=excluded.group_name, price=excluded.price,
                 prev_close=excluded.prev_close, currency=excluded.currency, updated=excluded.updated""",
            quote,
        )


def get_quotes() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM quotes ORDER BY group_name, symbol").fetchall()
    return [dict(r) for r in rows]


def get_quote(symbol: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM quotes WHERE symbol=?", (symbol,)).fetchone()
    return dict(row) if row else None


def save_index_quote(quote: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO index_quotes
               (symbol, price, prev_close, currency, session_date, updated_at)
               VALUES (:symbol, :price, :prev_close, :currency, :session_date,
                       :updated_at)
               ON CONFLICT(symbol) DO UPDATE SET
                 price=excluded.price, prev_close=excluded.prev_close,
                 currency=excluded.currency,
                 session_date=COALESCE(excluded.session_date, index_quotes.session_date),
                 updated_at=excluded.updated_at""",
            {"session_date": None, **quote},
        )


def get_index_quotes() -> dict[str, dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM index_quotes").fetchall()
    return {row["symbol"]: dict(row) for row in rows}


def log_collect(
    category: str,
    ok: int,
    total: int,
    duration_ms: int,
    details: str = "",
    status: str = "success",
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO collect_log
               (ts, category, status, ok, total, duration_ms, details)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (utc_now(), category, status, ok, total, duration_ms, details),
        )


def get_collect_log(category: str | None = None, limit: int = 100) -> list[dict]:
    q = "SELECT * FROM collect_log"
    params: list = []
    if category:
        q += " WHERE category=?"
        params.append(category)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 1000)))
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def set_collector_state(
    name: str,
    *,
    status: str,
    ok: int = 0,
    total: int = 0,
    duration_ms: int | None = None,
    error: str | None = None,
    details: str | None = None,
    success: bool = False,
) -> None:
    now = utc_now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO collector_state
               (name, last_attempt_at, last_success_at, status, ok, total,
                duration_ms, error, details)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 last_attempt_at=excluded.last_attempt_at,
                 last_success_at=CASE WHEN ? THEN excluded.last_success_at
                                      ELSE collector_state.last_success_at END,
                 status=excluded.status, ok=excluded.ok, total=excluded.total,
                 duration_ms=excluded.duration_ms, error=excluded.error,
                 details=excluded.details""",
            (
                name, now, now if success else None, status, ok, total,
                duration_ms, error, details, int(success),
            ),
        )


def get_collector_state(name: str | None = None) -> list[dict] | dict | None:
    with get_conn() as conn:
        if name:
            row = conn.execute("SELECT * FROM collector_state WHERE name=?", (name,)).fetchone()
            return dict(row) if row else None
        rows = conn.execute("SELECT * FROM collector_state ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def save_index_points(symbol: str, points: list[dict]) -> None:
    if not points:
        return
    retrieved_at = utc_now()
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO index_prices (symbol, date, value, retrieved_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(symbol, date) DO UPDATE SET
                 value=excluded.value, retrieved_at=excluded.retrieved_at""",
            [(symbol, p["date"], p["value"], retrieved_at) for p in points],
        )


def replace_index_points(symbol: str, points: list[dict]) -> None:
    """Replace one series only after a non-empty replacement was fetched."""
    if not points:
        raise ValueError("refusing to replace index history with no points")
    retrieved_at = utc_now()
    with get_conn() as conn:
        conn.execute("DELETE FROM index_prices WHERE symbol=?", (symbol,))
        conn.executemany(
            "INSERT INTO index_prices(symbol, date, value, retrieved_at) VALUES (?, ?, ?, ?)",
            [(symbol, p["date"], p["value"], retrieved_at) for p in points],
        )


def index_latest_date(symbol: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(date) AS d FROM index_prices WHERE symbol=?", (symbol,)
        ).fetchone()
    return row["d"] if row else None


def index_point_count(symbol: str, start: str | None = None) -> int:
    q = "SELECT COUNT(*) AS n FROM index_prices WHERE symbol=?"
    params: list = [symbol]
    if start:
        q += " AND date>=?"
        params.append(start)
    with get_conn() as conn:
        row = conn.execute(q, params).fetchone()
    return int(row["n"])


def get_index_points(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    q = "SELECT date, value FROM index_prices WHERE symbol=?"
    params: list = [symbol]
    if start:
        q += " AND date>=?"
        params.append(start)
    if end:
        q += " AND date<=?"
        params.append(end)
    q += " ORDER BY date"
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def market_run_status(source: str, dataset: str, day: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM market_dataset_runs "
            "WHERE source=? AND dataset=? AND date=?",
            (source, dataset, day),
        ).fetchone()
    return row["status"] if row else None


def record_market_run(
    source: str,
    dataset: str,
    day: str,
    *,
    status: str,
    row_count: int = 0,
    error: str | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO market_dataset_runs
               (source, dataset, date, status, row_count, retrieved_at, error)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, dataset, date) DO UPDATE SET
                 status=excluded.status, row_count=excluded.row_count,
                 retrieved_at=excluded.retrieved_at, error=excluded.error""",
            (source, dataset, day, status, row_count, utc_now(), error),
        )


def save_market_batch(
    source: str,
    dataset: str,
    requested_day: str,
    rows: list[dict],
) -> None:
    """Atomically save a provider-wide daily table and its discovered universe."""
    now = utc_now()
    status = "success" if rows else "empty"
    with get_conn() as conn:
        for row in rows:
            instrument = {
                "source": source,
                "dataset": dataset,
                "symbol": row["symbol"],
                "name": row["name"],
                "asset_type": row["asset_type"],
                "market": row.get("market"),
                "currency": row.get("currency"),
                "seen": row["date"],
                "metadata_json": json.dumps(
                    row.get("metadata", {}), ensure_ascii=False, separators=(",", ":")
                ),
            }
            conn.execute(
                """INSERT INTO market_instruments
                   (source, dataset, symbol, name, asset_type, market, currency,
                    first_seen, last_seen, metadata_json)
                   VALUES (:source, :dataset, :symbol, :name, :asset_type, :market,
                           :currency, :seen, :seen, :metadata_json)
                   ON CONFLICT(source, dataset, symbol) DO UPDATE SET
                     name=excluded.name, asset_type=excluded.asset_type,
                     market=excluded.market, currency=excluded.currency,
                     first_seen=MIN(market_instruments.first_seen, excluded.first_seen),
                     last_seen=MAX(market_instruments.last_seen, excluded.last_seen),
                     metadata_json=excluded.metadata_json""",
                instrument,
            )
            conn.execute(
                """INSERT INTO market_daily
                   (source, dataset, symbol, date, name, close, change, change_pct,
                    open, high, low, volume, turnover, market_cap, raw_json, retrieved_at)
                   VALUES (:source, :dataset, :symbol, :date, :name, :close, :change,
                           :change_pct, :open, :high, :low, :volume, :turnover,
                           :market_cap, :raw_json, :retrieved_at)
                   ON CONFLICT(source, dataset, symbol, date) DO UPDATE SET
                     name=excluded.name, close=excluded.close, change=excluded.change,
                     change_pct=excluded.change_pct, open=excluded.open,
                     high=excluded.high, low=excluded.low, volume=excluded.volume,
                     turnover=excluded.turnover, market_cap=excluded.market_cap,
                     raw_json=excluded.raw_json, retrieved_at=excluded.retrieved_at""",
                {
                    "source": source,
                    "dataset": dataset,
                    "retrieved_at": now,
                    **row,
                    "raw_json": json.dumps(
                        row.get("raw", {}), ensure_ascii=False, separators=(",", ":")
                    ),
                },
            )
        conn.execute(
            """INSERT INTO market_dataset_runs
               (source, dataset, date, status, row_count, retrieved_at, error)
               VALUES (?, ?, ?, ?, ?, ?, NULL)
               ON CONFLICT(source, dataset, date) DO UPDATE SET
                 status=excluded.status, row_count=excluded.row_count,
                 retrieved_at=excluded.retrieved_at, error=NULL""",
            (source, dataset, requested_day, status, len(rows), now),
        )


def market_overview(source: str | None = None) -> dict:
    where = " WHERE source=?" if source else ""
    params = [source] if source else []
    with get_conn() as conn:
        instruments = conn.execute(
            "SELECT COUNT(*) AS n FROM market_instruments" + where, params
        ).fetchone()["n"]
        daily = conn.execute(
            "SELECT COUNT(*) AS n FROM market_daily" + where, params
        ).fetchone()["n"]
        datasets = conn.execute(
            "SELECT COUNT(DISTINCT dataset) AS n FROM market_dataset_runs" + where, params
        ).fetchone()["n"]
        latest = conn.execute(
            "SELECT MAX(date) AS d FROM market_dataset_runs "
            + ("WHERE source=? AND status IN ('success','empty')" if source else
               "WHERE status IN ('success','empty')"),
            params,
        ).fetchone()["d"]
    return {
        "source": source,
        "instruments": int(instruments),
        "daily_rows": int(daily),
        "datasets": int(datasets),
        "latest_date": latest,
    }


def get_market_instruments(
    *,
    source: str | None = None,
    dataset: str | None = None,
    asset_type: str | None = None,
    query: str | None = None,
    limit: int = 500,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    for column, value in (("source", source), ("dataset", dataset), ("asset_type", asset_type)):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    if query:
        clauses.append("(symbol LIKE ? OR name LIKE ?)")
        pattern = f"%{query}%"
        params.extend((pattern, pattern))
    sql = "SELECT * FROM market_instruments"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY source, dataset, name LIMIT ?"
    params.append(max(1, min(limit, 5000)))
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    result = []
    for stored in rows:
        row = dict(stored)
        row["metadata"] = json.loads(row.pop("metadata_json"))
        result.append(row)
    return result


def get_market_daily(
    *,
    source: str | None = None,
    dataset: str | None = None,
    symbol: str | None = None,
    day: str | None = None,
    limit: int = 500,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    for column, value in (("source", source), ("dataset", dataset), ("symbol", symbol), ("date", day)):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    sql = "SELECT * FROM market_daily"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY date DESC, dataset, name LIMIT ?"
    params.append(max(1, min(limit, 5000)))
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    result = []
    for stored in rows:
        row = dict(stored)
        row["raw"] = json.loads(row.pop("raw_json"))
        result.append(row)
    return result


def get_latest_market_daily(source: str, dataset: str) -> dict:
    """Return every normalized row for one dataset's latest cached day."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(date) AS date FROM market_daily WHERE source=? AND dataset=?",
            (source, dataset),
        ).fetchone()
        latest = row["date"] if row else None
        if not latest:
            return {"date": None, "rows": []}
        rows = conn.execute(
            """SELECT source, dataset, symbol, date, name, close, change,
                      change_pct, open, high, low, volume, turnover, market_cap,
                      retrieved_at
               FROM market_daily
               WHERE source=? AND dataset=? AND date=?
               ORDER BY name""",
            (source, dataset, latest),
        ).fetchall()
    return {"date": latest, "rows": [dict(item) for item in rows]}


def get_market_close_history(
    source: str,
    dataset: str,
    *,
    end: str | None = None,
    observations: int = 20,
) -> list[dict]:
    """Return at most N latest closes per instrument using one window query."""
    observations = max(1, min(observations, 252))
    before = " AND date<=?" if end else ""
    params: list = [source, dataset]
    if end:
        params.append(end)
    params.append(observations)
    with get_conn() as conn:
        rows = conn.execute(
            f"""WITH ranked AS (
                    SELECT symbol, date, close,
                           ROW_NUMBER() OVER (
                               PARTITION BY symbol ORDER BY date DESC
                           ) AS position
                    FROM market_daily
                    WHERE source=? AND dataset=? AND close IS NOT NULL{before}
                )
                SELECT symbol, date, close
                FROM ranked
                WHERE position<=?
                ORDER BY symbol, date""",
            params,
        ).fetchall()
    return [dict(row) for row in rows]
