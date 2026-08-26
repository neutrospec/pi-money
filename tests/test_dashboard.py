"""Tests for the market situation view.

The front page exists to answer "what state is the market in" without a tour
of five screens, so these tests pin the parts a reader has to be able to
trust: that every number carries its observation date, that the three colour
axes stay separate, and that the page never reaches a provider.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import dashboard, db


class SituationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def _seed(self):
        db.save_indicator_points("kr_base_rate", [
            {"date": "2026-08-24", "value": 2.50},
            {"date": "2026-08-25", "value": 2.75},
        ], source="ecos")
        db.save_indicator_points("us_vix", [
            {"date": "2026-08-24", "value": 18.0},
            {"date": "2026-08-25", "value": 15.0},
        ], source="fred")
        db.save_index_quote({
            "symbol": "^KS11", "price": 6742.74, "prev_close": 6696.96,
            "currency": "KRW", "session_date": "2026-08-25",
            "updated_at": db.utc_now(),
        })

    def test_a_tile_reports_the_observation_date_of_its_value(self):
        self._seed()
        tiles = [
            tile for group in dashboard.headline_tiles()
            for tile in group["tiles"]
        ]
        rate = next(tile for tile in tiles if tile["key"] == "kr_base_rate")
        self.assertEqual(rate["value"], 2.75)
        self.assertEqual(rate["date"], "2026-08-25")
        self.assertAlmostEqual(rate["change"], 0.25)

    def test_a_tile_without_history_reports_no_change_rather_than_zero(self):
        db.save_indicator_points(
            "kr_base_rate", [{"date": "2026-08-25", "value": 2.75}], source="ecos"
        )
        tiles = [
            tile for group in dashboard.headline_tiles()
            for tile in group["tiles"]
        ]
        rate = next(tile for tile in tiles if tile["key"] == "kr_base_rate")
        self.assertIsNone(rate["change"])

    def test_a_rise_in_a_risk_series_is_marked_as_risk_not_as_growth(self):
        # The interface needs this to avoid painting a widening spread with
        # the same colour as a rising index.
        self._seed()
        tiles = {
            tile["key"]: tile for group in dashboard.headline_tiles()
            for tile in group["tiles"]
        }
        self.assertEqual(tiles["us_vix"]["direction"], dashboard.RISK)
        self.assertEqual(tiles["kr_base_rate"]["direction"], dashboard.NEUTRAL)

    def test_index_change_comes_from_the_stored_settled_close(self):
        self._seed()
        row = next(
            item for item in dashboard.headline_indices()
            if item["symbol"] == "^KS11"
        )
        self.assertAlmostEqual(row["change_pct"], 0.68, places=2)
        self.assertEqual(row["session_date"], "2026-08-25")

    def test_freshness_counts_missing_series_without_reading_observations(self):
        report = dashboard.freshness()
        self.assertEqual(report["status"], "incomplete")
        self.assertGreater(report["missing"], 0)

    def test_freshness_flags_an_index_behind_its_provider(self):
        db.save_index_points("^KS11", [{"date": "2026-08-20", "value": 1.0}])
        db.save_index_quote({
            "symbol": "^KS11", "price": 1.0, "prev_close": 1.0,
            "currency": "KRW", "session_date": "2026-08-25",
            "updated_at": db.utc_now(),
        })
        self.assertEqual(dashboard.freshness()["indices_behind"], 1)

    def test_only_high_impact_events_reach_the_front_page(self):
        db.replace_events([
            {
                "date": "2026-08-26", "time": "21:30", "country": "US",
                "title": "미국 GDP", "impact": "high", "note": "",
                "source": "curated",
            },
            {
                "date": "2026-08-26", "time": "10:00", "country": "KR",
                "title": "사소한 발표", "impact": "low", "note": "",
                "source": "curated",
            },
        ])
        with patch.object(dashboard, "kst_today", lambda: __import__(
            "datetime").date(2026, 8, 26)):
            titles = [event["title"] for event in dashboard.upcoming_events()]
        self.assertEqual(titles, ["미국 GDP"])

    def test_situation_is_assembled_without_touching_a_provider(self):
        self._seed()
        with patch(
            "app.collectors.indices.full_history",
            side_effect=AssertionError("live call"),
        ), patch(
            "app.collectors.indicators.fetch_indicator",
            side_effect=AssertionError("live call"),
        ):
            report = dashboard.situation()
        self.assertTrue(report["cached"])
        for field in ("regime", "groups", "indices", "risk", "events", "freshness"):
            self.assertIn(field, report)


class SituationPageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def test_front_page_renders_state_and_a_freshness_badge(self):
        from fastapi.testclient import TestClient

        from app import main

        with patch.dict(os.environ, {"MONEY_DISABLE_SCHEDULER": "1"}), patch(
            "app.collectors.indicators.fetch_indicator",
            side_effect=AssertionError("live call"),
        ):
            with TestClient(main.app) as client:
                page = client.get("/")
                calendar = client.get("/calendar")
                payload = client.get("/api/situation")
        self.assertEqual(200, page.status_code)
        self.assertIn("시장 상황판", page.text)
        self.assertIn("핵심 계열", page.text)
        self.assertIn("/manage", page.text)
        self.assertEqual(200, calendar.status_code)
        self.assertIn("경제 일정", calendar.text)
        self.assertTrue(payload.json()["cached"])

    def test_colour_axes_are_declared_once_and_stay_distinct(self):
        stylesheet = Path("app/templates/base.html").read_text()
        for token in (".up {", ".down {", ".pos {", ".neg {", ".sig-ok {"):
            self.assertIn(token, stylesheet)

    def test_analysis_screens_do_not_borrow_the_price_palette(self):
        # A positive correlation must never read as "the market fell".
        for name in ("correlation.html", "spillover.html"):
            markup = Path(f"app/templates/{name}").read_text()
            self.assertNotIn("#3fb950", markup, name)
            self.assertNotIn("#f85149", markup, name)


if __name__ == "__main__":
    unittest.main()


class AgentSurfaceTests(unittest.TestCase):
    """The two agent surfaces must not drift apart silently."""

    def test_pi_extension_exposes_the_same_tool_names_as_mcp(self):
        import re

        import anyio

        from app import mcp_server

        markup = Path(".pi/extensions/market.ts").read_text()
        pi_tools = set(re.findall(r'name:\s*"(market_[a-z_]+)"', markup))
        mcp_tools = {tool.name for tool in anyio.run(mcp_server.mcp.list_tools)}
        self.assertEqual(mcp_tools, pi_tools)

    def test_the_tool_guide_documents_every_registered_tool(self):
        import anyio

        from app import mcp_server

        guide = Path(
            ".agents/skills/money-market-intelligence/references/tool-guide.md"
        ).read_text()
        for tool in anyio.run(mcp_server.mcp.list_tools):
            self.assertIn(f"`{tool.name}`", guide, tool.name)


class CollectorStateFreshnessTests(unittest.TestCase):
    """A recovered collector must stop reporting the failure it recovered from.

    Otherwise the answer to "is collection healthy?" stays wrong until the
    collector's next scheduled run, which for a daily job can be a full day.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def _failed_then_healthy(self):
        from app.scheduler import Collector, Scheduler

        healthy = {"value": False}
        collector = Collector(
            name="probe",
            interval=3600,
            run=lambda: {"ok": 1, "total": 2, "errors": {"x": "boom"}},
            is_fresh=lambda: healthy["value"],
        )
        collector.execute(trigger="schedule")
        self.assertEqual("partial", db.get_collector_state("probe")["status"])
        self.assertIsNotNone(db.get_collector_state("probe")["error"])
        healthy["value"] = True
        scheduler = Scheduler()
        scheduler.register(collector)
        return scheduler

    def test_a_clean_audit_clears_the_previous_error(self):
        scheduler = self._failed_then_healthy()
        scheduler.reconcile()
        state = db.get_collector_state("probe")
        self.assertEqual("fresh", state["status"])
        self.assertIsNone(state["error"])

    def test_a_clean_cadence_check_also_clears_it(self):
        import time

        scheduler = self._failed_then_healthy()
        collector = scheduler.collectors[0]
        collector.due(time.time() + 7200)
        state = db.get_collector_state("probe")
        self.assertEqual("fresh", state["status"])
        self.assertIsNone(state["error"])

    def test_a_still_failing_collector_keeps_its_error(self):
        from app.scheduler import Collector, Scheduler

        collector = Collector(
            name="broken",
            interval=3600,
            run=lambda: {"ok": 0, "total": 2, "errors": {"x": "boom"}},
            is_fresh=lambda: False,
        )
        collector.execute(trigger="schedule")
        scheduler = Scheduler(repair_backoff=0, error_backoff=0)
        scheduler.register(collector)
        scheduler.reconcile()
        self.assertIsNotNone(db.get_collector_state("broken")["error"])
