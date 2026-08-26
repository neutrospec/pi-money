"""Tests for the coverage audit.

The audit exists because staleness alone cannot answer "is this evidence
complete?".  These tests pin the two judgements it must never confuse: a
weekend with no traded price is not a gap, and a weekend with no standing
policy rate is.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import coverage, db


class ExpectedDateTests(unittest.TestCase):
    def test_monthly_series_expects_every_month_start(self):
        held = ["2026-01-01", "2026-02-01", "2026-04-01"]
        expected = coverage.expected_dates(
            held, date_kind="period_start", frequency="M"
        )
        self.assertEqual(
            expected, ["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"]
        )

    def test_monthly_expectation_crosses_a_year_boundary(self):
        expected = coverage.expected_dates(
            ["2025-11-01", "2026-02-01"], date_kind="period_start", frequency="M"
        )
        self.assertEqual(
            expected,
            ["2025-11-01", "2025-12-01", "2026-01-01", "2026-02-01"],
        )

    def test_quarterly_series_expects_quarter_starts_only(self):
        expected = coverage.expected_dates(
            ["2025-04-01", "2026-01-01"], date_kind="period_start", frequency="Q"
        )
        self.assertEqual(
            expected, ["2025-04-01", "2025-07-01", "2025-10-01", "2026-01-01"]
        )

    def test_weekly_series_expects_a_seven_day_stride(self):
        expected = coverage.expected_dates(
            ["2026-08-01", "2026-08-22"], date_kind="period_start", frequency="W"
        )
        self.assertEqual(
            expected, ["2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22"]
        )

    def test_calendar_day_series_expects_weekends_too(self):
        # A standing policy rate is in effect on Saturday, so its absence is
        # a real gap rather than a market holiday.
        expected = coverage.expected_dates(
            ["2026-08-21", "2026-08-24"], date_kind="calendar_day", frequency="D"
        )
        self.assertEqual(
            expected, ["2026-08-21", "2026-08-22", "2026-08-23", "2026-08-24"]
        )

    def test_traded_series_cannot_be_derived_locally(self):
        # Deriving these would require a trading calendar, and guessing with a
        # weekday rule would invent gaps on every market holiday.
        self.assertIsNone(
            coverage.expected_dates(
                ["2026-08-21", "2026-08-24"], date_kind="trading_day", frequency="D"
            )
        )

    def test_a_series_too_short_to_have_a_shape_is_not_judged(self):
        self.assertIsNone(
            coverage.expected_dates([], date_kind="period_start", frequency="M")
        )
        self.assertIsNone(
            coverage.expected_dates(
                ["2026-01-01"], date_kind="period_start", frequency="M"
            )
        )


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def test_a_monthly_hole_is_reported_as_a_candidate_gap(self):
        db.save_indicator_points("us_cpi", [
            {"date": "2026-01-01", "value": 1.0},
            {"date": "2026-02-01", "value": 2.0},
            {"date": "2026-04-01", "value": 3.0},
        ], source="fred")
        row = next(
            item for item in coverage.indicator_coverage() if item["key"] == "us_cpi"
        )
        self.assertEqual(row["gaps"]["basis"], coverage.CANDIDATE)
        self.assertEqual(row["gaps"]["missing_count"], 1)
        self.assertEqual(row["gaps"]["missing_sample"], ["2026-03-01"])

    def test_a_traded_series_without_a_manifest_is_unverifiable_not_broken(self):
        db.save_indicator_points("us_10y", [
            {"date": "2026-08-20", "value": 4.0},
            {"date": "2026-08-24", "value": 4.1},
        ], source="fred")
        row = next(
            item for item in coverage.indicator_coverage() if item["key"] == "us_10y"
        )
        self.assertEqual(row["gaps"]["basis"], coverage.UNVERIFIABLE)
        self.assertEqual(row["gaps"]["missing_count"], 0)

    def test_a_provider_manifest_outranks_the_derived_cadence(self):
        db.save_indicator_points("us_cpi", [
            {"date": "2026-01-01", "value": 1.0},
            {"date": "2026-04-01", "value": 3.0},
        ], source="fred")
        db.ensure_recovery_target(
            layer="historical", kind="indicator_history", target="us_cpi",
            scope="provider_snapshot", fingerprint="test",
        )
        db.update_recovery_target(
            "historical", "indicator_history", "us_cpi", "provider_snapshot",
            status="complete",
            manifest=["2026-01-01", "2026-02-01", "2026-04-01"],
        )
        row = next(
            item for item in coverage.indicator_coverage() if item["key"] == "us_cpi"
        )
        # The provider never published March, so only February counts.
        self.assertEqual(row["gaps"]["basis"], coverage.CONFIRMED)
        self.assertEqual(row["gaps"]["missing_sample"], ["2026-02-01"])

    def test_an_index_behind_its_provider_session_is_flagged(self):
        db.save_index_points("^KS11", [
            {"date": "2026-08-20", "value": 1.0},
            {"date": "2026-08-21", "value": 2.0},
        ])
        db.save_index_quote({
            "symbol": "^KS11", "price": 3.0, "prev_close": 2.0,
            "currency": "KRW", "session_date": "2026-08-24",
            "updated_at": db.utc_now(),
        })
        row = next(
            item for item in coverage.index_coverage() if item["symbol"] == "^KS11"
        )
        self.assertEqual(row["tail"], "behind_provider")
        self.assertIn("^KS11", coverage.deficits()["indices"])

    def test_an_index_matching_its_provider_session_is_current(self):
        db.save_index_points("^KS11", [
            {"date": "2026-08-20", "value": 1.0},
            {"date": "2026-08-24", "value": 2.0},
        ])
        db.save_index_quote({
            "symbol": "^KS11", "price": 3.0, "prev_close": 2.0,
            "currency": "KRW", "session_date": "2026-08-24",
            "updated_at": db.utc_now(),
        })
        row = next(
            item for item in coverage.index_coverage() if item["symbol"] == "^KS11"
        )
        self.assertEqual(row["tail"], "current")
        self.assertNotIn("^KS11", coverage.deficits()["indices"])

    def test_an_empty_cache_reports_incomplete_rather_than_ok(self):
        report = coverage.audit()
        self.assertEqual(report["status"], "incomplete")
        self.assertGreater(report["unresolved"], 0)

    def test_audit_shape_is_stable_for_agents(self):
        report = coverage.audit()
        for field in (
            "status", "as_of", "indicators", "indices", "core",
            "core_ready_pct", "unresolved", "attention", "method", "cached",
        ):
            self.assertIn(field, report)


if __name__ == "__main__":
    unittest.main()


class NewSourceTests(unittest.TestCase):
    """The sources added to replace stalled OECD relays and fill the KRW curve."""

    def test_korean_money_market_curve_is_catalogued(self):
        from app.collectors import indicators

        catalog = indicators.catalog()
        # The point of the project is the Korean money market, so the curve
        # has to reach past five years and include the risk-free rate.
        for key in (
            "kr_kofr", "kr_cp_91d", "kr_msb_91d", "kr_treasury_1y",
            "kr_treasury_2y", "kr_treasury_10y", "kr_treasury_30y",
            "kr_corp_bond_bbb",
        ):
            self.assertIn(key, catalog, key)
            self.assertEqual(catalog[key]["source"], "ecos_raw", key)
            self.assertEqual(catalog[key]["frequency"], "D", key)

    def test_stalled_oecd_relays_were_replaced_by_their_own_central_banks(self):
        from app.collectors import indicators

        catalog = indicators.catalog()
        self.assertEqual(catalog["gb_rate"]["source"], "boe")
        self.assertEqual(catalog["eu_3m_rate"]["source"], "ecb")
        # date_kind follows the provider's publication calendar, not whether
        # the rate is conceptually in force. The BoE publishes Bank Rate on
        # business days only, so classifying it as calendar_day invented 350
        # weekend gaps; ECOS really does publish the Korean base rate daily.
        self.assertEqual(catalog["gb_rate"]["date_kind"], "trading_day")
        self.assertEqual(catalog["gb_sonia"]["date_kind"], "trading_day")
        self.assertEqual(catalog["kr_base_rate"]["date_kind"], "calendar_day")

    def test_every_source_resolves_a_url_and_a_frequency(self):
        from app.collectors import indicators

        for key, spec in indicators.catalog().items():
            self.assertTrue(spec["source_url"].startswith("https://"), key)
            self.assertEqual(
                spec["frequency"], indicators.cycle_of(key), key
            )

    def test_raw_ecos_series_declare_a_known_table(self):
        from app.collectors import indicators

        for key, spec in indicators.catalog().items():
            if spec["source"] != "ecos_raw":
                continue
            table = spec["series"].split("/", 1)[0]
            self.assertIn(table, indicators.ECOS_RAW_FREQUENCIES, key)

    def test_korean_term_spread_uses_the_ten_year(self):
        from app import mcp_server

        source = Path("app/mcp_server.py").read_text()
        self.assertIn('"kr_treasury_10y"', source)
        self.assertNotIn('"kr_treasury_5y", "한국"', source)


class ProviderStallTests(unittest.TestCase):
    """A widened allowance must not double as a way to hide a stalled source.

    Series relayed through OECD's MEI database stopped updating in mid-2025.
    Their allowances were widened so the repair loop stops re-requesting an
    unchanged series, but a seventeen-month-old CPI is still a limit on the
    evidence and has to stay visible.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def test_a_series_inside_a_widened_allowance_is_still_reported(self):
        from datetime import timedelta

        from app.collectors import indicators
        from app.timeutil import kst_today

        spec = indicators.catalog()["gb_cpi"]
        # Old enough to beat the normal monthly cadence, young enough to sit
        # inside the widened allowance.
        normal = indicators.DEFAULT_MAX_AGE_DAYS[spec["frequency"]]
        self.assertLess(normal, spec["max_age_days"])
        observed = kst_today() - timedelta(days=normal + 30)
        db.save_indicator_points(
            "gb_cpi",
            [{"date": observed.replace(day=1).isoformat(), "value": 100.0}],
            source="fred",
        )
        row = next(
            item for item in coverage.indicator_coverage()
            if item["key"] == "gb_cpi"
        )
        self.assertEqual(row["tail"], "fresh")
        self.assertTrue(row["provider_stalled"])
        self.assertIn("provider_stalled", coverage.audit()["attention"])

    def test_a_genuinely_current_series_is_not_flagged_as_stalled(self):
        from app.timeutil import kst_today

        db.save_indicator_points(
            "us_10y",
            [{"date": kst_today().isoformat(), "value": 4.3}],
            source="fred",
        )
        row = next(
            item for item in coverage.indicator_coverage()
            if item["key"] == "us_10y"
        )
        self.assertFalse(row["provider_stalled"])

    def test_the_dashboard_badge_carries_the_stall_count(self):
        from app import dashboard

        self.assertIn("provider_stalled", dashboard.freshness())


