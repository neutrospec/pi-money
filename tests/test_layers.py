"""The five evidence layers, and the confidence they report about themselves.

The staleness path cannot be tested against the live cache — collectors are
healthy, so nothing is late, and a test that only passes when data is fresh
verifies nothing about the case it exists for. Every downgrade here is built
from synthetic readings.
"""
from __future__ import annotations

import unittest
from datetime import date

from app import analysis, layers
from app.collectors import indicators


def reading(label="계열", *, voted=True, vote=0, late=None, priority="supporting",
            risk_percentile=50.0, key="k"):
    if not voted:
        return {"key": key, "label": label, "voted": False, "reason": "이력 부족"}
    return {
        "key": key, "label": label, "voted": True, "vote": vote,
        "risk_percentile": risk_percentile, "priority": priority,
        "days_late": late, "stale": late is not None,
    }


class LayerMapTests(unittest.TestCase):
    def test_every_analysis_group_belongs_to_exactly_one_layer(self):
        # A group added to the catalogue and not mapped here would vanish from
        # the screen silently, which is the failure this project keeps finding.
        self.assertEqual([], layers.unmapped())
        seen = set()
        for key, groups in layers.GROUPS_BY_LAYER.items():
            overlap = seen & groups
            self.assertEqual(set(), overlap, f"{key} re-claims {overlap}")
            seen |= groups

    def test_the_policy_layer_does_not_vote_and_says_why(self):
        # Every policy-rate and inflation series declares its direction as
        # neutral on purpose. A stance card there would be a risk verdict
        # invented out of series that refused to give one.
        policy = next(item for item in layers.LAYERS if item["key"] == "policy")
        self.assertEqual("level", policy["mode"])
        self.assertTrue(policy["abstains"].strip())
        catalog = indicators.catalog()
        members = [
            key for key, spec in catalog.items()
            if spec["analysis_group"] in policy["groups"]
        ]
        self.assertTrue(members)
        self.assertEqual(
            {indicators.NEUTRAL},
            {indicators.risk_direction(key) for key in members},
            "a policy series gained a risk direction — the level card's "
            "premise no longer holds and the layer should be reconsidered",
        )

    def test_the_voting_layers_use_the_regime_classifier_cuts(self):
        self.assertEqual(1, layers._vote(analysis.KR_RISK_ON_PERCENTILE))
        self.assertEqual(-1, layers._vote(analysis.KR_RISK_OFF_PERCENTILE))
        self.assertEqual(0, layers._vote(50.0))
        self.assertIsNone(layers._vote(None))


class StalenessTests(unittest.TestCase):
    """Late is measured against the series' own allowance, not a flat number."""

    def test_a_series_within_its_own_allowance_is_not_late(self):
        self.assertIsNone(
            layers._staleness({"max_age_days": 40}, "2026-08-01", date(2026, 8, 29))
        )

    def test_a_daily_series_silent_for_two_weeks_is_late(self):
        self.assertEqual(
            7, layers._staleness({"max_age_days": 7}, "2026-08-15", date(2026, 8, 29))
        )

    def test_a_quarterly_series_is_not_late_at_an_age_that_breaks_a_daily_one(self):
        old, allowance = "2026-07-01", date(2026, 8, 29)
        self.assertIsNone(layers._staleness({"max_age_days": 120}, old, allowance))
        self.assertIsNotNone(layers._staleness({"max_age_days": 7}, old, allowance))

    def test_a_missing_observation_date_is_not_silently_called_fresh(self):
        self.assertIsNone(layers._staleness({"max_age_days": 7}, None, date(2026, 8, 29)))


class ConfidenceTests(unittest.TestCase):
    def test_full_fresh_reporting_is_high(self):
        report = layers.confidence([reading() for _ in range(10)], 10)
        self.assertEqual(("high", 1.0), (report["level"], report["strength"]))
        self.assertEqual([], report["reasons"])

    def test_evidence_that_never_arrived_lowers_confidence_and_is_named(self):
        evidence = [reading() for _ in range(3)] + [
            reading(voted=False) for _ in range(7)
        ]
        report = layers.confidence(evidence, 10)
        self.assertEqual(("low", 0.3, 3, 0), (
            report["level"], report["strength"], report["voted"], report["stale"]))
        self.assertIn("7개가", report["reasons"][0])

    def test_late_evidence_lowers_confidence_without_being_discarded(self):
        # Half weight, stated rather than tuned: a value past its allowance is
        # worth having and not worth as much as a current one. Zero weight
        # would throw away the only reading there is.
        late = layers.confidence(
            [reading(late=3) for _ in range(10)], 10
        )
        self.assertEqual((0.5, "medium", 10, 10), (
            late["strength"], late["level"], late["voted"], late["stale"]))
        self.assertIn("갱신 주기", late["reasons"][0])

    def test_missing_and_late_are_counted_apart(self):
        report = layers.confidence(
            [reading(late=2), reading(), reading(voted=False)], 3
        )
        self.assertEqual((1, 1, 2), (report["stale"], report["fresh"], report["voted"]))
        self.assertEqual(2, len(report["reasons"]))

    def test_the_reason_names_the_latest_series_not_just_a_count(self):
        report = layers.confidence(
            [reading("빠른 것", late=1), reading("느린 것", late=30)], 2
        )
        self.assertIn("느린 것", report["reasons"][0])
        self.assertIn("30일", report["reasons"][0])


class CardTests(unittest.TestCase):
    def test_a_split_layer_is_reported_as_split_rather_than_averaged(self):
        self.assertEqual(0, layers._stance([1, -1, 1, -1])[1])
        self.assertEqual("neutral", layers._stance([1, -1, 1, -1])[0])

    def test_a_stance_needs_half_the_votes_not_a_plurality(self):
        self.assertEqual("neutral", layers._stance([1, 0, 0, 0])[0])
        self.assertEqual("risk_on", layers._stance([1, 1, 0, 0])[0])
        self.assertEqual("risk_off", layers._stance([-1, -1, 0, 0])[0])
        self.assertEqual("unknown", layers._stance([])[0])

    def test_the_combined_confidence_counts_evidence_not_displayed_rows(self):
        # The cards cap what they show. Summing the displayed rows against the
        # full expected count reported a third of the base that actually voted.
        parts = [
            {"expected": 40, "fresh": 40, "stale": 0, "reasons": [], "method": "m"},
            {"expected": 20, "fresh": 18, "stale": 2, "reasons": ["x"], "method": "m"},
        ]
        combined = layers._combined(parts)
        self.assertEqual((60, 60, 58, 2), (
            combined["expected"], combined["voted"],
            combined["fresh"], combined["stale"]))
        self.assertEqual("high", combined["level"])

    def test_a_layer_with_no_expected_evidence_does_not_divide_by_zero(self):
        report = layers.confidence([], 0)
        self.assertEqual(0.0, report["strength"])
        self.assertEqual(0, report["expected"])


if __name__ == "__main__":
    unittest.main()
