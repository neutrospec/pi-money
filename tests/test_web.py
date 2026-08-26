"""Tests for the web surface.

The remodel exists because collected data was unreachable: 39,962 KRX
instruments and eight analysis endpoints had no screen at all. These tests
hold the two properties that made that possible — every page renders, and
every endpoint the server offers is reachable from somewhere in the UI.
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db


PAGES = (
    "/", "/rates", "/charts", "/stocks", "/indices", "/markets",
    "/analysis", "/correlation", "/spillover", "/data", "/manage", "/calendar",
)

# Endpoints no page fetches, each for a stated reason. Keeping the reason in
# the test is the point: without it this list quietly becomes the place where
# a forgotten endpoint goes to be forgotten again.
UI_EXEMPT = {
    "/api/situation": "the front page renders this payload server-side",
    "/api/analysis/regime": "included in the server-rendered situation payload",
    "/api/analysis/sentiment": "included in the server-rendered situation payload",
    "/api/events": "the calendar page renders events server-side",
    "/api/health": "/data reads the richer /api/coverage instead",
    "/api/categories": "superseded by /api/indicator/categories",
    "/api/quote/{symbol}": "single-quote lookup for agents",
    "/api/market/daily": "called with query parameters from /markets",
    "/api/analysis/volatility": "/api/analysis/trend already returns volatility",
    "/api/analysis/yield_curve": (
        "two-point spread kept for agents; the UI uses the full "
        "/api/analysis/curve"
    ),
}


def _client():
    from fastapi.testclient import TestClient

    from app import main

    return TestClient(main.app)


class PageRenderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def test_every_page_renders_on_an_empty_cache(self):
        # A fresh install has no data; a page that only works once collection
        # has run is a page that greets every new user with a stack trace.
        with patch.dict(os.environ, {"MONEY_DISABLE_SCHEDULER": "1"}), patch(
            "app.collectors.indicators.fetch_indicator",
            side_effect=AssertionError("live call"),
        ):
            with _client() as client:
                for path in PAGES:
                    with self.subTest(path=path):
                        self.assertEqual(200, client.get(path).status_code)

    def test_no_page_leaks_an_unrendered_template_tag(self):
        with patch.dict(os.environ, {"MONEY_DISABLE_SCHEDULER": "1"}):
            with _client() as client:
                for path in PAGES:
                    body = client.get(path).text
                    self.assertNotIn("{{", body, path)
                    self.assertNotIn("{%", body, path)

    def test_navigation_reaches_every_page(self):
        with patch.dict(os.environ, {"MONEY_DISABLE_SCHEDULER": "1"}):
            with _client() as client:
                bodies = {path: client.get(path).text for path in PAGES}
        linked = set()
        for body in bodies.values():
            linked.update(re.findall(r'href="(/[a-z]*)"', body))
        for path in PAGES:
            self.assertIn(path, linked, f"{path} is not linked from any page")


class ApiReachabilityTests(unittest.TestCase):
    """Every endpoint should be reachable from the UI or explicitly exempt."""

    def test_no_endpoint_is_stranded_without_a_screen(self):
        from app import main

        offered = {
            route.path for route in main.app.routes
            if getattr(route, "path", "").startswith("/api/")
        }
        markup = "\n".join(
            path.read_text() for path in Path("app/templates").glob("*.html")
        )
        stranded = []
        for endpoint in sorted(offered - set(UI_EXEMPT)):
            # Templates build some paths by concatenation, so match the stem.
            stem = endpoint.split("{")[0].rstrip("/")
            if stem not in markup:
                stranded.append(endpoint)
        self.assertEqual([], stranded, f"UI cannot reach: {stranded}")

    def test_every_exemption_states_a_reason(self):
        for endpoint, reason in UI_EXEMPT.items():
            self.assertTrue(reason.strip(), endpoint)

    def test_the_exempt_list_only_names_real_endpoints(self):
        from app import main

        offered = {
            route.path for route in main.app.routes
            if getattr(route, "path", "").startswith("/api/")
        }
        self.assertEqual(set(), set(UI_EXEMPT) - offered)


class CurveTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def _seed(self):
        for key, value in (
            ("kr_treasury_1y", 3.4), ("kr_treasury_2y", 3.7),
            ("kr_treasury_3y", 3.8), ("kr_treasury_5y", 4.0),
            ("kr_treasury_10y", 4.3), ("kr_treasury_30y", 4.6),
        ):
            db.save_indicator_points(key, [
                {"date": "2026-08-24", "value": value - 0.1},
                {"date": "2026-08-25", "value": value},
            ], source="ecos_raw")

    def test_a_curve_uses_one_date_every_tenor_reported(self):
        from app import analysis

        self._seed()
        # A tenor that stopped a day early must pull the curve back, not be
        # mixed in from a different session.
        db.save_indicator_points(
            "kr_treasury_30y", [{"date": "2026-08-24", "value": 4.5}], source="ecos_raw"
        )
        with db.get_conn() as conn:
            conn.execute(
                "DELETE FROM indicator_points "
                "WHERE indicator='kr_treasury_30y' AND date='2026-08-25'"
            )
        keys = [key for _, key, _ in analysis.CURVE_TENORS["kr"]]
        result = analysis.yield_curve(
            {key: db.get_indicator_points(key) for key in keys}, "kr"
        )
        self.assertEqual("2026-08-24", result["as_of"])
        self.assertEqual(6, len(result["points"]))

    def test_an_inverted_segment_is_named(self):
        from app import analysis

        self._seed()
        db.save_indicator_points(
            "kr_treasury_10y", [{"date": "2026-08-25", "value": 3.5}], source="ecos_raw"
        )
        keys = [key for _, key, _ in analysis.CURVE_TENORS["kr"]]
        result = analysis.yield_curve(
            {key: db.get_indicator_points(key) for key in keys}, "kr"
        )
        self.assertIn(("5년", "10년"), [tuple(x) for x in result["inverted_segments"]])

    def test_too_few_tenors_reports_an_error_rather_than_a_line(self):
        from app import analysis

        db.save_indicator_points(
            "kr_treasury_3y", [{"date": "2026-08-25", "value": 3.8}], source="ecos_raw"
        )
        keys = [key for _, key, _ in analysis.CURVE_TENORS["kr"]]
        result = analysis.yield_curve(
            {key: db.get_indicator_points(key) for key in keys}, "kr"
        )
        self.assertIn("error", result)

    def test_an_unknown_country_is_refused(self):
        from app import analysis

        self.assertIn("error", analysis.yield_curve({}, "jp"))


if __name__ == "__main__":
    unittest.main()


class InstrumentSearchTests(unittest.TestCase):
    """A search must surface what was asked for, not what sorts first."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"
        db.init_db()
        rows = [
            ("drvprod_dd_trd", "KRX0001", "KRX 삼성전자 TR 레버리지 지수", "derivative_index"),
            ("drvprod_dd_trd", "KRX0002", "KRX 삼성전자 TR 인버스 -1X 지수", "derivative_index"),
            ("eqsfu_stk_bydd_trd", "A1169000", "삼성전자   F 202609", "future"),
            ("stk_bydd_trd", "005930", "삼성전자", "stock"),
            ("stk_bydd_trd", "005935", "삼성전자우", "stock"),
        ]
        for dataset, symbol, name, asset_type in rows:
            db.save_market_batch("krx", dataset, "2026-08-25", [{
                "symbol": symbol, "name": name, "asset_type": asset_type,
                "market": "KOSPI", "currency": "KRW", "date": "2026-08-25",
                "close": 1.0, "change": 0.0, "change_pct": 0.0,
                "open": 1.0, "high": 1.0, "low": 1.0,
                "volume": 1.0, "turnover": 1.0, "market_cap": 1.0,
                "metadata": {}, "raw": {},
            }])

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def test_the_exact_stock_outranks_derivative_products(self):
        found = db.get_market_instruments(source="krx", query="삼성전자", limit=5)
        # Ordering by dataset name previously buried 005930 under leveraged
        # index products that merely mention it.
        self.assertEqual("005930", found[0]["symbol"])
        self.assertEqual("stock", found[0]["asset_type"])

    def test_a_symbol_query_finds_its_instrument_first(self):
        found = db.get_market_instruments(source="krx", query="005935", limit=5)
        self.assertEqual("005935", found[0]["symbol"])

    def test_an_unfiltered_listing_keeps_its_stable_order(self):
        found = db.get_market_instruments(source="krx", limit=99)
        datasets = [item["dataset"] for item in found]
        self.assertEqual(sorted(datasets), datasets)
