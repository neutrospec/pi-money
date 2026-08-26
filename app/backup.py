"""Consistent, non-destructive SQLite backup command."""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from pathlib import Path

from app import db


def backup_database(destination_dir: Path | None = None) -> Path:
    """Create and integrity-check a timestamped SQLite backup."""
    db.init_db()
    destination_dir = destination_dir or db.DB_PATH.parent / "backups"
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = destination_dir / f"money-{stamp}.db"

    with db.get_conn() as source, sqlite3.connect(destination) as target:
        source.backup(target)
        result = target.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"backup integrity check failed: {result}")
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Money SQLite 일관성 백업 생성")
    parser.add_argument(
        "--destination",
        type=Path,
        help="백업 디렉터리 (기본: data/backups)",
    )
    args = parser.parse_args()
    print(backup_database(args.destination))


if __name__ == "__main__":
    main()
