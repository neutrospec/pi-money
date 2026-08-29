"""Deterministic tests for storage, calendar, scheduler, and analysis."""
from __future__ import annotations

import math
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app import (
    analysis, backup, correlation, db, history_recovery, market_metrics,
    dashboard, normalize, registry, sentiment, spillover,
)
from app.collectors import curated, indicators, krx
from app.scheduler import Collector, Scheduler
from app.timeutil import kst_today


def points(values: list[float], start: date = date(2025, 1, 1)) -> list[dict]:
    return [
        {"date": (start + timedelta(days=index)).isoformat(), "value": value}
        for index, value in enumerate(values)
    ]


class TemporaryDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()


class CalendarTests(unittest.TestCase):
    def test_fomc_is_converted_across_kst_date_boundary(self):
        events = curated.load()
        september = next(
            event for event in events
            if event["title"] == "FOMC 금리 결정"
            and event["source_date"] == "2026-09-16"
        )
        december = next(
            event for event in events
            if event["title"] == "FOMC 금리 결정"
            and event["source_date"] == "2026-12-09"
        )
        self.assertEqual(("2026-09-17", "03:00"), (september["date"], september["time"]))
        self.assertEqual(("2026-12-10", "04:00"), (december["date"], december["time"]))

    def test_unknown_official_time_remains_unknown(self):
        bok = next(event for event in curated.load() if event["country"] == "KR")
        self.assertIsNone(bok["time"])
        self.assertIsNone(bok["source_time"])

    def test_all_events_have_provenance_and_valid_impact(self):
        for event in curated.load():
            self.assertIn(event["impact"], {"high", "medium", "low"})
            self.assertTrue(event["source_url"].startswith("https://"))
            date.fromisoformat(event["date"])

    def test_imminent_bea_and_labor_detail_releases_are_not_omitted(self):
        events = curated.load()
        gdp = next(
            event for event in events
            if event["title"] == "미국 GDP" and event["source_date"] == "2026-08-26"
        )
        jolts = next(
            event for event in events
            if event["title"] == "미국 JOLTS" and event["source_date"] == "2026-09-01"
        )
        self.assertEqual(("2026-08-26", "21:30"), (gdp["date"], gdp["time"]))
        self.assertEqual(("2026-09-01", "23:00"), (jolts["date"], jolts["time"]))


class DatabaseTests(TemporaryDatabaseTest):
    def test_init_migrates_legacy_quarter_dates(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute(
                "CREATE TABLE indicator_points("
                "indicator TEXT, date TEXT, value REAL, "
                "PRIMARY KEY(indicator, date))"
            )
            conn.execute(
                "INSERT INTO indicator_points VALUES('gdp', '2026-Q2-01', 0.6)"
            )
        db.init_db()
        self.assertEqual(
            [{"date": "2026-04-01", "value": 0.6}],
            db.get_indicator_points("gdp"),
        )
        self.assertEqual("1", db.get_meta("migrated_quarter_dates"))

    def test_indicator_revisions_are_retained_as_vintages(self):
        db.init_db()
        db.save_indicator_points("x", [{"date": "2026-01-01", "value": 1.0}], "fred")
        db.save_indicator_points("x", [{"date": "2026-01-01", "value": 2.0}], "fred")
        with db.get_conn() as conn:
            vintages = conn.execute(
                "SELECT value FROM indicator_vintages "
                "WHERE indicator='x' ORDER BY retrieved_at"
            ).fetchall()
        self.assertEqual([1.0, 2.0], [row["value"] for row in vintages])
        self.assertEqual(2.0, db.get_indicator_points("x")[-1]["value"])

    def test_series_catalog_keeps_analysis_coverage_metadata(self):
        db.init_db()
        registry._sync_catalog()
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT analysis_group, priority, is_proxy, source_url, max_age_days, "
                "source_options_json FROM series_catalog "
                "WHERE key='us_equal_weight_proxy'"
            ).fetchone()
            selected = conn.execute(
                "SELECT source_options_json FROM series_catalog "
                "WHERE key='kr_all_industry_output'"
            ).fetchone()
        self.assertEqual("market_breadth", row["analysis_group"])
        self.assertEqual("core", row["priority"])
        self.assertEqual(1, row["is_proxy"])
        self.assertIn("RSP", row["source_url"])
        self.assertEqual("{}", row["source_options_json"])
        self.assertEqual(5, row["max_age_days"])
        self.assertEqual('{"item_code2":"2"}', selected["source_options_json"])
        self.assertEqual("8", db.get_meta("schema_version"))

    def test_replace_index_refuses_empty_input(self):
        db.init_db()
        with self.assertRaises(ValueError):
            db.replace_index_points("^TEST", [])

    def test_market_batch_discovers_universe_and_preserves_raw_fields(self):
        db.init_db()
        db.save_market_batch("krx", "stk_bydd_trd", "2026-08-21", [{
            "symbol": "005930",
            "name": "삼성전자",
            "asset_type": "stock",
            "market": "KOSPI",
            "currency": "KRW",
            "date": "2026-08-21",
            "close": 80000.0,
            "change": 1000.0,
            "change_pct": 1.27,
            "open": 79000.0,
            "high": 81000.0,
            "low": 78500.0,
            "volume": 12345.0,
            "turnover": 999999.0,
            "market_cap": 1000000.0,
            "metadata": {"provider_path": "sto/stk_bydd_trd"},
            "raw": {"LIST_SHRS": "5,969,782,550"},
        }])
        universe = db.get_market_instruments(source="krx", query="삼성")
        daily = db.get_market_daily(source="krx", symbol="005930")
        self.assertEqual("005930", universe[0]["symbol"])
        self.assertEqual("sto/stk_bydd_trd", universe[0]["metadata"]["provider_path"])
        self.assertEqual("5,969,782,550", daily[0]["raw"]["LIST_SHRS"])
        self.assertEqual("success", db.market_run_status("krx", "stk_bydd_trd", "2026-08-21"))
        self.assertEqual(1, db.market_overview("krx")["instruments"])

    def test_backup_is_consistent_and_keeps_source(self):
        db.init_db()
        db.set_meta("sentinel", "present")
        source = db.DB_PATH
        # Backups are compressed by default, so the archive has to be
        # expanded before it can be opened as a database.
        archive = backup.backup_database(Path(self.tempdir.name) / "backups")
        destination = backup.restore(archive, Path(self.tempdir.name) / "restored.db")
        self.assertTrue(source.exists())
        self.assertTrue(archive.exists())
        with sqlite3.connect(destination) as conn:
            self.assertEqual("ok", conn.execute("PRAGMA quick_check").fetchone()[0])
            self.assertEqual(
                "present",
                conn.execute("SELECT value FROM meta WHERE key='sentinel'").fetchone()[0],
            )


