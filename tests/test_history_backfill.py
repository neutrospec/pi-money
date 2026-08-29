"""Reaching past the collectors' three-year default, without overwriting.

The risk in a backfill is never that it fetches too little. It is that it
writes something subtly different over what is already there — a lookalike
series, a rebased level, a different item code that happens to return numbers.
Every test here is about refusing that.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db, history_backfill


class TemporaryDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()


class EcosDeepeningTests(TemporaryDatabaseTest):
    def test_a_deeper_fetch_extends_rather_than_replaces(self):
        db.save_indicator_points("kr_cd_91d", [
            {"date": "2026-01-02", "value": 3.5},
        ])
        deep = [{"date": "2010-01-04", "value": 2.8},
                {"date": "2026-01-02", "value": 3.5}]
        with patch("app.collectors.indicators.ecos_raw_series", return_value=deep):
            result = history_backfill.deepen_ecos("kr_cd_91d")
        self.assertEqual((1, 2, 1), (result["before"], result["after"], result["added"]))
        self.assertEqual("2010-01-04", result["earliest"])

    def test_a_disagreement_on_an_overlapping_date_refuses_the_write(self):
        # Same dates, different values means a different series — a deeper
        # feed for the same thing must agree where they overlap. Overwriting
        # here would silently swap the series under every screen that reads it.
        db.save_indicator_points("kr_cd_91d", [{"date": "2026-01-02", "value": 3.5}])
        wrong = [{"date": "2010-01-04", "value": 2.8},
                 {"date": "2026-01-02", "value": 9.9}]
        with patch("app.collectors.indicators.ecos_raw_series", return_value=wrong):
            result = history_backfill.deepen_ecos("kr_cd_91d")
        self.assertEqual(0, result["added"])
        self.assertEqual(1, result["clashes"])
        self.assertEqual(
            [3.5], [item["value"] for item in db.get_indicator_points("kr_cd_91d")])

    def test_an_empty_provider_response_writes_nothing(self):
        db.save_indicator_points("kr_cd_91d", [{"date": "2026-01-02", "value": 3.5}])
        with patch("app.collectors.indicators.ecos_raw_series", return_value=[]):
            result = history_backfill.deepen_ecos("kr_cd_91d")
        self.assertEqual(0, result["added"])
        self.assertEqual(1, len(db.get_indicator_points("kr_cd_91d")))

    def test_a_series_with_no_deep_mapping_is_refused_not_guessed(self):
        with self.assertRaises(ValueError):
            history_backfill.deepen_ecos("us_vix")


class KrxIndexBackfillTests(TemporaryDatabaseTest):
    def rows(self, value):
        return [{"IDX_NM": "코스피 200 변동성지수", "CLSPRC_IDX": str(value)},
                {"IDX_NM": "코스피 200", "CLSPRC_IDX": "400.0"}]

    def test_it_uses_the_same_matcher_the_live_aggregation_uses(self):
        # A fresh substring match would also catch "코스피 200 변동성지수 선물"
        # if it existed, and the backfilled points would be a different row
        # from the derived ones with nothing to reveal the difference.
        from app.collectors import krx

        found = krx.extract_named_indices(self.rows(21.0), "2015-06-15")
        self.assertEqual([{"indicator": "kr_vkospi", "date": "2015-06-15",
                           "value": 21.0}], found)

    def test_a_holiday_returning_no_rows_is_recorded_as_nothing_not_zero(self):
        with patch("app.collectors.krx.fetch_dataset", return_value=[]):
            result = history_backfill.deepen_krx_index(
                start="2015-06-15", end="2015-06-18")
        self.assertEqual(0, result["added"])
        self.assertGreater(result["empty_days"], 0)
        self.assertEqual([], db.get_indicator_points("kr_vkospi"))

    def test_a_day_already_held_is_not_refetched(self):
        db.save_indicator_points("kr_vkospi", [{"date": "2015-06-15", "value": 13.96}])
        calls = []

        def spy(spec, day):
            calls.append(day)
            return self.rows(99.0)

        with patch("app.collectors.krx.fetch_dataset", side_effect=spy):
            history_backfill.deepen_krx_index(start="2015-06-15", end="2015-06-17")
        self.assertNotIn("2015-06-15", calls)
        self.assertEqual(
            13.96,
            {p["date"]: p["value"] for p in db.get_indicator_points("kr_vkospi")}["2015-06-15"],
        )

    def test_a_failing_day_is_counted_rather_than_aborting_the_walk(self):
        def flaky(spec, day):
            if day == "2015-06-16":
                raise RuntimeError("provider hiccup")
            return self.rows(20.0)

        with patch("app.collectors.krx.fetch_dataset", side_effect=flaky):
            result = history_backfill.deepen_krx_index(
                start="2015-06-15", end="2015-06-18")
        self.assertEqual(1, result["failed_days"])
        self.assertGreater(result["added"], 0)


class CoherenceTests(TemporaryDatabaseTest):
    """Backfilled values carry no vintage, so two providers must agree."""

    def seed(self, implied, drift):
        from datetime import date, timedelta

        start = date(2020, 1, 1)
        price, prices, points = 100.0, [], []
        for offset in range(300):
            day = (start + timedelta(days=offset)).isoformat()
            price *= 1 + (drift if offset % 2 else -drift)
            prices.append({"date": day, "value": price})
            points.append({"date": day, "value": implied})
        db.replace_index_points("^KS11", prices)
        db.save_indicator_points("kr_vkospi", points)

    def test_an_implied_level_matching_realised_vol_is_not_flagged(self):
        # ±1% alternating daily moves annualise to about 16%.
        self.seed(implied=16.0, drift=0.01)
        report = history_backfill.volatility_coherence()
        self.assertEqual([], report["outliers"])
        self.assertAlmostEqual(1.0, report["years"][0]["ratio"], delta=0.25)

    def test_an_implied_level_divorced_from_realised_vol_is_flagged(self):
        # Same quiet market, an implied level five times too high: exactly the
        # shape a corrupted or rebased backfill would take.
        self.seed(implied=90.0, drift=0.01)
        self.assertEqual(["2020"], history_backfill.volatility_coherence()["outliers"])

    def test_a_year_with_too_few_observations_is_skipped_not_scored(self):
        db.replace_index_points("^KS11", [
            {"date": "2020-01-01", "value": 100.0},
            {"date": "2020-01-02", "value": 101.0},
        ])
        db.save_indicator_points("kr_vkospi", [{"date": "2020-01-02", "value": 20.0}])
        self.assertEqual([], history_backfill.volatility_coherence()["years"])


class LicenceTests(unittest.TestCase):
    def test_the_module_records_which_series_cannot_be_deepened(self):
        # us_ig_spread is capped by FRED at 2023-08-29 because ICE licenses a
        # rolling window. Writing that down is what stops the next person from
        # trying, and stops anyone from assuming the US window can move.
        self.assertIn("us_ig_spread", history_backfill.__doc__)
        self.assertIn("2023-08", history_backfill.__doc__)
        self.assertNotIn("us_ig_spread", history_backfill.ECOS_DEEP)


if __name__ == "__main__":
    unittest.main()
