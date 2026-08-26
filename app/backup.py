"""Consistent SQLite backup with tiered retention.

A full copy of this database is cheap today and will not stay that way: KRX
adds roughly ten million rows a year, so an unbounded backup directory grows
faster than the database it protects.  Retention here answers three separate
questions rather than one:

*How far back can I go?*  Tiered counts — several recent copies, one per week,
one per month — cover "I broke it an hour ago" and "I broke it last month"
without keeping a copy of every run in between.

*How much disk may this use?*  A total budget prunes oldest-first regardless
of tier, so a growing database cannot silently fill the disk.

*Can I afford to keep more?*  Backups compress about sevenfold because the
provider payloads are JSON text, so compression is the default and multiplies
every other allowance.
"""
from __future__ import annotations

import argparse
import gzip
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app import db
from app.timeutil import parse_instant, utc_now


STAMP_PATTERN = re.compile(r"money-(\d{8}T\d{6}\d*Z)\.db(\.gz)?$")

KEEP_RECENT = max(1, int(os.environ.get("BACKUP_KEEP_RECENT", "3")))
KEEP_WEEKLY = max(0, int(os.environ.get("BACKUP_KEEP_WEEKLY", "4")))
KEEP_MONTHLY = max(0, int(os.environ.get("BACKUP_KEEP_MONTHLY", "6")))
MAX_TOTAL_MB = max(0, int(os.environ.get("BACKUP_MAX_TOTAL_MB", "20480")))
COMPRESS = os.environ.get("BACKUP_COMPRESS", "1").strip().lower() not in {
    "0", "false", "no", "off",
}


@dataclass(frozen=True)
class Backup:
    path: Path
    taken_at: datetime
    size: int

    @property
    def week(self) -> tuple[int, int]:
        return self.taken_at.isocalendar()[:2]

    @property
    def month(self) -> tuple[int, int]:
        return self.taken_at.year, self.taken_at.month


def list_backups(destination_dir: Path) -> list[Backup]:
    """Return existing backups, newest first, ignoring unrelated files."""
    found = []
    if not destination_dir.exists():
        return found
    for path in destination_dir.iterdir():
        match = STAMP_PATTERN.search(path.name)
        if not match or not path.is_file():
            continue
        stamp = match.group(1)
        moment = parse_instant(
            f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T"
            f"{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}+00:00"
        )
        if moment is None:
            continue
        found.append(Backup(path=path, taken_at=moment, size=path.stat().st_size))
    return sorted(found, key=lambda item: item.taken_at, reverse=True)


def select_retained(backups: list[Backup]) -> list[Backup]:
    """Choose which backups to keep. Input must be newest first.

    Each tier claims the newest backup in its period, so a weekly slot is
    filled by that week's most recent copy rather than an arbitrary one.
    """
    keep: dict[Path, Backup] = {}
    for item in backups[:KEEP_RECENT]:
        keep[item.path] = item
    for attribute, limit in (("week", KEEP_WEEKLY), ("month", KEEP_MONTHLY)):
        if limit <= 0:
            continue
        seen: set = set()
        for item in backups:
            period = getattr(item, attribute)
            if period in seen:
                continue
            seen.add(period)
            keep[item.path] = item
            if len(seen) >= limit:
                break
    retained = sorted(keep.values(), key=lambda item: item.taken_at, reverse=True)
    if not MAX_TOTAL_MB:
        return retained
    # The budget is a backstop: drop the oldest first, but never the newest
    # copy, because a retention policy that can delete the only backup is
    # worse than none.
    budget = MAX_TOTAL_MB * 1024 * 1024
    within, used = [], 0
    for item in retained:
        if within and used + item.size > budget:
            break
        within.append(item)
        used += item.size
    return within