class SetupGuideTests(unittest.TestCase):
    """Onboarding docs must not drift from what the code actually needs.

    A setup guide that names a variable the code no longer reads, or omits
    one it does, costs a newcomer more time than having no guide at all.
    """

    def test_the_template_declares_every_variable_the_code_requires(self):
        template = Path(".env.example").read_text()
        for variable in ("ECOS_API_KEY", "FRED_API_KEY", "KRX_API_KEY"):
            self.assertIn(f"{variable}=", template, variable)

    def test_the_template_ships_no_real_values(self):
        import re

        for line in Path(".env.example").read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            self.assertIsNotNone(
                re.fullmatch(r"[A-Z0-9_]+=", stripped),
                f"template line carries a value: {stripped}",
            )

    def test_the_setup_guide_names_the_krx_services_that_matter(self):
        guide = Path("docs/sources/setup.md").read_text()
        # These two are the whole point of the KRX section; the guide has to
        # name them exactly, because the portal lists a similarly named index
        # service that does not work for breadth.
        self.assertIn("sto/stk_bydd_trd", guide)
        self.assertIn("sto/ksq_bydd_trd", guide)
        self.assertIn("idx/kospi_dd_trd", guide)  # the confusable one
        self.assertIn("app.doctor", guide)

    def test_the_doctor_checks_every_required_credential(self):
        from app import doctor

        source = Path("app/doctor.py").read_text()
        for variable in ("ECOS_API_KEY", "FRED_API_KEY", "KRX_API_KEY"):
            self.assertIn(variable, source, variable)
        # The datasets the doctor calls required must exist in the collector.
        from app.collectors import krx

        known = {spec["dataset"] for spec in krx.dataset_specs()}
        for dataset in doctor.KRX_REQUIRED:
            self.assertIn(dataset, known, dataset)
        for dataset in doctor.KRX_USEFUL:
            self.assertIn(dataset, known, dataset)

    def test_entry_points_link_to_the_setup_guide(self):
        for path in ("README.md", "docs/README.md", "docs/sources/README.md"):
            self.assertIn("setup.md", Path(path).read_text(), path)
