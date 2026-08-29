"""The backtest, and the leak surface it introduces.

`app.pit` protects the verdict side. Pairing a verdict at D with what happened
by D+N is new code with its own date arithmetic, and that is where look-ahead
walks back in — so most of this file is about the forward window rather than
about any statistic computed from it.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import backtest, db, pit


class TemporaryDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def index(self, symbol, values, start_day=1):
        db.replace_index_points(symbol, [
            {"date": f"2026-01-{start_day + offset:02d}", "value": float(value)}
            for offset, value in enumerate(values)
        ])


class ForwardWindowTests(TemporaryDatabaseTest):
    """The outcome window is strictly after D. This is the leak surface."""

    def test_the_window_excludes_the_day_it_is_measured_from(self):
        # Including D's own close would grade a verdict partly on the bar it
        # was made from — the outcome-side twin of look-ahead.
        self.index("^KS11", [100, 90, 95, 96, 97])
        out = backtest.forward("^KS11", horizon=2)
        self.assertEqual(90.0, out["2026-01-01"]["low"])
        self.assertEqual(-10.0, out["2026-01-01"]["drawdown_pct"])
        # From the 2nd, the 100 is behind and must not appear.
        self.assertEqual(95.0, out["2026-01-02"]["low"])

    def test_the_window_never_reaches_backwards(self):
        self.index("^KS11", [50, 100, 101, 102, 103])
        out = backtest.forward("^KS11", horizon=2)
        self.assertEqual(101.0, out["2026-01-02"]["low"])
        self.assertGreater(out["2026-01-02"]["drawdown_pct"], 0)

    def test_a_day_whose_horizon_has_not_finished_is_omitted(self):
        # A truncated window is not a smaller window; it is a day whose outcome
        # has not happened. Counting it would read the unfinished present as
        # evidence of calm.
        self.index("^KS11", [100, 99, 98, 97, 96])
        out = backtest.forward("^KS11", horizon=3)
        self.assertEqual(["2026-01-01", "2026-01-02"], sorted(out))

    def test_the_horizon_counts_sessions_not_calendar_days(self):
        db.replace_index_points("^KS11", [
            {"date": "2026-01-02", "value": 100.0},
            {"date": "2026-01-09", "value": 90.0},   # a week later
            {"date": "2026-01-12", "value": 95.0},
        ])
        out = backtest.forward("^KS11", horizon=2)
        self.assertEqual(90.0, out["2026-01-02"]["low"])


class ContingencyTests(TemporaryDatabaseTest):
    def rows(self, pairs):
        return [{"date": f"2026-01-{day:02d}", "korea_regime": verdict}
                for day, verdict in pairs]

    def outcomes(self, drawdowns):
        return {f"2026-01-{day:02d}": {"drawdown_pct": value, "return_pct": value}
                for day, value in drawdowns}

    def test_the_four_cells_are_counted_as_named(self):
        rows = self.rows([(1, "risk_off"), (2, "risk_off"),
                          (3, "neutral"), (4, "neutral")])
        outcomes = self.outcomes([(1, -10.0), (2, -1.0), (3, -10.0), (4, -1.0)])
        table = backtest.contingency(rows, outcomes, field="korea_regime")
        self.assertEqual(
            (1, 1, 1, 1),
            (table["hit"], table["false_alarm"], table["miss"],
             table["correct_rejection"]),
        )

    def test_precision_is_reported_with_the_base_rate_that_gives_it_meaning(self):
        # Warn every day: precision equals the base rate exactly, and lift is
        # zero. Precision alone would read as 50% skill.
        rows = self.rows([(day, "risk_off") for day in range(1, 5)])
        outcomes = self.outcomes([(1, -10.0), (2, -10.0), (3, -1.0), (4, -1.0)])
        table = backtest.contingency(rows, outcomes, field="korea_regime")
        self.assertEqual(50.0, table["precision"])
        self.assertEqual(50.0, table["base_rate"])
        self.assertEqual(0.0, table["lift"])

    def test_a_classifier_that_never_warns_reports_no_precision_not_zero(self):
        rows = self.rows([(day, "neutral") for day in range(1, 5)])
        outcomes = self.outcomes([(day, -10.0) for day in range(1, 5)])
        table = backtest.contingency(rows, outcomes, field="korea_regime")
        self.assertIsNone(table["precision"])
        self.assertEqual(4, table["miss"])
        self.assertEqual(0.0, table["recall"])

    def test_a_day_with_no_finished_outcome_is_dropped_from_every_cell(self):
        rows = self.rows([(1, "risk_off"), (2, "risk_off")])
        table = backtest.contingency(
            rows, self.outcomes([(1, -10.0)]), field="korea_regime")
        self.assertEqual(1, table["days"])

    def test_the_threshold_is_a_floor_not_a_range(self):
        rows = self.rows([(1, "risk_off"), (2, "risk_off")])
        outcomes = self.outcomes([(1, -7.0), (2, -6.999)])
        table = backtest.contingency(rows, outcomes, field="korea_regime",
                                     threshold=7.0)
        self.assertEqual((1, 1), (table["hit"], table["false_alarm"]))


class CalendarTests(TemporaryDatabaseTest):
    """Two markets, two calendars. Grading one against the other loses days."""

    def test_the_replay_calendar_is_the_union_of_both_benchmarks(self):
        db.replace_index_points("^KS11", [
            {"date": "2026-01-01", "value": 1.0},
            {"date": "2026-01-02", "value": 1.0},
        ])
        db.replace_index_points("^GSPC", [
            {"date": "2026-01-02", "value": 1.0},
            {"date": "2026-01-05", "value": 1.0},   # KRX holiday
        ])
        self.assertEqual(
            ["2026-01-01", "2026-01-02", "2026-01-05"],
            backtest.replay_calendar("2026-01-01", "2026-01-31"),
        )

    def test_a_day_the_benchmark_did_not_trade_is_counted_not_dropped(self):
        # This is how the US mismatch hid: `if row["date"] in outcomes` is a
        # silent filter, so 17 verdicts became 16 rows with nothing saying why.
        rows = [{"date": f"2026-01-{day:02d}", "korea_regime": "risk_off"}
                for day in (1, 2, 3)]
        outcomes = {"2026-01-01": {"drawdown_pct": -10.0, "return_pct": -10.0}}
        table = backtest.contingency(rows, outcomes, field="korea_regime")
        self.assertEqual((1, 2), (table["days"], table["ungraded"]))
        grouped = backtest.conditional(rows, outcomes, field="korea_regime")
        self.assertEqual(2, grouped["risk_off"]["ungraded"])


class StructureTests(unittest.TestCase):
    """Measuring what the classifier is, without changing it."""

    def rows(self, components):
        return [{"date": f"2026-01-{index + 1:02d}", "korea_regime": verdict,
                 "korea_components": __import__("json").dumps(votes)}
                for index, (verdict, votes) in enumerate(components)]

    def test_a_component_that_cannot_vote_zero_is_named_degenerate(self):
        rows = self.rows([
            ("neutral", {"trend": 1, "credit": 0}),
            ("risk_off", {"trend": -1, "credit": -1}),
        ])
        found = {item["key"]: item for item in backtest.structure(rows)["components"]}
        self.assertTrue(found["trend"]["degenerate"])
        self.assertFalse(found["credit"]["degenerate"])

    def test_a_degenerate_component_negative_on_every_warning_is_a_gate(self):
        # Not a vote: with the ratio rule the verdict cannot fire without it,
        # so a nominal five-input composite is really gated on one.
        rows = self.rows([
            ("neutral", {"trend": 1, "credit": -1}),
            ("risk_off", {"trend": -1, "credit": -1}),
            ("risk_off", {"trend": -1, "credit": 0}),
        ])
        self.assertEqual(["trend"], backtest.structure(rows)["mandatory_gates"])

    def test_a_component_negative_on_only_some_warnings_is_not_a_gate(self):
        rows = self.rows([
            ("risk_off", {"trend": -1}),
            ("risk_off", {"trend": 1}),
        ])
        self.assertEqual([], backtest.structure(rows)["mandatory_gates"])

    def test_the_negative_share_is_reported_per_year(self):
        rows = [{"date": "2025-01-01", "korea_regime": "neutral",
                 "korea_components": '{"volatility": 0}'},
                {"date": "2026-01-01", "korea_regime": "risk_off",
                 "korea_components": '{"volatility": -1}'}]
        share = backtest.structure(rows)["negative_share_by_year"]
        self.assertEqual({"2025": {"volatility": 0.0}, "2026": {"volatility": 100.0}},
                         share)


class PriceRuleTests(TemporaryDatabaseTest):
    """The surrogate must be point-in-time, or the out-of-window test lies."""

    def test_the_rule_uses_only_history_up_to_each_day(self):
        from app import analysis

        # A long flat run then a crash: before the crash the rule must not know
        # about it, which is the whole point of testing outside the window.
        span = max(analysis.KR_MIN_HISTORY["trend"],
                   analysis.KR_DRAWDOWN_WINDOW,
                   analysis.KR_MIN_HISTORY["drawdown"]) + 40
        values = [100.0] * span + [60.0] * 20
        from datetime import date, timedelta

        start = date(2020, 1, 1)
        db.replace_index_points("^KS11", [
            {"date": (start + timedelta(days=offset)).isoformat(), "value": value}
            for offset, value in enumerate(values)
        ])
        rows = backtest.price_rule(db.get_index_points("^KS11"))
        flat = [row for row in rows if row["date"] < (start + timedelta(days=span)).isoformat()]
        crashed = [row for row in rows if row["date"] >= (start + timedelta(days=span)).isoformat()]
        self.assertTrue(flat and crashed)
        self.assertFalse(any(row["warning"] for row in flat),
                         "the rule saw a crash that had not happened yet")
        self.assertTrue(any(row["warning"] for row in crashed))

    def test_the_out_of_window_split_reports_both_sides_against_a_baseline(self):
        from datetime import date, timedelta
        from app import analysis

        span = max(analysis.KR_MIN_HISTORY["trend"],
                   analysis.KR_DRAWDOWN_WINDOW) + 60
        start = date(2020, 1, 1)
        db.replace_index_points("^KS11", [
            {"date": (start + timedelta(days=offset)).isoformat(),
             "value": 100.0 - offset * 0.01}
            for offset in range(span + 40)
        ])
        report = backtest.out_of_window(horizon=5, boundary="2021-01-01")
        if not report["available"]:
            # No history before the boundary. That must be said outright, not
            # rendered as zeros — an empty `before` bucket reads as "no effect
            # out of sample" when it means "no out of sample".
            self.assertTrue(report["reason"].strip())
            return
        for side in ("before", "after"):
            # The baseline must always be present: a warning rate quoted
            # without the unconditional rate says nothing.
            self.assertIn("all_days_median_return", report[side])
            self.assertIn("all_days_positive_pct", report[side])

    def test_a_missing_holdout_is_reported_rather_than_rendered_as_zeros(self):
        from datetime import date, timedelta
        from app import analysis

        span = analysis.KR_DRAWDOWN_WINDOW + analysis.KR_MIN_HISTORY["trend"] + 60
        start = date(2020, 1, 1)
        db.replace_index_points("^KS11", [
            {"date": (start + timedelta(days=offset)).isoformat(),
             "value": 100.0 - offset * 0.01}
            for offset in range(span)
        ])
        report = backtest.out_of_window(horizon=5, boundary="2020-01-02")
        self.assertFalse(report["available"])
        self.assertIn("홀드아웃", report["reason"])


class StratifiedTests(unittest.TestCase):
    """Pooled lift credits being switched on during bad years. Stratified does not."""

    def rows(self, spec):
        return [{"date": day, "korea_regime": verdict} for day, verdict in spec]

    def test_pooling_across_periods_with_different_base_rates_manufactures_lift(self):
        # A classifier with zero within-year skill: inside each year it warns
        # on exactly the same share of stress days as the base rate. Pooled it
        # still shows lift, because it warns in the dangerous year and not in
        # the calm one. This is the artifact, reproduced in eight rows.
        spec, outcomes = [], {}
        for index in range(10):     # dangerous year: 80% stress, all warned
            day = f"2020-01-{index + 1:02d}"
            spec.append((day, "risk_off"))
            outcomes[day] = {"drawdown_pct": -10.0 if index < 8 else -1.0,
                             "return_pct": 0.0}
        for index in range(10):     # calm year: 10% stress, never warned
            day = f"2021-01-{index + 1:02d}"
            spec.append((day, "neutral"))
            outcomes[day] = {"drawdown_pct": -10.0 if index < 1 else -1.0,
                             "return_pct": 0.0}
        rows = self.rows(spec)
        pooled = backtest.contingency(rows, outcomes, field="korea_regime")
        report = backtest.stratified(rows, outcomes, field="korea_regime")
        self.assertGreater(pooled["lift"], 20)
        # Inside its own year the warning added nothing: it fired on every day,
        # so its precision equals that year's base rate exactly.
        self.assertEqual(0.0, report["year"]["weighted_lift"])

    def test_episodes_are_counted_as_runs_not_days(self):
        rows = self.rows([
            ("2020-01-01", "risk_off"), ("2020-01-02", "risk_off"),
            ("2020-01-03", "neutral"),
            ("2020-01-04", "risk_off"),
        ])
        runs = backtest.episodes(rows, field="korea_regime")
        self.assertEqual([2, 1], [len(run) for run in runs])

    def test_the_stratified_report_names_how_many_strata_favour_the_claim(self):
        rows = self.rows([("2020-01-01", "risk_off"), ("2021-01-01", "risk_off")])
        outcomes = {"2020-01-01": {"drawdown_pct": -10.0, "return_pct": 0.0},
                    "2021-01-01": {"drawdown_pct": -1.0, "return_pct": 0.0}}
        report = backtest.stratified(rows, outcomes, field="korea_regime")
        self.assertEqual(2, report["year"]["with_warnings"])
        self.assertEqual(0, report["year"]["positive"])


class TimingTests(unittest.TestCase):
    """A lead that cannot exceed the search window is not a lead."""

    def test_the_run_age_is_not_capped_by_the_horizon(self):
        # The old measure searched back only HORIZON_DAYS, so median == max ==
        # cap was the signature of censoring being read as central tendency.
        span = backtest.HORIZON_DAYS * 3
        rows = [{"date": f"2020-{1 + day // 28:02d}-{day % 28 + 1:02d}",
                 "korea_regime": "risk_off"} for day in range(span)]
        outcomes = {rows[-1]["date"]: {"drawdown_pct": -10.0, "return_pct": 0.0}}
        report = backtest.timing(rows, outcomes, field="korea_regime")
        self.assertEqual(span - 1, report["run_age"]["best"])
        self.assertGreater(report["run_age"]["best"], backtest.HORIZON_DAYS)

    def test_a_stress_day_with_no_warning_running_is_counted_unwarned(self):
        rows = [{"date": "2020-01-01", "korea_regime": "neutral"}]
        outcomes = {"2020-01-01": {"drawdown_pct": -10.0, "return_pct": 0.0}}
        report = backtest.timing(rows, outcomes, field="korea_regime")
        self.assertEqual((1, 0, 1), (report["stress_days"],
                                     report["warned_on_the_day"],
                                     report["unwarned"]))


class ChurnTests(unittest.TestCase):
    def test_run_lengths_are_reported_not_just_a_change_count(self):
        rows = [{"korea_regime": name} for name in
                ["neutral"] * 5 + ["risk_off"] * 2 + ["neutral"] * 10]
        report = backtest.churn(rows, field="korea_regime")
        self.assertEqual((2, 3, 2, 10), (
            report["changes"], report["runs"],
            report["shortest_run_days"], report["longest_run_days"]))

    def test_an_empty_series_does_not_report_a_change(self):
        report = backtest.churn([], field="korea_regime")
        self.assertEqual((0, 0), (report["changes"], report["runs"]))


class DeclaredConstantTests(unittest.TestCase):
    """The declarations are the contract; a silent edit is the failure."""

    def test_the_declared_pair_appears_in_the_reported_grid(self):
        self.assertIn(backtest.DRAWDOWN_PCT, backtest.GRID_PCT)
        self.assertIn(backtest.HORIZON_DAYS, backtest.GRID_DAYS)

    def test_the_window_starts_where_a_verdict_first_becomes_possible(self):
        # Re-derived rather than trusted: the backfill moved this boundary by
        # sixteen years, and a declared constant that no longer matches the
        # data is worse than none.
        from datetime import date, timedelta
        from app import analysis

        start = date.fromisoformat(backtest.WINDOW_START)
        after = pit.replay(backtest.WINDOW_START)["korea_regime"]
        self.assertNotEqual("unknown", after["regime"])
        self.assertGreaterEqual(after["component_count"],
                                analysis.KR_MIN_ACTIVE_COMPONENTS)
        before = pit.replay((start - timedelta(days=2)).isoformat())["korea_regime"]
        self.assertEqual("unknown", before["regime"])

    def test_the_full_window_starts_where_all_five_components_first_vote(self):
        from datetime import date, timedelta

        start = date.fromisoformat(backtest.FULL_WINDOW_START)
        after = pit.replay(backtest.FULL_WINDOW_START)["korea_regime"]
        self.assertEqual(after["component_total"], after["component_count"])
        before = pit.replay((start - timedelta(days=2)).isoformat())["korea_regime"]
        self.assertLess(before["component_count"], before["component_total"])
        self.assertLess(backtest.WINDOW_START, backtest.FULL_WINDOW_START)


if __name__ == "__main__":
    unittest.main()