class IndicatorTests(unittest.TestCase):
    def test_missing_fred_key_fails_before_network_request(self):
        with patch.dict(os.environ, {"FRED_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "FRED_API_KEY"):
                indicators.fred_series("DGS10")

    def test_fred_frequency_classification(self):
        self.assertEqual("M", indicators.cycle_of("us_rate"))
        self.assertEqual("W", indicators.cycle_of("us_jobless"))
        self.assertEqual("Q", indicators.cycle_of("us_gdp"))
        self.assertEqual("D", indicators.cycle_of("us_10y"))
        self.assertEqual("D", indicators.cycle_of("us_sofr"))
        self.assertEqual("W", indicators.cycle_of("us_nfci"))
        self.assertEqual("W", indicators.cycle_of("us_fed_assets"))
        self.assertEqual("M", indicators.cycle_of("us_cfnai"))
        self.assertEqual("W", indicators.cycle_of("us_tga"))
        self.assertEqual("D", indicators.cycle_of("us_on_rrp"))
        self.assertEqual("Q", indicators.cycle_of("us_bank_lending_standards"))
        self.assertEqual("Q", indicators.cycle_of("kr_hourly_wage"))

    def test_catalog_is_well_formed(self):
        catalog = indicators.catalog()
        self.assertEqual(168, len(catalog))
        for key, spec in catalog.items():
            self.assertTrue(key)
            self.assertIn(
                spec["source"],
                {"fred", "ecos", "ecos_raw", "ecb", "boe", "yahoo", "krx"},
            )
            self.assertIn(spec["category"], indicators.categories())
            self.assertIn(spec["priority"], {"core", "supporting"})
            self.assertTrue(spec["analysis_group"])
            self.assertIn(spec["frequency"], {"D", "W", "M", "Q", "A"})
            self.assertEqual(spec["frequency"], indicators.cycle_of(key))
            self.assertGreater(spec["max_age_days"], 0)
            self.assertIsInstance(spec["source_options"], dict)
            self.assertTrue(spec["source_url"].startswith("https://"))
        self.assertEqual("원", catalog["kr_investor_deposits"]["unit"])
        self.assertTrue(catalog["us_high_yield_proxy"]["proxy"])
        self.assertTrue(catalog["us_lqd_proxy"]["proxy"])
        self.assertEqual("2020=100", catalog["kr_import_price"]["unit"])
        self.assertEqual(14, catalog["us_term_premium_10y"]["max_age_days"])
        self.assertEqual(
            set(),
            {
                spec["series"] for spec in catalog.values() if spec["source"] == "fred"
            } - set(indicators.FRED_FREQUENCIES),
        )

    def test_ecos_same_date_variants_require_an_explicit_selector(self):
        class Node:
            spec = type("Spec", (), {"cycle": "M"})()

            def fetch(self, **_kwargs):
                return [
                    {"time": "202607", "data_value": "100", "item_code2": "1"},
                    {"time": "202607", "data_value": "101", "item_code2": "2"},
                ]

        class Client:
            series = Node()

            def close(self):
                pass

        with patch("pyecos.ECOS", return_value=Client()):
            with self.assertRaisesRegex(ValueError, "declare an ECOS selector"):
                indicators.ecos_series("series")
        with patch("pyecos.ECOS", return_value=Client()):
            selected = indicators.ecos_series(
                "series", selectors={"item_code2": "2"}
            )
        self.assertEqual([{"date": "2026-07-01", "value": 101.0}], selected)


class KrxTests(unittest.TestCase):
    def test_balanced_scope_collects_the_cash_market_and_index_options(self):
        datasets = {item["dataset"] for item in krx.dataset_specs("balanced")}
        self.assertTrue({"kospi_dd_trd", "stk_bydd_trd", "ksq_bydd_trd", "etf_bydd_trd"} <= datasets)
        # Index options joined the default scope because the put/call ratio is
        # a sentiment input with no substitute elsewhere in the catalog.
        self.assertIn("opt_bydd_trd", datasets)
        # Single-stock options and warrants stay out: far thinner, and nothing
        # in the analysis consumes them.
        self.assertNotIn("elw_bydd_trd", datasets)
        self.assertNotIn("eqsop_bydd_trd", datasets)
        self.assertEqual(31, len(krx.dataset_specs("all")))

    def test_aggregation_tags_name_a_known_summariser(self):
        tagged = {
            item["dataset"]: item["aggregate"]
            for item in krx.DATASETS if item.get("aggregate")
        }
        self.assertEqual(
            {"opt_bydd_trd": "put_call", "drvprod_dd_trd": "named_index"}, tagged
        )

    def test_vkospi_is_lifted_out_of_the_bulk_index_table(self):
        # It arrives as one row among 320 derivative indices; a consumer must
        # not have to know which row it hides in.
        points = krx.extract_named_indices([
            {"IDX_NM": "코스피 200 변동성지수", "CLSPRC_IDX": "56.29"},
            {"IDX_NM": "코스피 200 가치저변동성", "CLSPRC_IDX": "1234.5"},
        ], "2026-08-25")
        self.assertEqual(
            [{"indicator": "kr_vkospi", "date": "2026-08-25", "value": 56.29}],
            points,
        )

    def test_a_named_index_without_a_close_is_skipped(self):
        self.assertEqual([], krx.extract_named_indices(
            [{"IDX_NM": "코스피 200 변동성지수", "CLSPRC_IDX": ""}], "2026-08-25"
        ))

    def test_collector_fed_series_are_not_fetched_one_key_at_a_time(self):
        # They are catalogued so agents can discover them, but requesting them
        # from a provider would fail on every run.
        from app import registry
        from app.collectors import indicators

        daily = [
            key for key in indicators.catalog()
            if indicators.cycle_of(key) == "D"
        ]
        self.assertIn("kr_vkospi", daily)
        self.assertTrue(indicators.is_collector_fed("kr_vkospi"))
        with patch.object(
            indicators, "fetch_keys_into_db", return_value={}
        ) as fetch:
            registry._run_indicators({"D"})
        requested = fetch.call_args[0][0]
        self.assertNotIn("kr_vkospi", requested)
        self.assertNotIn("kr_put_call_volume", requested)
        self.assertIn("us_10y", requested)

    def test_put_call_ratio_uses_index_options_from_the_regular_session(self):
        rows = [
            # counted: KOSPI 200 index options, regular session
            {"PROD_NM": "코스피200 옵션", "RGHT_TP_NM": "CALL", "ISU_NM": "코스피200 C (정규)",
             "ACC_TRDVOL": "100", "ACC_TRDVAL": "1,000", "ACC_OPNINT_QTY": "50"},
            {"PROD_NM": "코스피200 옵션", "RGHT_TP_NM": "PUT", "ISU_NM": "코스피200 P (정규)",
             "ACC_TRDVOL": "150", "ACC_TRDVAL": "3,000", "ACC_OPNINT_QTY": "100"},
            # excluded: overnight session would double-count one trading day
            {"PROD_NM": "코스피200 옵션", "RGHT_TP_NM": "PUT", "ISU_NM": "코스피200 P (야간)",
             "ACC_TRDVOL": "9999", "ACC_TRDVAL": "9999", "ACC_OPNINT_QTY": "9999"},
            # excluded: KOSDAQ150 is a different underlying
            {"PROD_NM": "코스닥150 옵션", "RGHT_TP_NM": "PUT", "ISU_NM": "코스닥150 P (정규)",
             "ACC_TRDVOL": "5000", "ACC_TRDVAL": "5000", "ACC_OPNINT_QTY": "5000"},
        ]
        points = {p["indicator"]: p for p in krx.aggregate_put_call(rows, "2026-08-25")}
        self.assertEqual(1.5, points["kr_put_call_volume"]["value"])
        self.assertEqual(3.0, points["kr_put_call_value"]["value"])
        self.assertEqual(2.0, points["kr_put_call_open_interest"]["value"])
        self.assertEqual("2026-08-25", points["kr_put_call_volume"]["date"])

    def test_aggregation_is_silent_when_no_calls_traded(self):
        self.assertEqual([], krx.aggregate_put_call([], "2026-08-25"))

    def test_option_rows_keep_their_right_and_implied_volatility(self):
        spec = next(item for item in krx.DATASETS if item["dataset"] == "opt_bydd_trd")
        row = krx.normalize_row(spec, {
            "BAS_DD": "20260825", "ISU_CD": "B0169335",
            "ISU_NM": "코스피200 C 202609 335.0 (정규)",
            "PROD_NM": "코스피200 옵션", "RGHT_TP_NM": "CALL",
            "TDD_CLSPRC": "1.23", "IMP_VOLT": "64.00",
            "ACC_TRDVOL": "10", "ACC_OPNINT_QTY": "500",
        }, "2026-08-25")
        self.assertEqual("CALL", row["metadata"]["right"])
        self.assertEqual(64.0, row["metadata"]["implied_volatility"])
        self.assertEqual(500.0, row["metadata"]["open_interest"])
        # The provider payload is still kept whole alongside the promotion.
        self.assertEqual("코스피200 옵션", row["raw"]["PROD_NM"])

    def test_krx_row_is_normalized_and_keeps_provider_payload(self):
        spec = next(item for item in krx.DATASETS if item["dataset"] == "stk_bydd_trd")
        row = krx.normalize_row(spec, {
            "BAS_DD": "20260821",
            "ISU_CD": "005930",
            "ISU_NM": "삼성전자",
            "MKT_NM": "KOSPI",
            "TDD_CLSPRC": "80,000",
            "FLUC_RT": "1.25",
            "ACC_TRDVOL": "12,345",
            "LIST_SHRS": "5,969,782,550",
        }, "2026-08-21")
        self.assertEqual(("005930", "2026-08-21", 80000.0), (row["symbol"], row["date"], row["close"]))
        self.assertEqual(5969782550, int(row["raw"]["LIST_SHRS"].replace(",", "")))

    def test_catchup_dates_skip_weekends(self):
        self.assertEqual(
            ["2026-08-19", "2026-08-20", "2026-08-21"],
            krx.catchup_dates(3, today=date(2026, 8, 24)),
        )


class AnalysisTests(unittest.TestCase):
    def test_term_spread_uses_latest_common_date(self):
        short = [
            {"date": "2026-01-01", "value": 3.0},
            {"date": "2026-01-03", "value": 3.2},
        ]
        long = [
            {"date": "2026-01-01", "value": 3.5},
            {"date": "2026-01-02", "value": 3.7},
        ]
        result = analysis.term_spread(short, long)
        self.assertEqual("2026-01-01", result["date"])
        self.assertEqual(0.5, result["spread"])

    def test_max_drawdown_keeps_peak_associated_with_trough(self):
        result = analysis.max_drawdown([
            {"date": "d1", "value": 100},
            {"date": "d2", "value": 50},
            {"date": "d3", "value": 200},
        ])
        self.assertEqual(-50.0, result["max_drawdown_pct"])
        self.assertEqual("d1", result["peak_date"])
        self.assertEqual("d2", result["trough_date"])

    def test_flat_rsi_is_neutral(self):
        self.assertEqual(50.0, analysis.rsi(points([100.0] * 30)))

    def test_var_is_never_reported_as_negative_loss(self):
        rising = points([100 + index for index in range(100)])
        self.assertEqual(0.0, analysis.value_at_risk(rising)["var_1day_pct"])


class NormalizationEngineTests(TemporaryDatabaseTest):
    """One definition of "where does this value sit in its own distribution"."""

    def test_extracted_primitive_reproduces_both_former_implementations(self):
        """The refactor net: the two copies must survive as one, bit for bit.

        ``analysis._risk_on_percentile`` and ``sentiment._percentile_score``
        were byte-identical duplicates. Anything the new primitive returns has
        to match what both used to, including the edge conventions — the
        empty-history 50.0 is load-bearing for a component that has a value
        but no distribution yet.
        """
        cases = [
            (56.0, [20.0] * 100 + [80.0] * 100),
            (0.688, [0.5] * 250),
            (1.0, [1.0] * 10),                    # ties are not "below"
            (5.0, []),                            # no distribution at all
            (-0.5, [-1.0, 0.0, 1.0]),
        ]
        for value, history in cases:
            for invert in (False, True):
                with self.subTest(value=value, invert=invert):
                    expected = analysis._risk_on_percentile(
                        value, history, invert=invert
                    )
                    self.assertEqual(
                        expected, normalize.percentile(value, history, invert=invert)
                    )
                    self.assertEqual(
                        expected,
                        sentiment._percentile_score(value, history, invert=invert),
                    )

    def test_window_policy_comes_from_the_catalog_not_the_call_site(self):
        """Mean-reverting levels get the whole record, everything else a year."""
        for key in indicators.MEAN_REVERTING_LEVELS:
            self.assertIsNone(normalize.window_for(key), key)
        self.assertEqual(
            normalize.TRAILING_WINDOW, normalize.window_for("kr_treasury_3y")
        )

    def test_unclassified_series_gets_no_risk_orientation(self):
        """A guessed direction would orient every downstream reading backwards."""
        points = [{"date": f"2026-01-{d:02d}", "value": float(d)} for d in range(1, 29)]
        plain = normalize.position(points * 3, direction=None, minimum=10)
        risky = normalize.position(
            points * 3, direction=indicators.RISK, minimum=10
        )
        self.assertIsNone(plain["risk_percentile"])
        self.assertIsNone(plain["direction"])
        self.assertIsNotNone(risky["risk_percentile"])
        self.assertEqual(plain["percentile"], 100.0 - risky["risk_percentile"])

    def test_thin_history_is_reported_not_estimated(self):
        reading = normalize.position([{"date": "2026-01-01", "value": 1.0}])
        self.assertFalse(reading["available"])
        self.assertIn("60", reading["reason"])
        self.assertEqual(1, reading["observations"])

    def test_dashboard_directions_are_the_catalog_declarations(self):
        """No second copy: the tile colour and the analysis orientation agree.

        The declaration used to live in dashboard.py, where nothing connected
        it to the percentile that reads the same property.
        """
        for _, keys in dashboard.HEADLINE_GROUPS:
            for key in keys:
                with self.subTest(key=key):
                    self.assertIsNotNone(
                        indicators.risk_direction(key),
                        f"{key}: 상황판 타일은 방향이 선언돼 있어야 합니다",
                    )

    def test_sentiment_volatility_uses_the_declared_window(self):
        """The gauge reads the policy rather than restating it.

        Both this gauge and the regime classifier score VKOSPI. While each
        carried its own window they disagreed — 38.4 against a trailing year,
        13.7 against the record — on the same observation.
        """
        db.init_db()
        values = [20.0] * 600 + [80.0] * 100 + [56.0]
        db.save_indicator_points(
            "kr_vkospi",
            [
                {"date": (date(2023, 1, 1) + timedelta(days=i)).isoformat(),
                 "value": value}
                for i, value in enumerate(values)
            ],
            "krx",
        )
        component = sentiment._volatility()
        reading = normalize.position_for("kr_vkospi")

        self.assertEqual(reading["risk_percentile"], component["score"])
        self.assertIn(reading["window_label"], component["method"])
        self.assertIn("전체", component["method"])  # not a trailing year

    def test_empty_history_reads_as_the_middle_not_as_absence(self):
        self.assertEqual(50.0, normalize.percentile(5.0, [], invert=False))
        self.assertEqual(50.0, normalize.percentile(5.0, [], invert=True))

    def test_ties_count_as_not_below(self):
        self.assertEqual(0.0, normalize.percentile(1.0, [1.0, 1.0], invert=False))
        self.assertEqual(100.0, normalize.percentile(1.0, [1.0, 1.0], invert=True))


class KoreaRegimeTests(TemporaryDatabaseTest):
    """The Korean classifier is percentile-based; these pin down why."""

    @staticmethod
    def _series(values: list[float]) -> list[dict]:
        """Daily points ending today, so the freshness gate lets them through."""
        today = kst_today()
        return [
            {
                "date": (today - timedelta(days=len(values) - 1 - index)).isoformat(),
                "value": float(value),
            }
            for index, value in enumerate(values)
        ]

    @staticmethod
    def _ramp(start: float, end: float, count: int) -> list[float]:
        return [start + (end - start) * i / (count - 1) for i in range(count)]

    def _bounce_inside_a_crash(self) -> list[dict]:
        """A long climb, a sharp break, a partial recovery — KOSPI in 2026-08."""
        return self._series(
            self._ramp(1000, 2000, 310) + self._ramp(2000, 4000, 140)
            + self._ramp(4000, 2600, 30) + self._ramp(2600, 3200, 30)
        )

    def test_spread_is_scored_against_its_own_distribution_not_an_absolute_cut(self):
        """The same 0.688%p reads as narrow or wide depending on its history.

        This is the whole reason the Korean classifier is not a copy of the US
        one: a 0.688 corporate spread is well inside the US "narrow" rule of
        thumb, yet it sat at the 87th percentile of its own recent range.
        """
        wide = analysis.korea_regime(
            None, self._series([0.5] * 250 + [0.688]),
            self._series([0.5] * 250 + [0.5]), None,
        )
        narrow = analysis.korea_regime(
            None, self._series([0.9] * 250 + [0.688]),
            self._series([0.5] * 250 + [0.5]), None,
        )
        credit_of = lambda r: next(  # noqa: E731
            c for c in r["components"] if c["key"] == "credit"
        )
        self.assertEqual(0.688, credit_of(wide)["value"])
        self.assertEqual(0.688, credit_of(narrow)["value"])
        self.assertEqual(-1, credit_of(wide)["score"])
        self.assertEqual(1, credit_of(narrow)["score"])

    def test_volatility_is_scored_against_its_whole_history_not_a_trailing_year(self):
        """A crisis inside the window must not become the window's baseline.

        Two calm years then a crisis: against the last 250 sessions — most of
        them already in the crisis — today's elevated reading looks ordinary.
        Against the whole record it is what it is. Spreads keep the trailing
        window because their normal level drifts with the rate cycle; implied
        volatility mean reverts, so it belongs with the drawdown component.
        """
        # Calm years dominate the record, but the crisis fills the last year.
        calm = [20.0] * 1500
        crisis = [80.0] * 260 + [56.0]
        vkospi = self._series(calm + crisis)
        spread = self._series([0.5] * 250 + [0.5])
        result = analysis.korea_regime(vkospi, spread, spread, None)
        volatility = next(
            c for c in result["components"] if c["key"] == "volatility"
        )

        # Trailing 250 would put 56 near the calm end of a crisis-only window
        # and score it risk-on; the full record ranks it above two calm years.
        trailing = analysis._risk_on_percentile(
            56.0, (calm + crisis)[-250:], invert=True
        )
        self.assertGreaterEqual(trailing, analysis.KR_RISK_ON_PERCENTILE)
        self.assertLessEqual(volatility["percentile"], analysis.KR_RISK_OFF_PERCENTILE)
        self.assertEqual(-1, volatility["score"])
        self.assertIn("전체", volatility["detail"])

    def test_trend_and_drawdown_disagree_on_a_bounce_inside_a_crash(self):
        """Above the 200-day average and deep below the 52-week high at once.

        The US classifier reads only the first half of that and calls it
        risk-on. Scoring both is what makes the state visible.
        """
        result = analysis.korea_regime(None, None, None, self._bounce_inside_a_crash())
        scores = {c["key"]: c["score"] for c in result["components"]}
        self.assertEqual(1, scores["trend"])
        self.assertEqual(-1, scores["drawdown"])

    def test_short_history_component_is_pending_not_guessed(self):
        result = analysis.korea_regime(
            self._series([55, 56, 57, 58, 59]), None, None,
            self._bounce_inside_a_crash(),
        )
        pending = {item["key"]: item["reason"] for item in result["pending"]}
        self.assertIn("volatility", pending)
        self.assertIn("50", pending["volatility"])
        self.assertIn("현재 5", pending["volatility"])
        self.assertNotIn(
            "volatility", {c["key"] for c in result["components"]}
        )

    def test_stale_component_is_excluded_with_a_reason(self):
        stale = self._series([0.5] * 250 + [0.688])
        for point in stale:  # push the whole series a fortnight into the past
            point["date"] = (
                date.fromisoformat(point["date"]) - timedelta(days=14)
            ).isoformat()
        result = analysis.korea_regime(None, stale, None, None)
        pending = {item["key"]: item["reason"] for item in result["pending"]}
        self.assertIn("credit", pending)
        self.assertIn("오래", pending["credit"])

    def test_declines_to_judge_below_the_minimum_component_count(self):
        # Only the trend component can report: a verdict from one reading
        # would be a guess wearing a verdict's clothes.
        result = analysis.korea_regime(
            None, None, None, self._series(self._ramp(1000, 2000, 300))
        )
        self.assertEqual("unknown", result["regime"])
        self.assertIsNone(result["ratio"])
        self.assertEqual(1, result["component_count"])

    def test_ratio_holds_the_bar_as_the_component_count_moves(self):
        """A net +2 is decisive out of four readings and is not out of five."""
        low_spread = self._series([0.9] * 60 + [0.4])
        kospi = self._bounce_inside_a_crash()
        four = analysis.korea_regime(None, low_spread, low_spread, kospi)
        self.assertEqual((4, 2), (four["component_count"], four["score"]))
        self.assertEqual("risk_on", four["regime"])

        # A fifth reading that leans neither way keeps the net score and
        # dilutes the majority; a raw-sum rule would ignore that.
        middling_vol = self._series(list(range(50)) + [25])
        five = analysis.korea_regime(middling_vol, low_spread, low_spread, kospi)
        self.assertEqual((5, 2), (five["component_count"], five["score"]))
        self.assertEqual("neutral", five["regime"])

    def test_shared_spread_alignment_feeds_the_classifier(self):
        """The classifier must read the same spread the risk panel shows."""
        db.init_db()
        db.save_indicator_points(
            "kr_corp_bond_3y", [{"date": "2026-08-28", "value": 4.476}], "ecos"
        )
        db.save_indicator_points(
            "kr_treasury_3y", [{"date": "2026-08-28", "value": 3.788}], "ecos"
        )
        series = market_metrics.aligned_spread_series(
            "kr_corp_bond_3y", "kr_treasury_3y"
        )
        self.assertEqual(0.688, series[-1]["value"])
        self.assertEqual(
            series[-1], market_metrics._aligned_difference(
                "kr_corp_bond_3y", "kr_treasury_3y"
            ),
        )


class MarketMetricsTests(TemporaryDatabaseTest):
    def setUp(self):
        super().setUp()
        db.init_db()

    def test_derived_snapshot_aligns_units_and_exposes_transformations(self):
        db.save_indicator_points("kr_corp_bond_3y", [
            {"date": "2026-08-21", "value": 4.5}
        ], "ecos")
        db.save_indicator_points("kr_treasury_3y", [
            {"date": "2026-08-21", "value": 3.8}
        ], "ecos")
        db.save_indicator_points("kr_treasury_5y", [
            {"date": "2026-08-21", "value": 3.9}
        ], "ecos")
        db.save_indicator_points("us_fed_assets", [
            {"date": "2026-08-19", "value": 8_000_000}
        ], "fred")
        db.save_indicator_points("us_tga", [
            {"date": "2026-08-19", "value": 500_000}
        ], "fred")
        db.save_indicator_points("us_on_rrp", [
            {"date": "2026-08-19", "value": 100}
        ], "fred")
        db.save_indicator_points("us_reserve_balances", [
            {"date": "2026-08-19", "value": 3_000_000}
        ], "fred")
        db.save_indicator_points("us_nfp", [
            {"date": "2026-06-01", "value": 159_000},
            {"date": "2026-07-01", "value": 159_025},
        ], "fred")
        relative_dates = points([100 + index for index in range(21)])
        db.save_indicator_points("us_discretionary_proxy", relative_dates, "yahoo")
        db.save_indicator_points(
            "us_staples_proxy", points([100.0] * 21), "yahoo"
        )

        result = market_metrics.derived_snapshot()

        self.assertEqual(0.7, result["macro"]["kr_credit_spread_3y"]["value"])
        self.assertEqual(
            7_400_000,
            result["macro"]["us_net_liquidity"]["net_liquidity_million_usd"],
        )
        self.assertEqual(
            25,
            result["macro"]["us_nfp_monthly_change"]["change_thousand"],
        )
        self.assertEqual(
            20.0,
            result["cross_asset"]["discretionary_vs_staples"]["relative_20d_pct"],
        )
        self.assertNotIn("us_tga", result["missing_inputs"])

    def test_krx_breadth_uses_latest_rows_and_bounded_history(self):
        start = date(2026, 7, 27)
        for index in range(20):
            day = (start + timedelta(days=index)).isoformat()
            db.save_market_batch("krx", "stk_bydd_trd", day, [
                {
                    "symbol": "000001", "name": "상승주", "asset_type": "stock",
                    "market": "KOSPI", "currency": "KRW", "date": day,
                    "close": 100 + index, "change": 1, "change_pct": 1,
                    "open": 100, "high": 101, "low": 99,
                    "volume": 100, "turnover": 1_000, "market_cap": 10_000,
                    "metadata": {}, "raw": {},
                },
                {
                    "symbol": "000002", "name": "하락주", "asset_type": "stock",
                    "market": "KOSPI", "currency": "KRW", "date": day,
                    "close": 200 - index, "change": -1, "change_pct": -1,
                    "open": 200, "high": 201, "low": 199,
                    "volume": 50, "turnover": 500, "market_cap": 5_000,
                    "metadata": {}, "raw": {},
                },
            ])

        result = market_metrics.krx_breadth_snapshot()
        kospi = result["markets"][0]
        kosdaq = result["markets"][1]

        self.assertEqual("ok", kospi["status"])
        self.assertEqual((1, 1), (kospi["advances"], kospi["declines"]))
        self.assertEqual(2, kospi["history_20d"]["eligible_issues"])
        self.assertEqual(1, kospi["history_20d"]["above_ma20"])
        self.assertEqual((1, 1), (
            kospi["history_20d"]["new_highs"],
            kospi["history_20d"]["new_lows"],
        ))
        self.assertEqual("unavailable", kosdaq["status"])


class CorrelationTests(unittest.TestCase):
    def setUp(self):
        x = [100 + math.sin(index / 5) + index * 0.2 for index in range(180)]
        y = [200 + 2 * math.sin(index / 5) + index * 0.4 for index in range(180)]
        self.a = points(x)
        self.b = points(y)

    def test_empty_matrix_returns_domain_error(self):
        self.assertIn("error", correlation.correlation_matrix({}))

    def test_pairwise_matrix_reports_observation_counts(self):
        result = correlation.correlation_matrix({"a": self.a, "b": self.b})
        self.assertEqual(["a", "b"], result["names"])
        self.assertGreater(result["observations"][0][1], 100)
        self.assertGreater(result["matrix"][0][1], 0.9)

    def test_invalid_lag_is_rejected_without_numpy_error(self):
        result = correlation.lead_lag(self.a, self.b, 100)
        self.assertEqual("max_lag must be between 0 and 20", result["error"])

    def test_lead_lag_includes_uncertainty_and_warning(self):
        result = correlation.lead_lag(self.a, self.b, 5)
        self.assertEqual(11, len(result["lags"]))
        self.assertEqual(11, len(result["p_values"]))
        self.assertIn("인과관계의 증거가 아닙니다", result["interpretation"])


class SpilloverTests(unittest.TestCase):
    def test_display_edges_keep_only_strongest_paths_per_sender(self):
        edges = [
            {"source": "a", "target": "b", "value": 0.3},
            {"source": "a", "target": "c", "value": 0.2},
            {"source": "a", "target": "d", "value": 0.1},
            {"source": "b", "target": "a", "value": 0.4},
            {"source": "b", "target": "c", "value": 0.1},
        ]
        selected = spillover._display_edges(edges, per_source=2)
        self.assertEqual(4, len(selected))
        self.assertNotIn(("a", "d"), {(edge["source"], edge["target"]) for edge in selected})
        self.assertLessEqual(
            max(sum(edge["source"] == source for edge in selected) for source in {"a", "b"}),
            2,
        )

    def test_generalized_connectedness_is_order_invariant(self):
        rng = np.random.default_rng(42)
        x, y = [100.0], [120.0]
        for _ in range(320):
            shock_x, shock_y = rng.normal(0, 0.5, 2)
            x.append(x[-1] * (1 + (0.15 * shock_y + shock_x) / 100))
            y.append(y[-1] * (1 + (0.20 * shock_x + shock_y) / 100))
        a, b = points(x), points(y)
        forward = spillover.spillover_network({"a": a, "b": b})
        reverse = spillover.spillover_network({"b": b, "a": a})
        self.assertNotIn("error", forward)
        self.assertNotIn("error", reverse)
        self.assertAlmostEqual(
            forward["total_connectedness"],
            reverse["total_connectedness"],
            places=3,
        )
        self.assertIn("display_edges", forward)
        self.assertLessEqual(len(forward["display_edges"]), len(forward["nodes"]) * 2)
        self.assertIn("구조적 인과관계", forward["causality_warning"])


class SchedulerTests(TemporaryDatabaseTest):
    def test_quote_batch_retains_provider_error_by_symbol(self):
        db.init_db()
        with patch("app.registry.watchlist.watchlist", return_value=[
            {"symbol": "FAIL", "label": "실패", "group": "테스트"}
        ]), patch("app.registry.quotes.quote", side_effect=RuntimeError("provider down")):
            result = registry._run_quotes()
        self.assertEqual((0, 1), (result["ok"], result["total"]))
        self.assertIn("provider down", result["errors"]["FAIL"])

    def test_cadence_survives_new_collector_instance(self):
        db.init_db()
        calls = []

        def run():
            calls.append(1)
            return {"ok": 1, "total": 1}

        first = Collector("demo", 3600, run)
        first.execute()
        second = Collector("demo", 3600, run)
        self.assertFalse(second.due(first.last_run + 1))
        self.assertEqual(1, len(calls))
        self.assertEqual("success", db.get_collector_state("demo")["status"])

    def test_fresh_skip_is_visible(self):
        db.init_db()
        collector = Collector("fresh-demo", 60, lambda: {"ok": 1, "total": 1}, lambda: True)
        self.assertFalse(collector.due(10_000))
        self.assertEqual("fresh", db.get_collector_state("fresh-demo")["status"])

    def test_fresh_skip_preserves_previous_success_counts(self):
        db.init_db()
        db.set_collector_state(
            "fresh-counts", status="success", ok=24, total=24, success=True
        )
        collector = Collector(
            "fresh-counts", 0, lambda: {"ok": 1, "total": 1}, lambda: True
        )
        self.assertFalse(collector.due(10_000_000_000))
        state = db.get_collector_state("fresh-counts")
        self.assertEqual((24, 24), (state["ok"], state["total"]))

    def test_reconciliation_repairs_missing_data_even_when_cadence_is_not_due(self):
        import time

        db.init_db()
        repaired = []
        db.set_collector_state(
            "repair-demo", status="success", ok=1, total=1, success=True
        )
        fresh = [False]

        def repair():
            fresh[0] = True
            repaired.append(1)
            return {"ok": 1, "total": 1}

        collector = Collector(
            "repair-demo",
            86_400,
            lambda: {"ok": 1, "total": 1},
            is_fresh=lambda: fresh[0],
            repair=repair,
        )
        scheduler = Scheduler(repair_backoff=0, error_backoff=0)
        scheduler.register(collector)

        self.assertFalse(collector.due(time.time()))
        report = scheduler.reconcile()

        self.assertEqual([1], repaired)
        self.assertEqual("repaired", report[0]["action"])
        self.assertIn('"trigger": "reconcile"', db.get_collector_state("repair-demo")["details"])
        self.assertEqual("ok", db.get_reconciliation_state()["status"])

        stuck = Scheduler(repair_backoff=0, error_backoff=0)
        stuck.register(Collector(
            "stuck-demo", 86_400, lambda: {"ok": 1, "total": 1},
            is_fresh=lambda: False,
            repair=lambda: {"ok": 1, "total": 1},
        ))
        stuck_report = stuck.reconcile()
        self.assertEqual("partial", stuck_report[0]["status"])
        self.assertEqual("pending", db.get_reconciliation_state()["status"])

    def test_reconciliation_backs_off_after_provider_error(self):
        db.init_db()
        calls = []
        db.set_collector_state("broken-demo", status="error", error="provider down")
        scheduler = Scheduler(repair_backoff=0, error_backoff=3600)
        scheduler.register(Collector(
            "broken-demo",
            86_400,
            lambda: {"ok": 0, "total": 1, "errors": {"x": "failed"}},
            is_fresh=lambda: False,
            repair=lambda: calls.append(1) or {"ok": 1, "total": 1},
        ))

        report = scheduler.reconcile()

        self.assertEqual([], calls)
        self.assertEqual("backoff", report[0]["action"])
        self.assertEqual("provider_error", report[0]["reason"])
        self.assertGreater(report[0]["retry_in_seconds"], 0)
        self.assertEqual("pending", db.get_reconciliation_state()["status"])

    def test_cadence_backoff_is_not_an_unresolved_reconciliation_action(self):
        db.init_db()
        db.set_collector_state(
            "cadence-demo", status="success", ok=1, total=1, success=True
        )
        scheduler = Scheduler(repair_backoff=3600, error_backoff=3600)
        scheduler.register(Collector(
            "cadence-demo", 86_400, lambda: {"ok": 1, "total": 1},
            is_fresh=lambda: False,
            repair=lambda: {"ok": 1, "total": 1},
        ))

        report = scheduler.reconcile()

        self.assertEqual("backoff", report[0]["action"])
        self.assertEqual("cadence", report[0]["reason"])
        # Data younger than one collection interval is not a fault. Counting
        # it as unresolved left the whole system reading as degraded in the
        # gaps between a five-minute collector's own runs.
        self.assertEqual("ok", db.get_reconciliation_state()["status"])

    def test_bounded_backlog_is_a_healthy_run_not_a_failure(self):
        db.init_db()
        collector = Collector(
            "bounded-demo", 0,
            lambda: {"ok": 2, "total": 2, "pending": 7, "errors": {}},
            is_fresh=lambda: False,
        )
        outcome = collector.execute(trigger="reconcile")
        state = db.get_collector_state("bounded-demo")

        self.assertEqual("backlog", outcome["status"])
        self.assertEqual(7, outcome["pending"])
        self.assertIsNone(state["error"])
        self.assertIn('"pending": 7', state["details"])
        # The remaining queue is disclosed, but the run counts as a success:
        # otherwise the recovery delay applies the provider-error backoff and
        # a queue drains slower for no reason other than being non-empty.
        self.assertIsNotNone(state["last_success_at"])
        self.assertIn("backlog", db.HEALTHY_RUN_STATUSES)

    def test_backlog_does_not_take_the_provider_error_backoff(self):
        db.init_db()
        scheduler = Scheduler(repair_backoff=0, error_backoff=3600)
        collector = Collector(
            "backlog-demo", 86_400,
            lambda: {"ok": 1, "total": 1, "pending": 5, "errors": {}},
            is_fresh=lambda: False,
        )
        scheduler.register(collector)

        first = scheduler.reconcile()
        self.assertEqual("repaired", first[0]["action"])
        self.assertEqual("backlog", first[0]["status"])

        # A second sweep still finds work; with no error recorded it must be
        # allowed to run again rather than wait out the error backoff.
        second = scheduler.reconcile()
        self.assertEqual("repaired", second[0]["action"])
        self.assertEqual("ok", db.get_reconciliation_state()["status"])

    def test_quote_repair_fetches_only_missing_symbols(self):
        db.init_db()
        items = [
            {"symbol": "FRESH", "label": "정상", "group": "테스트"},
            {"symbol": "MISSING", "label": "누락", "group": "테스트"},
        ]
        db.save_quote({
            "symbol": "FRESH", "label": "정상", "group_name": "테스트",
            "price": 1.0, "prev_close": 1.0, "currency": "KRW",
            "updated": db.utc_now(),
        })
        with patch("app.registry.watchlist.watchlist", return_value=items), \
             patch("app.registry.quotes.quote", return_value={
                 "symbol": "MISSING", "price": 2.0,
                 "prev_close": 1.0, "currency": "KRW",
             }) as fetch:
            result = registry._repair_quotes()

        self.assertEqual((1, 1), (result["ok"], result["total"]))
        fetch.assert_called_once_with("MISSING")

        with patch("app.registry._indicator_deficits", side_effect=[
            ["us_cpi"], ["us_cpi"],
        ]), patch(
            "app.registry.indicators.fetch_keys_into_db",
            return_value={"us_cpi": 1},
        ) as indicator_fetch:
            indicator_result = registry._repair_indicators({"M"})
        indicator_fetch.assert_called_once_with(["us_cpi"])
        self.assertIn("us_cpi", indicator_result["errors"])

        scheduler = registry.build_scheduler()
        self.assertTrue(all(job.is_fresh and job.repair for job in scheduler.collectors))

    def test_event_freshness_checks_rows_not_only_version_hash(self):
        db.init_db()
        registry._run_events()
        self.assertTrue(registry._events_fresh())
        with db.get_conn() as conn:
            conn.execute("UPDATE events SET date='2099-01-01' WHERE id=(SELECT MIN(id) FROM events)")
        self.assertFalse(registry._events_fresh())

    def test_krx_batch_is_idempotent_and_skips_completed_provider_table(self):
        db.init_db()
        spec = next(item for item in krx.DATASETS if item["dataset"] == "stk_bydd_trd")
        payload = [{
            "BAS_DD": "20260821", "ISU_CD": "005930", "ISU_NM": "삼성전자",
            "MKT_NM": "KOSPI", "TDD_CLSPRC": "80000",
        }]
        with patch("app.registry.krx.dataset_specs", return_value=[spec]), \
             patch("app.registry.krx.catchup_dates", return_value=["2026-08-21"]), \
             patch("app.registry.krx.fetch_dataset", return_value=payload) as fetch:
            first = registry._run_krx_market()
            second = registry._run_krx_market()
        self.assertEqual((1, 1), (first["ok"], first["total"]))
        self.assertEqual((1, 1), (second["ok"], second["total"]))
        fetch.assert_called_once()
        self.assertEqual(1, db.market_overview("krx")["daily_rows"])


class HistoricalRecoveryTests(TemporaryDatabaseTest):
    def _indicator_patches(self, fetch):
        catalog = {
            "x": {
                "label": "테스트", "unit": "idx", "category": "물가",
                "source": "fred", "series": "TEST", "future": [],
            }
        }
        return (
            patch("app.history_recovery.indicators.catalog", return_value=catalog),
            patch("app.history_recovery.indicators.cycle_of", return_value="M"),
            patch("app.history_recovery.indicators.fetch_indicator", side_effect=fetch),
            patch("app.history_recovery.indices.index_list", return_value=[]),
            patch("app.history_recovery.krx.enabled", return_value=False),
            patch.object(history_recovery, "INDICATOR_CALL_BUDGET", 1),
            patch.object(history_recovery, "INDEX_CALL_BUDGET", 0),
            patch.object(history_recovery, "KRX_CALL_BUDGET", 0),
        )

    def test_provider_manifest_completes_once_and_repairs_local_historical_gap(self):
        db.init_db()
        series = [
            {"date": "2026-01-01", "value": 1.0},
            {"date": "2026-02-01", "value": 2.0},
        ]
        db.save_indicator_points("x", series, "fred")
        calls = []

        def fetch(_key):
            calls.append(1)
            return {"source": "fred", "series": series}

        patches = self._indicator_patches(fetch)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patches[5], patches[6], patches[7]:
            first = history_recovery.run()
            second = history_recovery.run()
            with db.get_conn() as conn:
                conn.execute(
                    "DELETE FROM indicator_points WHERE indicator='x' AND date='2026-01-01'"
                )
            audit = history_recovery.audit_targets()
            third = history_recovery.run()

        self.assertEqual((1, 0, 1), (first["total"], second["total"], third["total"]))
        self.assertEqual(2, len(calls))
        self.assertEqual(2, len(db.get_indicator_points("x")))
        self.assertEqual("pending", audit["status"])
        row = db.get_recovery_target(
            "historical", "indicator_history", "x", "provider_snapshot"
        )
        self.assertEqual("complete", row["status"])
        self.assertEqual(
            ["2026-01-01", "2026-02-01"], row["manifest"]
        )

    def test_provider_errors_stop_at_finite_exhausted_state(self):
        db.init_db()
        patches = self._indicator_patches(RuntimeError("provider down"))
        with patches[0], patches[1], patches[2] as fetch, patches[3], patches[4], \
             patches[5], patches[6], patches[7], \
             patch.object(history_recovery, "MAX_ATTEMPTS", 2), \
             patch.object(history_recovery, "RETRY_BACKOFF", 0), \
             patch.object(history_recovery, "MAX_BACKOFF", 0):
            history_recovery.run()
            history_recovery.run()
            third = history_recovery.run()

        self.assertEqual(2, fetch.call_count)
        self.assertEqual(0, third["total"])
        row = db.get_recovery_target(
            "historical", "indicator_history", "x", "provider_snapshot"
        )
        self.assertEqual("exhausted", row["status"])
        self.assertEqual(2, row["attempts"])

    def test_krx_verified_empty_day_is_terminal_and_not_retried(self):
        db.init_db()
        spec = next(item for item in krx.DATASETS if item["dataset"] == "stk_bydd_trd")
        with patch("app.history_recovery.indicators.catalog", return_value={}), \
             patch("app.history_recovery.indices.index_list", return_value=[]), \
             patch("app.history_recovery.krx.enabled", return_value=True), \
             patch("app.history_recovery.krx.dataset_specs", return_value=[spec]), \
             patch("app.history_recovery.krx.catchup_dates", return_value=["2026-08-17"]), \
             patch("app.history_recovery.krx.fetch_dataset", return_value=[]) as fetch, \
             patch.object(history_recovery, "INDICATOR_CALL_BUDGET", 0), \
             patch.object(history_recovery, "INDEX_CALL_BUDGET", 0), \
             patch.object(history_recovery, "KRX_CALL_BUDGET", 1):
            history_recovery.run()
            second = history_recovery.run()

        self.assertEqual(1, fetch.call_count)
        self.assertEqual(0, second["total"])
        self.assertEqual(
            "empty", db.market_run_status("krx", "stk_bydd_trd", "2026-08-17")
        )
        row = db.get_recovery_target(
            "historical", "krx_history", "stk_bydd_trd", "2026-08-17"
        )
        self.assertEqual("verified_empty", row["status"])

    def test_recovered_krx_day_stores_the_series_the_table_implies(self):
        """A stored table without its derived series is not a recovered day.

        Only first-line collection used to derive these, so every day the
        second-line layer recovered left the ledger calling the day complete
        while the gauge that needed it stayed empty.
        """
        db.init_db()
        # Depth 1 so the generation is exactly the day under test; this
        # dataset ships a 250-day backfill window in production.
        spec = {
            **next(
                item for item in krx.DATASETS if item["dataset"] == "drvprod_dd_trd"
            ),
            "history_days": 1,
        }
        day = "2026-08-17"
        raw = [{
            "BAS_DD": "20260817", "IDX_CLSS": "변동성",
            "IDX_NM": "코스피 200 변동성지수", "CLSPRC_IDX": "56.29",
        }]
        with patch("app.history_recovery.indicators.catalog", return_value={}), \
             patch("app.history_recovery.indices.index_list", return_value=[]), \
             patch("app.history_recovery.krx.enabled", return_value=True), \
             patch("app.history_recovery.krx.dataset_specs", return_value=[spec]), \
             patch("app.history_recovery.krx.catchup_dates", return_value=[day]), \
             patch("app.history_recovery.krx.fetch_dataset", return_value=raw), \
             patch.object(history_recovery, "INDICATOR_CALL_BUDGET", 0), \
             patch.object(history_recovery, "INDEX_CALL_BUDGET", 0), \
             patch.object(history_recovery, "KRX_CALL_BUDGET", 1):
            history_recovery.run()

        self.assertEqual(
            [{"date": day, "value": 56.29}], db.get_indicator_points("kr_vkospi")
        )

    def test_audit_rebuilds_a_missing_aggregate_from_rows_already_held(self):
        """The repair needs no provider: market_daily keeps the raw payload."""
        db.init_db()
        spec = next(
            item for item in krx.DATASETS if item["dataset"] == "drvprod_dd_trd"
        )
        day = "2026-08-17"
        db.save_market_batch("krx", "drvprod_dd_trd", day, krx.normalize_rows(
            spec,
            [{
                "BAS_DD": "20260817", "IDX_CLSS": "변동성",
                "IDX_NM": "코스피 200 변동성지수", "CLSPRC_IDX": "83.43",
            }],
            day,
        ))
        self.assertEqual([], db.get_indicator_points("kr_vkospi"))

        with patch("app.history_recovery.krx.dataset_specs", return_value=[spec]), \
             patch("app.history_recovery.krx.fetch_dataset") as fetch:
            repaired = history_recovery.rebuild_krx_aggregates()

        fetch.assert_not_called()
        self.assertEqual({"drvprod_dd_trd": 1}, repaired)
        self.assertEqual(
            [{"date": day, "value": 83.43}], db.get_indicator_points("kr_vkospi")
        )

    def test_deeper_history_extends_backwards_only_and_is_idempotent(self):
        """Raising a dataset's depth must not let the queue grow forward.

        The generation is anchored at its own newest day. Anchoring on today
        instead would add a session every day and never settle.
        """
        db.init_db()
        base = dict(
            next(item for item in krx.DATASETS if item["dataset"] == "stk_bydd_trd")
        )
        days = ["2026-08-17", "2026-08-18", "2026-08-19"]
        scopes = lambda: {  # noqa: E731
            row["scope"] for row in db.list_recovery_targets(
                layer="historical", kind="krx_history", manifest=False
            )
        }
        with patch("app.history_recovery.krx.enabled", return_value=True), \
             patch("app.history_recovery.krx.catchup_dates", return_value=days), \
             patch("app.history_recovery.krx.dataset_specs", return_value=[base]):
            history_recovery.ensure_targets()
            shallow = scopes()
            history_recovery.ensure_targets()
            self.assertEqual(shallow, scopes())  # repeat adds nothing

        deep = {**base, "history_days": 6}
        with patch("app.history_recovery.krx.enabled", return_value=True), \
             patch("app.history_recovery.krx.catchup_dates", return_value=days), \
             patch("app.history_recovery.krx.dataset_specs", return_value=[deep]):
            history_recovery.ensure_targets()
            extended = scopes()
            history_recovery.ensure_targets()
            self.assertEqual(extended, scopes())

        self.assertEqual(set(days), shallow)
        self.assertTrue(shallow < extended)
        self.assertEqual(max(days), max(extended))   # never forward
        self.assertLess(min(extended), min(days))    # only backward

    def test_history_depth_is_a_budget_not_part_of_target_identity(self):
        """Changing depth must not re-arm every KRX target.

        The fingerprint is what re-arms a target. A depth change that touched
        it would repeat the 843-target re-arm that left the system degraded
        for three days.
        """
        shallow = next(
            item for item in krx.DATASETS if item["dataset"] == "drvprod_dd_trd"
        )
        deep = {**shallow, "history_days": 750}
        source_spec = lambda spec: {  # noqa: E731
            "source": "krx", "dataset": spec["dataset"], "path": spec["path"],
        }
        self.assertEqual(
            history_recovery._target_fingerprint(
                "krx_history", shallow["dataset"], source_spec(shallow)
            ),
            history_recovery._target_fingerprint(
                "krx_history", deep["dataset"], source_spec(deep)
            ),
        )

    def test_krx_401_stops_remaining_dates_in_the_same_history_batch(self):
        db.init_db()
        spec = next(item for item in krx.DATASETS if item["dataset"] == "stk_bydd_trd")
        days = ["2026-08-17", "2026-08-18", "2026-08-19"]
        with patch("app.history_recovery.indicators.catalog", return_value={}), \
             patch("app.history_recovery.indices.index_list", return_value=[]), \
             patch("app.history_recovery.krx.enabled", return_value=True), \
             patch("app.history_recovery.krx.dataset_specs", return_value=[spec]), \
             patch("app.history_recovery.krx.catchup_dates", return_value=days), \
             patch("app.history_recovery.krx.fetch_dataset", side_effect=RuntimeError(
                 "KRX 인증 실패 또는 서비스 미승인 (HTTP 401)"
             )) as fetch, \
             patch.object(history_recovery, "INDICATOR_CALL_BUDGET", 0), \
             patch.object(history_recovery, "INDEX_CALL_BUDGET", 0), \
             patch.object(history_recovery, "KRX_CALL_BUDGET", 3):
            history_recovery.run()

        self.assertEqual(1, fetch.call_count)
        rows = db.list_recovery_targets(
            layer="historical", kind="krx_history", manifest=False
        )
        self.assertEqual({"blocked"}, {row["status"] for row in rows})

    def test_krx_401_blocks_both_layers_until_explicit_reset(self):
        db.init_db()
        spec = next(item for item in krx.DATASETS if item["dataset"] == "stk_bydd_trd")
        with patch("app.registry.krx.enabled", return_value=True), \
             patch("app.registry.krx.dataset_specs", return_value=[spec]), \
             patch("app.registry.krx.catchup_dates", return_value=["2026-08-17"]), \
             patch("app.registry.krx.fetch_dataset", side_effect=RuntimeError(
                 "KRX 인증 실패 또는 서비스 미승인 (HTTP 401)"
             )) as fetch, \
             patch("app.history_recovery.krx.enabled", return_value=True), \
             patch("app.history_recovery.krx.dataset_specs", return_value=[spec]), \
             patch("app.history_recovery.krx.catchup_dates", return_value=["2026-08-17"]):
            registry._run_krx_market()
            registry._run_krx_market()

        self.assertEqual(1, fetch.call_count)
        gate = db.get_recovery_target(
            "historical", "krx_access", "stk_bydd_trd", "authorization"
        )
        self.assertEqual("blocked", gate["status"])
        self.assertEqual(
            2,
            history_recovery.reset(target="stk_bydd_trd"),
        )
        self.assertEqual(
            "pending",
            db.get_recovery_target(
                "historical", "krx_access", "stk_bydd_trd", "authorization"
            )["status"],
        )


    def test_collector_fed_series_are_never_enrolled_for_provider_recovery(self):
        db.init_db()
        catalog = {
            "x": {
                "label": "테스트", "unit": "idx", "category": "물가",
                "source": "fred", "series": "TEST", "future": [],
            },
            "kr_vkospi": {
                "label": "VKOSPI", "unit": "idx", "category": "심리",
                "source": "krx", "series": "drvprod_dd_trd/변동성지수",
                "future": [],
            },
        }
        with patch("app.history_recovery.indicators.catalog", return_value=catalog), \
             patch("app.history_recovery.indicators.cycle_of", return_value="D"), \
             patch("app.history_recovery.indices.index_list", return_value=[]), \
             patch("app.history_recovery.krx.enabled", return_value=False):
            # A row enrolled before the exclusion existed has no provider call
            # behind it, so it must be dropped rather than re-armed.
            db.ensure_recovery_target(
                layer="historical", kind="indicator_history",
                target="kr_vkospi", scope="provider_snapshot",
                fingerprint="enrolled-by-mistake",
            )
            history_recovery.ensure_targets()
            settled = history_recovery.is_settled()

        targets = {
            row["target"] for row in db.list_recovery_targets(
                layer="historical", kind="indicator_history", manifest=False
            )
        }
        self.assertEqual({"x"}, targets)
        self.assertFalse(settled)  # "x" is still pending; kr_vkospi is simply gone

    def test_first_line_market_run_settles_the_krx_authorization_gate(self):
        db.init_db()
        spec = next(item for item in krx.DATASETS if item["dataset"] == "stk_bydd_trd")
        day = "2026-08-17"
        # The first-line collector already reached this dataset, which is what
        # authorization means. Nothing else ever completes the access row, so
        # without adopting that proof the layer can never report settled.
        db.save_market_batch("krx", "stk_bydd_trd", day, [])
        with patch("app.history_recovery.indicators.catalog", return_value={}), \
             patch("app.history_recovery.indices.index_list", return_value=[]), \
             patch("app.history_recovery.krx.enabled", return_value=True), \
             patch("app.history_recovery.krx.dataset_specs", return_value=[spec]), \
             patch("app.history_recovery.krx.catchup_dates", return_value=[day]), \
             patch("app.history_recovery.krx.fetch_dataset") as fetch:
            settled = history_recovery.is_settled()

        fetch.assert_not_called()
        gate = db.get_recovery_target(
            "historical", "krx_access", "stk_bydd_trd", "authorization"
        )
        self.assertEqual("complete", gate["status"])
        self.assertTrue(settled)

    def test_run_row_budget_defers_without_spending_a_finite_attempt(self):
        db.init_db()
        spec = next(item for item in krx.DATASETS if item["dataset"] == "stk_bydd_trd")
        day = "2026-08-17"
        with patch("app.history_recovery.indicators.catalog", return_value={}), \
             patch("app.history_recovery.indices.index_list", return_value=[]), \
             patch("app.history_recovery.krx.enabled", return_value=True), \
             patch("app.history_recovery.krx.dataset_specs", return_value=[spec]), \
             patch("app.history_recovery.krx.catchup_dates", return_value=[day]), \
             patch("app.history_recovery.krx.fetch_dataset", return_value=[{}, {}]), \
             patch.object(history_recovery, "INDICATOR_CALL_BUDGET", 0), \
             patch.object(history_recovery, "INDEX_CALL_BUDGET", 0), \
             patch.object(history_recovery, "KRX_CALL_BUDGET", 1), \
             patch.object(history_recovery, "KRX_ROW_BUDGET", 1), \
             patch.object(history_recovery, "MAX_ATTEMPTS", 1):
            first = history_recovery.run()
            history_recovery.run()

        row = db.get_recovery_target(
            "historical", "krx_history", "stk_bydd_trd", day
        )
        # MAX_ATTEMPTS is 1 here: had the guard charged an attempt, the first
        # sweep alone would have retired a day the provider never refused.
        self.assertEqual("pending", row["status"])
        self.assertEqual(0, row["attempts"])
        # The deferral is backlog, not failure, so the run stays healthy.
        self.assertEqual({}, first["errors"])
        self.assertEqual((0, 0), (first["ok"], first["total"]))
        self.assertGreater(first["pending"], 0)

    def test_blocked_authorization_is_not_overwritten_by_an_older_run(self):
        db.init_db()
        spec = next(item for item in krx.DATASETS if item["dataset"] == "stk_bydd_trd")
        day = "2026-08-17"
        db.save_market_batch("krx", "stk_bydd_trd", day, [])
        with patch("app.history_recovery.indicators.catalog", return_value={}), \
             patch("app.history_recovery.indices.index_list", return_value=[]), \
             patch("app.history_recovery.krx.enabled", return_value=True), \
             patch("app.history_recovery.krx.dataset_specs", return_value=[spec]), \
             patch("app.history_recovery.krx.catchup_dates", return_value=[day]):
            history_recovery.ensure_targets()
            db.update_recovery_target(
                "historical", "krx_access", "stk_bydd_trd", "authorization",
                status="blocked", reason="provider_access_blocked",
            )
            history_recovery.ensure_targets()

        gate = db.get_recovery_target(
            "historical", "krx_access", "stk_bydd_trd", "authorization"
        )
        self.assertEqual("blocked", gate["status"])


class PublicationLagTests(TemporaryDatabaseTest):
    def test_weekly_published_fx_is_not_a_daily_deficit(self):
        """FRED H.10 publishes daily FX once a week, on Monday, through the
        prior Friday. A healthy series is therefore routinely 8-10 days old,
        and the 5-day daily allowance made every FX series a standing deficit
        from midweek until the next release — a repair loop refetching the
        same values every six hours and never clearing."""
        db.init_db()
        for key in ("eur_usd", "us_krw", "usd_jpy", "us_dollar_index"):
            self.assertEqual(14, indicators.freshness_days(key))
        db.save_indicator_points(
            "us_krw", [{"date": "2026-08-21", "value": 1300.0}], "fred"
        )
        with patch("app.registry.kst_today", return_value=date(2026, 8, 29)):
            deficits = registry._indicator_deficits({"D", "W"})
        self.assertNotIn("us_krw", deficits)

    def test_allowance_still_discloses_a_genuinely_stalled_provider(self):
        db.init_db()
        db.save_indicator_points(
            "us_krw", [{"date": "2026-07-01", "value": 1300.0}], "fred"
        )
        with patch("app.registry.kst_today", return_value=date(2026, 8, 29)):
            deficits = registry._indicator_deficits({"D", "W"})
        self.assertIn("us_krw", deficits)


class ApiTests(TemporaryDatabaseTest):
    def setUp(self):
        super().setUp()
        db.init_db()
        from app import main
        self.main = main
        self.main._scheduler = None

    def test_series_endpoints_read_limited_cache_without_live_provider(self):
        db.save_index_points("^KS11", [{"date": "2026-01-01", "value": 3000}])
        db.save_indicator_points("us_cpi", [
            {"date": "2026-01-01", "value": 320.0},
            {"date": "2026-02-01", "value": 321.0},
        ], "fred")
        with patch("app.collectors.indices.quote", side_effect=AssertionError("live call")):
            index = self.main.api_index("^KS11")
            indicator = self.main.api_indicator("us_cpi", limit=1)
        self.assertEqual(3000, index["points"][-1]["value"])
        self.assertEqual([{"date": "2026-02-01", "value": 321.0}], indicator["points"])

    def test_unknown_quote_is_http_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as caught:
            self.main.api_quote("NOT-ALLOWED")
        self.assertEqual(404, caught.exception.status_code)

    def test_http_events_days_uses_kst_and_validates_ranges(self):
        from fastapi.testclient import TestClient

        with patch.dict(os.environ, {"MONEY_DISABLE_SCHEDULER": "1"}):
            with TestClient(self.main.app) as client:
                response = client.get("/api/events?days=1")
                invalid = client.get("/api/events?start=2026-02-02&end=2026-02-01")
                scheduler = client.get("/api/scheduler")
        self.assertEqual(200, response.status_code)
        self.assertEqual(kst_today().isoformat(), response.json()["start"])
        self.assertEqual(422, invalid.status_code)
        self.assertIn("reconciliation", scheduler.json())

    def test_local_chart_bundle_is_served(self):
        from fastapi.testclient import TestClient

        with patch.dict(os.environ, {"MONEY_DISABLE_SCHEDULER": "1"}):
            with TestClient(self.main.app) as client:
                response = client.get("/static/echarts.min.js")
        self.assertEqual(200, response.status_code)
        self.assertGreater(len(response.content), 500_000)

    def test_analysis_page_templates_render(self):
        from fastapi.testclient import TestClient

        with patch.dict(os.environ, {"MONEY_DISABLE_SCHEDULER": "1"}):
            with TestClient(self.main.app) as client:
                response = client.get("/spillover")
                correlation_page = client.get("/correlation")
                manage = client.get("/manage")
        self.assertEqual(200, response.status_code)
        self.assertIn("edgeDensity", response.text)
        self.assertIn("focus:'adjacency'", response.text)
        self.assertEqual(200, correlation_page.status_code)
        self.assertIn("dimension:2", correlation_page.text)
        self.assertEqual(200, manage.status_code)
        self.assertIn("결측 보상 감사", manage.text)
        self.assertIn("핵심 분석 준비", manage.text)

    def test_market_universe_endpoints_read_cache_without_provider_call(self):
        from fastapi.testclient import TestClient

        db.save_market_batch("krx", "kospi_dd_trd", "2026-08-21", [{
            "symbol": "코스피", "name": "코스피", "asset_type": "index",
            "market": "KOSPI", "currency": "KRW", "date": "2026-08-21",
            "close": 3000.0, "change": 10.0, "change_pct": 0.3,
            "open": 2990.0, "high": 3010.0, "low": 2980.0,
            "volume": 1.0, "turnover": 2.0, "market_cap": 3.0,
            "metadata": {}, "raw": {"IDX_NM": "코스피"},
        }])
        with patch("app.collectors.krx.fetch_dataset", side_effect=AssertionError("live call")), \
             patch.dict(os.environ, {"MONEY_DISABLE_SCHEDULER": "1"}):
            with TestClient(self.main.app) as client:
                universe = client.get("/api/market/universe?source=krx&q=코스피")
                daily = client.get("/api/market/daily?source=krx&symbol=코스피")
        self.assertEqual(1, universe.json()["count"])
        self.assertEqual(3000.0, daily.json()["rows"][0]["close"])

    def test_derived_endpoints_are_cache_only_and_stocks_page_exposes_them(self):
        from fastapi.testclient import TestClient

        db.save_indicator_points("us_nfp", [
            {"date": "2026-06-01", "value": 100},
            {"date": "2026-07-01", "value": 110},
        ], "fred")
        with patch(
            "app.collectors.indicators.fetch_indicator",
            side_effect=AssertionError("live call"),
        ), patch.dict(os.environ, {"MONEY_DISABLE_SCHEDULER": "1"}):
            with TestClient(self.main.app) as client:
                derived = client.get("/api/analysis/derived")
                breadth = client.get("/api/analysis/krx-breadth")
                page = client.get("/stocks")
        self.assertEqual(200, derived.status_code)
        self.assertEqual(10, derived.json()["macro"]["us_nfp_monthly_change"]["change_thousand"])
        self.assertEqual("unavailable", breadth.json()["markets"][0]["status"])
        self.assertIn("캐시 기반 시장 진단", page.text)
        self.assertIn("/api/analysis/derived", page.text)


class McpTests(TemporaryDatabaseTest):
    def test_server_registers_cache_only_tools(self):
        import anyio
        from app import mcp_server

        db.init_db()
        names = {tool.name for tool in anyio.run(mcp_server.mcp.list_tools)}
        self.assertEqual({
            "market_health", "market_situation", "market_coverage",
            "market_events", "market_quotes",
            "market_indices", "market_indicator_list", "market_indicator",
            "market_universe", "market_correlation", "market_spillover",
            "market_yield_curve", "market_index_analysis", "market_technical",
            "market_risk", "market_regime", "market_sentiment",
            "market_derived_metrics", "market_breadth",
            "market_datasets", "market_daily",
        }, names)
        self.assertEqual("ok", mcp_server.market_health()["database_integrity"])
        self.assertIn("reconciliation", mcp_server.market_health())

        async def call_health():
            return await mcp_server.mcp.call_tool("market_health", {})

        result = anyio.run(call_health)
        self.assertFalse(result.is_error)

    def test_discovery_and_analysis_tools_expose_cache_context(self):
        from app import mcp_server

        db.init_db()
        db.save_indicator_points(
            "us_cpi", [{"date": "2026-07-01", "value": 323.1}], "fred"
        )
        db.save_index_points("^GSPC", points(
            [5000 + index + math.sin(index / 4) for index in range(260)],
            start=date(2025, 10, 1),
        ))
        db.save_market_batch("krx", "stk_bydd_trd", "2026-08-21", [{
            "symbol": "005930", "name": "삼성전자", "asset_type": "stock",
            "market": "KOSPI", "currency": "KRW", "date": "2026-08-21",
            "close": 80000.0, "change": 1000.0, "change_pct": 1.27,
            "open": 79000.0, "high": 81000.0, "low": 78500.0,
            "volume": 12345.0, "turnover": 999999.0, "market_cap": 1000000.0,
            "metadata": {}, "raw": {},
        }])

        indicator = next(
            item for item in mcp_server.market_indicator_list()["items"]
            if item["key"] == "us_cpi"
        )
        technical = mcp_server.market_technical("^GSPC")
        universe = mcp_server.market_universe(query="삼성")

        self.assertEqual("2026-07-01", indicator["latest_date"])
        self.assertEqual("core", indicator["priority"])
        self.assertIsNotNone(indicator["retrieved_at"])
        self.assertTrue(technical["cached"])
        self.assertEqual("yahoo", technical["source"])
        self.assertIsNotNone(technical["as_of"])
        self.assertEqual("005930", universe["instruments"][0]["symbol"])
        self.assertEqual(
            "years must be between 1 and 20",
            mcp_server.market_risk("^GSPC", years=0)["error"],
        )

    def test_partial_collector_is_visible_as_degraded_health(self):
        from app import mcp_server

        db.init_db()
        db.set_collector_state("krx_market", status="partial", ok=1, total=2)
        health = mcp_server.market_health()
        self.assertEqual("degraded", health["status"])
        self.assertEqual("partial", health["collector_issues"][0]["status"])
        self.assertIn("core_ready_pct", health["coverage"])


class ProjectSkillTests(unittest.TestCase):
    def test_project_skill_defines_cache_and_interpretation_contract(self):
        skill = (
            Path(__file__).resolve().parents[1]
            / ".agents/skills/money-market-intelligence/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\nname: money-market-intelligence\n"))
        self.assertIn("market_indicator_list", skill)
        self.assertIn("retrieval timestamp", skill)
        self.assertIn("not structural causality", skill)


if __name__ == "__main__":
    unittest.main()


class KrxAggregateIsolationTests(TemporaryDatabaseTest):
    """A derived summary must not invalidate the rows it was derived from."""

    def _run_with_broken_aggregate(self):
        from app import registry
        from app.collectors import krx as krx_module

        spec = next(
            item for item in krx_module.DATASETS
            if item["dataset"] == "opt_bydd_trd"
        )
        raw = [{
            "BAS_DD": "20260825", "ISU_CD": "B01",
            "ISU_NM": "코스피200 C (정규)", "PROD_NM": "코스피200 옵션",
            "RGHT_TP_NM": "CALL", "TDD_CLSPRC": "1.0",
            "ACC_TRDVOL": "10", "ACC_TRDVAL": "100", "ACC_OPNINT_QTY": "5",
        }]
        with patch.object(krx_module, "fetch_dataset", return_value=raw), \
             patch.object(krx_module, "catchup_dates", return_value=["2026-08-25"]), \
             patch.object(krx_module, "dataset_specs", return_value=[spec]), \
             patch.object(
                 registry.krx, "aggregate_put_call",
                 side_effect=RuntimeError("summariser broke"),
             ):
            return registry._run_krx_market()

    def test_a_failed_summary_leaves_the_day_recorded_as_collected(self):
        db.init_db()
        result = self._run_with_broken_aggregate()
        # Marking the day an error would make the collector re-fetch rows it
        # already holds and make the recovery ledger call it a gap.
        self.assertEqual(
            "success", db.market_run_status("krx", "opt_bydd_trd", "2026-08-25")
        )
        self.assertEqual(
            1, len(db.get_market_daily(source="krx", dataset="opt_bydd_trd"))
        )
        self.assertTrue(
            any(key.endswith("#aggregate") for key in result["errors"]),
            result["errors"],
        )

    def test_a_working_summary_reports_no_error(self):
        db.init_db()
        from app import registry
        from app.collectors import krx as krx_module

        spec = next(
            item for item in krx_module.DATASETS
            if item["dataset"] == "opt_bydd_trd"
        )
        raw = [
            {"BAS_DD": "20260825", "ISU_CD": "B01", "ISU_NM": "코스피200 C (정규)",
             "PROD_NM": "코스피200 옵션", "RGHT_TP_NM": "CALL", "TDD_CLSPRC": "1.0",
             "ACC_TRDVOL": "10", "ACC_TRDVAL": "100", "ACC_OPNINT_QTY": "5"},
            {"BAS_DD": "20260825", "ISU_CD": "B02", "ISU_NM": "코스피200 P (정규)",
             "PROD_NM": "코스피200 옵션", "RGHT_TP_NM": "PUT", "TDD_CLSPRC": "1.0",
             "ACC_TRDVOL": "20", "ACC_TRDVAL": "200", "ACC_OPNINT_QTY": "10"},
        ]
        with patch.object(krx_module, "fetch_dataset", return_value=raw), \
             patch.object(krx_module, "catchup_dates", return_value=["2026-08-25"]), \
             patch.object(krx_module, "dataset_specs", return_value=[spec]):
            result = registry._run_krx_market()
        self.assertEqual({}, result["errors"])
        self.assertEqual(
            2.0, db.get_indicator_points("kr_put_call_volume")[-1]["value"]
        )