def prune(destination_dir: Path, *, dry_run: bool = False) -> dict:
    backups = list_backups(destination_dir)
    retained = {item.path for item in select_retained(backups)}
    removed = [item for item in backups if item.path not in retained]
    if not dry_run:
        for item in removed:
            item.path.unlink(missing_ok=True)
    kept = [item for item in backups if item.path in retained]
    return {
        "kept": len(kept),
        "removed": len(removed),
        "removed_paths": [str(item.path.name) for item in removed],
        "bytes_kept": sum(item.size for item in kept),
        "bytes_removed": sum(item.size for item in removed),
        "policy": {
            "keep_recent": KEEP_RECENT,
            "keep_weekly": KEEP_WEEKLY,
            "keep_monthly": KEEP_MONTHLY,
            "max_total_mb": MAX_TOTAL_MB or None,
            "compress": COMPRESS,
        },
    }


def backup_database(
    destination_dir: Path | None = None,
    *,
    compress: bool | None = None,
    retain: bool = True,
) -> Path:
    """Create and integrity-check a timestamped SQLite backup."""
    db.init_db()
    destination_dir = destination_dir or db.DB_PATH.parent / "backups"
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    compress = COMPRESS if compress is None else compress

    # The integrity check runs against the plain copy before compression, so
    # a corrupt page is caught while it is still cheap to see.
    staging = Path(tempfile.mkdtemp(dir=destination_dir)) / f"money-{stamp}.db"
    try:
        # `with` on a connection opens a transaction; it does not close the
        # handle. The copy has to be closed before it is moved or read, or
        # pages still buffered by SQLite never reach the file.
        with db.get_conn() as source:
            target = sqlite3.connect(staging)
            try:
                source.backup(target)
                result = target.execute("PRAGMA quick_check").fetchone()[0]
                if result != "ok":
                    raise RuntimeError(f"backup integrity check failed: {result}")
            finally:
                target.close()
        if compress:
            destination = destination_dir / f"money-{stamp}.db.gz"
            with staging.open("rb") as raw, gzip.open(destination, "wb", 6) as packed:
                shutil.copyfileobj(raw, packed, length=4 * 1024 * 1024)
        else:
            destination = destination_dir / f"money-{stamp}.db"
            staging.replace(destination)
    finally:
        shutil.rmtree(staging.parent, ignore_errors=True)

    if retain:
        prune(destination_dir)
    return destination


def restore(archive: Path, destination: Path) -> Path:
    """Expand a compressed backup so it can be opened directly."""
    if archive.suffix != ".gz":
        shutil.copyfile(archive, destination)
        return destination
    with gzip.open(archive, "rb") as packed, destination.open("wb") as raw:
        shutil.copyfileobj(packed, raw, length=4 * 1024 * 1024)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Money SQLite 일관성 백업 생성")
    parser.add_argument("--destination", type=Path, help="백업 디렉터리 (기본: data/backups)")
    parser.add_argument("--no-compress", action="store_true", help="gzip 압축 없이 저장")
    parser.add_argument("--no-retain", action="store_true", help="오래된 백업 정리 생략")
    parser.add_argument("--prune-only", action="store_true", help="새 백업 없이 정리만 수행")
    parser.add_argument("--dry-run", action="store_true", help="정리 대상만 출력")
    parser.add_argument("--restore", type=Path, help="지정한 백업을 풀어서 복원")
    parser.add_argument("--into", type=Path, help="--restore 대상 경로")
    args = parser.parse_args()

    directory = args.destination or db.DB_PATH.parent / "backups"
    if args.restore:
        if not args.into:
            parser.error("--restore 에는 --into 경로가 필요합니다")
        print(restore(args.restore, args.into))
        return
    if args.prune_only or args.dry_run:
        report = prune(directory, dry_run=args.dry_run)
        verb = "정리 예정" if args.dry_run else "정리 완료"
        print(
            f"{verb}: 보존 {report['kept']}개 "
            f"({report['bytes_kept'] / 1024 / 1024:.1f} MB), "
            f"삭제 {report['removed']}개 "
            f"({report['bytes_removed'] / 1024 / 1024:.1f} MB)"
        )
        for name in report["removed_paths"]:
            print(f"  - {name}")
        return
    print(backup_database(
        directory, compress=not args.no_compress, retain=not args.no_retain
    ))


if __name__ == "__main__":
    main()
