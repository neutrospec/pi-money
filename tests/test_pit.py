"""Point-in-time replay: what this repository could have said on a past date.

Everything here runs on a temporary database. A replay test that reads the
live cache would pass today and drift tomorrow, and this project has already
had a test write synthetic values into the real series twice.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import analysis, db, market_metrics, normalize, pit


class TemporaryDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def vintage(self, indicator, day, value, retrieved_at):
        with db.get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO indicator_vintages
                   (indicator, date, retrieved_at, value, source)
                   VALUES (?, ?, ?, ?, 'test')""",
                (indicator, day, retrieved_at, value),
            )

    def krx_row(self, day, symbol, close, change_pct, retrieved_at):
        with db.get_conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO market_instruments
                   (source, dataset, symbol, name, asset_type, first_seen, last_seen)
                   VALUES ('krx','stk_bydd_trd',?,?,'stock',?,?)""",
                (symbol, symbol, day, day),
            )
            conn.execute(
                """INSERT OR REPLACE INTO market_daily
                   (source, dataset, symbol, date, name, close, change_pct,
                    turnover, raw_json, retrieved_at)
                   VALUES ('krx','stk_bydd_trd',?,?,?,?,?,1000,'{}',?)""",
                (symbol, day, symbol, close, change_pct, retrieved_at),
            )


class VintageCutTests(TemporaryDatabaseTest):
    """The cut is a string comparison in SQL, so its spelling is load-bearing."""

    def test_the_cut_is_written_in_the_shape_the_ledger_stores(self):
        # KST midnight is 15:00Z the same calendar day. Spelled as
        # "+09:00" the string sorts after that afternoon's UTC rows even
        # though the instant is identical, and nine hours of the future
        # walk in. On 2026-08-27 that was 25 real rows, all US closes.
        self.assertEqual("2026-08-27T15:00:00+00:00", pit._instant("2026-08-27"))
        self.assertLess(pit._instant("2026-08-27"), "2026-08-27T17:42:09.147649+00:00")

    def test_a_value_received_after_the_close_is_not_in_that_days_replay(self):
        self.vintage("us_vix", "2026-08-27", 14.0, "2026-08-27T13:00:00+00:00")
        self.vintage("us_vix", "2026-08-27", 19.0, "2026-08-27T17:42:00+00:00")
        ledger = pit.Ledger("2026-08-27", pit.VINTAGE)
        self.assertEqual([14.0], [item["value"] for item in ledger.indicator("us_vix")])

    def test_a_revision_is_visible_as_the_difference_between_two_as_ofs(self):
        self.vintage("brent", "2026-08-26", 86.60, "2026-08-26T09:00:00+00:00")
        self.vintage("brent", "2026-08-26", 87.84, "2026-08-28T09:00:00+00:00")
        early = pit.Ledger("2026-08-26", pit.VINTAGE).indicator("brent")
        later = pit.Ledger("2026-08-28", pit.VINTAGE).indicator("brent")
        self.assertEqual(86.60, early[-1]["value"])
        self.assertEqual(87.84, later[-1]["value"])

    def test_provenance_says_unavailable_rather_than_falling_back_to_live(self):
        db.save_indicator_points("us_vix", [{"date": "2026-08-27", "value": 14.0}])
        ledger = pit.Ledger("2026-08-01", pit.VINTAGE)
        self.assertEqual([], ledger.indicator("us_vix"))
        self.assertEqual(pit.UNAVAILABLE, ledger.provenance["us_vix"])


class ReadinessTests(TemporaryDatabaseTest):
    """When a series became replayable is a KST date, read off a UTC instant."""

    def test_an_arrival_after_the_kst_cut_belongs_to_the_next_day(self):
        # The replay cut for day D is 15:00Z on D. A row received at 16:00Z is
        # excluded from D's replay, so reporting its UTC date as the day the
        # series became replayable is a day early — and 411 rows in this
        # ledger arrived past 15:00Z.
        self.assertEqual("2026-08-27", pit._kst_day("2026-08-27T14:59:00+00:00"))
        self.assertEqual("2026-08-27", pit._kst_day("2026-08-27T15:00:00+00:00"))
        self.assertEqual("2026-08-28", pit._kst_day("2026-08-27T16:00:00+00:00"))

    def test_the_reported_date_is_one_a_replay_actually_succeeds_on(self):
        for day in range(1, 4):
            self.vintage("us_vix", f"2026-08-{day:02d}", float(day),
                         f"2026-08-27T16:00:00.{day:06d}+00:00")
        report = pit.readiness(["us_vix"], today=date(2026, 8, 29))
        row = report["series"][0]
        self.assertEqual(3, row["observations"])
        # Three observations against a minimum of sixty: short, and reported
        # as short rather than given a date it cannot honour.
        self.assertIsNone(row["replayable_from"])
        self.assertEqual(["us_vix"], report["waiting"])
        # Arrived 16:00Z on the 27th, which is already the 28th in Seoul.
        self.assertEqual("2026-08-28", row["first_arrival"])

    def test_a_series_deep_enough_reports_the_day_its_nth_row_arrived(self):
        needed = 60
        start = date(2026, 5, 1)
        for step in range(needed):
            # Distinct observation dates — the ledger counts dates, not rows.
            self.vintage("us_vix", (start + timedelta(days=step)).isoformat(),
                         float(step),
                         f"2026-08-2{step % 3 + 1}T09:00:00.{step:06d}+00:00")
        report = pit.readiness(["us_vix"], today=date(2026, 8, 29))
        row = report["series"][0]
        self.assertTrue(row["replayable_from"])
        self.assertTrue(row["usable_today"])
        depth = len(db.get_indicator_vintage_points(
            "us_vix", as_of=pit._instant(row["replayable_from"])))
        self.assertGreaterEqual(depth, needed, "reported date cannot replay")


class ObservationDateTests(TemporaryDatabaseTest):
    def test_an_observed_replay_stops_at_the_day_it_replays(self):
        db.save_indicator_points("us_vix", [
            {"date": "2026-08-26", "value": 14.0},
            {"date": "2026-08-27", "value": 15.0},
            {"date": "2026-08-28", "value": 22.0},
        ])
        ledger = pit.Ledger("2026-08-27")
        self.assertEqual(["2026-08-26", "2026-08-27"],
                         [item["date"] for item in ledger.indicator("us_vix")])
        self.assertEqual(pit.FROM_OBSERVED, ledger.provenance["us_vix"])

    def test_breadth_reads_the_replayed_session_not_the_newest_one(self):
        # The leak this module was built to find, in its smallest form:
        # get_latest_market_daily hardcoded MAX(date), so a capped replay
        # returned the newest session's advance/decline regardless.
        for symbol, pct in (("A", 1.0), ("B", 1.0), ("C", -1.0)):
            self.krx_row("2026-08-25", symbol, 100, pct, "2026-08-25T07:00:00+00:00")
        for symbol, pct in (("A", -1.0), ("B", -1.0), ("C", -1.0)):
            self.krx_row("2026-08-28", symbol, 100, pct, "2026-08-28T07:00:00+00:00")
        past = market_metrics._breadth_market("stk_bydd_trd", "KOSPI", day="2026-08-25")
        live = market_metrics._breadth_market("stk_bydd_trd", "KOSPI")
        self.assertEqual(("2026-08-25", 2, 1), (past["as_of"], past["advances"], past["declines"]))
        self.assertEqual(("2026-08-28", 0, 3), (live["as_of"], live["advances"], live["declines"]))


class FrozenClockTests(unittest.TestCase):
    """A freshness gate reading the real clock empties every past verdict."""

    def test_a_stale_point_is_accepted_when_the_clock_is_frozen_to_its_era(self):
        # A date old enough that the real clock will always call it stale,
        # so this test says the same thing whenever it runs.
        points = [{"date": "2024-03-15", "value": 14.0}]
        self.assertIsNone(analysis._latest_if_recent(points, 7))
        self.assertEqual(
            14.0,
            analysis._latest_if_recent(points, 7, date(2024, 3, 18))["value"],
        )
        self.assertIsNone(
            analysis._latest_if_recent(points, 7, date(2024, 4, 30))
        )

    def test_a_replayed_verdict_does_not_go_blank_from_the_wall_clock(self):
        # Built so every component is old relative to today but current
        # relative to the replayed day.
        vix = [{"date": f"2024-0{1 + i // 28}-{i % 28 + 1:02d}", "value": 12 + i % 9}
               for i in range(112)]
        vix[-1] = {"date": "2024-04-28", "value": 30.0}
        frozen = analysis.market_regime(vix, None, None, date(2024, 4, 29))
        self.assertEqual(["VIX 높음 (30.0)"], frozen["reasons"])
        # The same inputs, judged by the real clock: everything is stale, so
        # the verdict empties. That is the failure a replay must not have.
        self.assertEqual([], analysis.market_regime(vix, None, None)["reasons"])


class LeakReportTests(TemporaryDatabaseTest):
    """The report must not call missing data a finding."""

    def test_a_thin_ledger_is_reported_as_thin_not_as_covered(self):
        self.vintage("kr_vkospi", "2026-08-27", 21.0, "2026-08-27T07:00:00+00:00")
        report = pit.coverage("2026-08-27", ["kr_vkospi"])
        self.assertEqual(
            (1, 0, 1, False),
            (report["requested"], report["usable"], report["thin"], report["complete"]),
        )
        self.assertEqual(normalize.minimum_for("kr_vkospi"),
                         report["series"][0]["minimum"])

    def test_verdicts_are_not_compared_when_the_vintage_side_is_incomplete(self):
        observed = {"korea_regime": {"regime": "neutral", "score": -1, "components": []},
                    "regime": {"regime": "risk_on", "score": 3}}
        vintaged = {"korea_regime": {"regime": "unknown", "score": 0, "components": [],
                                     "pending": [{"key": "volatility", "reason": "이력 부족"}]},
                    "regime": {"regime": "unknown", "score": 0}}
        thin = pit._revisions(observed, vintaged, {"complete": False})
        self.assertEqual([], thin["verdicts"])
        self.assertFalse(thin["verdict_comparable"])
        self.assertEqual(["volatility"], [x["key"] for x in thin["not_compared"]])

        full = pit._revisions(observed, vintaged, {"complete": True})
        self.assertEqual(2, len(full["verdicts"]))

    def test_a_component_carrying_no_percentile_is_not_counted_as_revised(self):
        observed = {"korea_regime": {"regime": "neutral", "score": 0, "components": [
            {"key": "trend", "label": "추세", "score": 1}]}, "regime": {}}
        vintaged = {"korea_regime": {"regime": "neutral", "score": 0, "components": [
            {"key": "trend", "label": "추세", "score": 1}], "pending": []}, "regime": {}}
        self.assertEqual([], pit._revisions(observed, vintaged, {"complete": True})["components"])


if __name__ == "__main__":
    unittest.main()
