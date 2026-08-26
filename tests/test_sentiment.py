"""Tests for the market sentiment gauge.

The gauge's whole claim is that it is reproducible: every component states
its method, and one that cannot be measured is dropped rather than guessed.
These tests hold it to that.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import db, sentiment


class ScaleTests(unittest.TestCase):
    def test_the_midpoint_of_a_range_reads_neutral(self):
        self.assertEqual(50.0, sentiment._scale(0.0, -10.0, 10.0))

    def test_values_beyond_the_range_are_clipped_not_extrapolated(self):
        self.assertEqual(100.0, sentiment._scale(999.0, -10.0, 10.0))
        self.assertEqual(0.0, sentiment._scale(-999.0, -10.0, 10.0))

    def test_an_inverted_range_flips_the_direction(self):
        # Put/call is scaled high-to-low because a high ratio means fear.
        self.assertGreater(
            sentiment._scale(0.6, 1.4, 0.6), sentiment._scale(1.4, 1.4, 0.6)
        )

    def test_a_degenerate_range_does_not_divide_by_zero(self):
        self.assertEqual(50.0, sentiment._scale(5.0, 3.0, 3.0))

    def test_percentile_inversion_makes_a_wide_spread_read_as_fear(self):
        history = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertLess(
            sentiment._percentile_score(5.0, history, invert=True),
            sentiment._percentile_score(1.0, history, invert=True),
        )

    def test_an_empty_history_returns_neutral_rather_than_an_edge(self):
        self.assertEqual(50.0, sentiment._percentile_score(1.0, [], invert=True))


class BandTests(unittest.TestCase):
    def test_bands_cover_the_whole_scale_in_order(self):
        readings = [sentiment.band(score)[0] for score in (0, 24, 30, 50, 60, 80, 100)]
        self.assertEqual(
            [
                "extreme_fear", "extreme_fear", "fear",
                "neutral", "greed", "extreme_greed", "extreme_greed",
            ],
            readings,
        )


class GaugeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def test_an_empty_cache_reports_unavailable_rather_than_fifty(self):
        report = sentiment.gauge()
        self.assertEqual("unavailable", report["status"])
        self.assertEqual([], report["components"])
        self.assertNotIn("score", report)

    def test_every_unmeasurable_component_states_a_reason(self):
        report = sentiment.gauge()
        self.assertEqual(len(sentiment.COMPONENTS), len(report["pending"]))
        for item in report["pending"]:
            self.assertTrue(item["reason"])

    def test_a_single_measurable_component_still_produces_a_reading(self):
        db.save_indicator_points(
            "kr_put_call_volume",
            [{"date": "2026-08-25", "value": 0.6}],
            source="krx",
        )
        report = sentiment.gauge()
        self.assertEqual("ok", report["status"])
        self.assertEqual(1, report["component_count"])
        self.assertEqual(7, report["component_total"])
        # A ratio at the greedy end of the scale must not read as fear.
        self.assertGreater(report["score"], 55)

    def test_the_composite_is_the_mean_of_what_was_measured(self):
        db.save_indicator_points(
            "kr_put_call_volume",
            [{"date": "2026-08-25", "value": 1.0}],
            source="krx",
        )
        report = sentiment.gauge()
        measured = [item["score"] for item in report["components"]]
        self.assertAlmostEqual(
            report["score"], sum(measured) / len(measured), places=1
        )

    def test_every_component_publishes_its_method_and_observation_date(self):
        db.save_indicator_points(
            "kr_put_call_volume",
            [{"date": "2026-08-25", "value": 0.9}],
            source="krx",
        )
        for component in sentiment.gauge()["components"]:
            for field in ("key", "label", "score", "detail", "as_of", "method"):
                self.assertIn(field, component, component.get("key"))
            self.assertTrue(component["method"], component["key"])

    def test_the_reading_is_labelled_as_descriptive_not_directive(self):
        db.save_indicator_points(
            "kr_put_call_volume",
            [{"date": "2026-08-25", "value": 0.9}],
            source="krx",
        )
        report = sentiment.gauge()
        self.assertIn("신호가 아닙니다", report["warning"])
        self.assertIn("CNN", report["warning"])

    def test_the_gauge_never_calls_a_provider(self):
        from unittest.mock import patch

        with patch(
            "app.collectors.indices.full_history",
            side_effect=AssertionError("live call"),
        ), patch(
            "app.collectors.krx.fetch_dataset",
            side_effect=AssertionError("live call"),
        ):
            self.assertTrue(sentiment.gauge()["cached"])


if __name__ == "__main__":
    unittest.main()
