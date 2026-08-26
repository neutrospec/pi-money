"""Tests for backup retention.

Retention exists because a full copy of this database stops being cheap: KRX
adds roughly ten million rows a year. These tests hold the policy to the one
property that matters more than any count — it must never leave the operator
without a backup.
"""
from __future__ import annotations

import gzip
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import backup, db


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S%fZ")


class RetentionSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.directory = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _write(self, moment: datetime, size: int = 1024) -> Path:
        path = self.directory / f"money-{_stamp(moment)}.db"
        path.write_bytes(b"x" * size)
        return path

    def test_backups_are_discovered_newest_first(self):
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        for offset in (0, 5, 2):
            self._write(base - timedelta(days=offset))
        found = backup.list_backups(self.directory)
        self.assertEqual(3, len(found))
        self.assertEqual(
            sorted((item.taken_at for item in found), reverse=True),
            [item.taken_at for item in found],
        )

    def test_unrelated_files_are_left_alone(self):
        (self.directory / "notes.txt").write_text("keep me")
        (self.directory / "money-garbage.db").write_bytes(b"x")
        self._write(datetime(2026, 8, 26, tzinfo=timezone.utc))
        self.assertEqual(1, len(backup.list_backups(self.directory)))
        report = backup.prune(self.directory)
        self.assertEqual(0, report["removed"])
        self.assertTrue((self.directory / "notes.txt").exists())

    def test_recent_weekly_and_monthly_tiers_are_each_honoured(self):
        base = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
        for days in (0, 1, 2, 3, 10, 20, 40, 80, 200):
            self._write(base - timedelta(days=days))
        with patch.object(backup, "KEEP_RECENT", 2), \
             patch.object(backup, "KEEP_WEEKLY", 2), \
             patch.object(backup, "KEEP_MONTHLY", 2), \
             patch.object(backup, "MAX_TOTAL_MB", 0):
            retained = backup.select_retained(backup.list_backups(self.directory))
        # Two newest, plus one for each of two distinct weeks and months.
        self.assertGreaterEqual(len(retained), 2)
        self.assertLessEqual(len(retained), 6)
        newest = max(item.taken_at for item in backup.list_backups(self.directory))
        self.assertIn(newest, [item.taken_at for item in retained])

    def test_a_tier_claims_the_newest_backup_in_its_period(self):
        base = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
        newest_of_week = self._write(base)
        self._write(base - timedelta(hours=6))
        with patch.object(backup, "KEEP_RECENT", 1), \
             patch.object(backup, "KEEP_WEEKLY", 1), \
             patch.object(backup, "KEEP_MONTHLY", 0), \
             patch.object(backup, "MAX_TOTAL_MB", 0):
            retained = backup.select_retained(backup.list_backups(self.directory))
        self.assertIn(newest_of_week, [item.path for item in retained])

    def test_the_size_budget_drops_oldest_first(self):
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        for days in range(6):
            self._write(base - timedelta(days=days), size=1024 * 1024)
        with patch.object(backup, "KEEP_RECENT", 6), \
             patch.object(backup, "KEEP_WEEKLY", 0), \
             patch.object(backup, "KEEP_MONTHLY", 0), \
             patch.object(backup, "MAX_TOTAL_MB", 3):
            retained = backup.select_retained(backup.list_backups(self.directory))
        self.assertEqual(3, len(retained))
        self.assertEqual(base, max(item.taken_at for item in retained))

    def test_the_newest_backup_survives_a_budget_smaller_than_itself(self):
        # A policy that can delete the only copy is worse than no policy.
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        self._write(base, size=8 * 1024 * 1024)
        with patch.object(backup, "KEEP_RECENT", 3), \
             patch.object(backup, "KEEP_WEEKLY", 0), \
             patch.object(backup, "KEEP_MONTHLY", 0), \
             patch.object(backup, "MAX_TOTAL_MB", 1):
            retained = backup.select_retained(backup.list_backups(self.directory))
        self.assertEqual(1, len(retained))

    def test_prune_reports_what_it_removed_and_dry_run_removes_nothing(self):
        base = datetime(2026, 8, 26, tzinfo=timezone.utc)
        for days in range(5):
            self._write(base - timedelta(days=days))
        with patch.object(backup, "KEEP_RECENT", 1), \
             patch.object(backup, "KEEP_WEEKLY", 0), \
             patch.object(backup, "KEEP_MONTHLY", 0), \
             patch.object(backup, "MAX_TOTAL_MB", 0):
            preview = backup.prune(self.directory, dry_run=True)
            self.assertEqual(4, preview["removed"])
            self.assertEqual(5, len(backup.list_backups(self.directory)))
            applied = backup.prune(self.directory)
        self.assertEqual(4, applied["removed"])
        self.assertEqual(1, len(backup.list_backups(self.directory)))


class BackupRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.original_path = db.DB_PATH
        db.DB_PATH = self.root / "money-test.db"
        db.init_db()
        db.set_meta("sentinel", "present")

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def test_a_compressed_backup_restores_to_an_intact_database(self):
        archive = backup.backup_database(self.root / "backups")
        self.assertEqual(".gz", archive.suffix)
        restored = backup.restore(archive, self.root / "restored.db")
        with sqlite3.connect(restored) as conn:
            self.assertEqual("ok", conn.execute("PRAGMA quick_check").fetchone()[0])
            self.assertEqual(
                "present",
                conn.execute(
                    "SELECT value FROM meta WHERE key='sentinel'"
                ).fetchone()[0],
            )

    def test_compression_can_be_turned_off(self):
        plain = backup.backup_database(self.root / "backups", compress=False)
        self.assertEqual(".db", plain.suffix)
        with sqlite3.connect(plain) as conn:
            self.assertEqual("ok", conn.execute("PRAGMA quick_check").fetchone()[0])

    def test_the_source_database_is_never_touched(self):
        before = db.DB_PATH.read_bytes()
        backup.backup_database(self.root / "backups")
        self.assertTrue(db.DB_PATH.exists())
        self.assertEqual(before, db.DB_PATH.read_bytes())

    def test_no_staging_directory_survives_a_run(self):
        directory = self.root / "backups"
        backup.backup_database(directory)
        leftovers = [item for item in directory.iterdir() if item.is_dir()]
        self.assertEqual([], leftovers)

    def test_a_new_backup_prunes_within_the_policy(self):
        directory = self.root / "backups"
        with patch.object(backup, "KEEP_RECENT", 1), \
             patch.object(backup, "KEEP_WEEKLY", 0), \
             patch.object(backup, "KEEP_MONTHLY", 0), \
             patch.object(backup, "MAX_TOTAL_MB", 0):
            for _ in range(3):
                backup.backup_database(directory)
        self.assertEqual(1, len(backup.list_backups(directory)))

    def test_the_archive_is_real_gzip(self):
        archive = backup.backup_database(self.root / "backups")
        with gzip.open(archive, "rb") as packed:
            self.assertEqual(b"SQLite format 3\x00", packed.read(16))


if __name__ == "__main__":
    unittest.main()
