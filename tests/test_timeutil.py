"""Tests pinning the storage time convention.

The bug these exist to prevent shipped silently: reading a provider epoch as
a UTC date filed 476 ASX sessions under weekend dates, and every date-aligned
correlation involving that index was computed against the wrong day for the
five months Australia observes daylight saving.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import db
from app.collectors import indicators, indices, yahoo
from app.timeutil import (
    KST, exchange_date, instant_age_seconds, instant_epoch, parse_instant,
    to_kst, utc_now, utc_now_iso,
)


class ExchangeDateTests(unittest.TestCase):
    def test_southern_summer_session_keeps_its_local_date(self):
        # 2025-12-01 10:00 in Sydney is 2025-11-30 23:00 UTC.  Reading it as
        # a UTC date moves a Monday session onto a Sunday.
        self.assertEqual(exchange_date(1764543600, "Australia/Sydney"), "2025-12-01")

    def test_southern_winter_session_is_unaffected(self):
        self.assertEqual(exchange_date(1787270400, "Australia/Sydney"), "2026-08-21")

    def test_new_york_session_keeps_its_local_date(self):
        self.assertEqual(exchange_date(1787319000, "America/New_York"), "2026-08-21")

    def test_seoul_session_at_the_utc_midnight_boundary(self):
        self.assertEqual(exchange_date(1787270400, "Asia/Seoul"), "2026-08-21")

    def test_unknown_zone_falls_back_to_the_provider_offset(self):
        self.assertEqual(
            exchange_date(1764543600, "Nowhere/Unknown", gmt_offset=39600),
            "2025-12-01",
        )

    def test_unresolvable_zone_refuses_to_guess(self):
        # Failing one symbol loudly is recoverable; a silently wrong date is
        # not, because it corrupts every alignment downstream.
        with self.assertRaises(ValueError):
            exchange_date(1764543600, "Nowhere/Unknown")
        with self.assertRaises(ValueError):
            exchange_date(1764543600, None)


class InstantTests(unittest.TestCase):
    def test_offsetless_legacy_value_is_read_as_utc(self):
        parsed = parse_instant("2026-08-23T07:57:57")
        self.assertEqual(parsed.isoformat(), "2026-08-23T07:57:57+00:00")

    def test_offset_value_is_normalized_to_utc(self):
        parsed = parse_instant("2026-08-23T07:57:57+09:00")
        self.assertEqual(parsed.isoformat(), "2026-08-22T22:57:57+00:00")

    def test_unusable_values_are_none_rather_than_now(self):
        for value in (None, "", "not-a-time", "2026-13-45"):
            self.assertIsNone(parse_instant(value))

    def test_unknown_instant_is_infinitely_old_not_fresh(self):
        self.assertEqual(instant_age_seconds(None), float("inf"))
        self.assertEqual(instant_epoch(None), 0.0)

    def test_age_is_measured_against_utc_regardless_of_notation(self):
        now = utc_now()
        same_moment_in_kst = now.astimezone(KST).isoformat()
        self.assertAlmostEqual(
            instant_age_seconds(same_moment_in_kst, now=now), 0.0, places=3
        )

    def test_stored_instants_always_carry_an_offset(self):
        self.assertIsNotNone(parse_instant(utc_now_iso()))
        self.assertIn("+00:00", utc_now_iso())

    def test_presentation_conversion_lands_in_kst(self):
        self.assertEqual(to_kst("2026-08-25T22:35:00+00:00").hour, 7)


class CatalogDateKindTests(unittest.TestCase):
    def test_traded_prices_are_trading_day_series(self):
        catalog = indicators.catalog()
        self.assertEqual(catalog["us_10y"]["date_kind"], "trading_day")
        self.assertEqual(catalog["gold"]["date_kind"], "trading_day")

    def test_standing_policy_rates_are_calendar_day_series(self):
        # These publish a value every day the rate is in effect, so a missing
        # weekend is a real gap rather than a market holiday.
        catalog = indicators.catalog()
        self.assertEqual(catalog["kr_base_rate"]["date_kind"], "calendar_day")
        self.assertEqual(catalog["eu_rate"]["date_kind"], "calendar_day")

    def test_lower_frequency_series_are_period_stamped(self):
        catalog = indicators.catalog()
        self.assertEqual(catalog["us_cpi"]["date_kind"], "period_start")
        self.assertEqual(catalog["us_gdp"]["date_kind"], "period_start")

    def test_every_catalog_entry_declares_a_known_kind(self):
        allowed = {"trading_day", "calendar_day", "period_start"}
        for key, spec in indicators.catalog().items():
            self.assertIn(spec["date_kind"], allowed, key)


class YahooPayloadTests(unittest.TestCase):
    @staticmethod
    def _payload(
        timestamps, closes, tz="Australia/Sydney", offset=39600, **meta_extra
    ):
        return {"chart": {"result": [{
            "meta": {
                "exchangeTimezoneName": tz, "gmtoffset": offset, **meta_extra,
            },
            "timestamp": list(timestamps),
            "indicators": {"quote": [{"close": list(closes)}]},
        }]}}

    def test_points_are_dated_in_exchange_local_time(self):
        parsed = indices._points(self._payload([1764543600], [8500.0]))
        self.assertEqual(parsed[0]["date"], "2025-12-01")

    def test_settled_session_ignores_an_unsettled_trailing_bar(self):
        # Yahoo emits the current session's bar with a null close.  Treating
        # it as expected coverage would make completeness unreachable.
        payload = self._payload([1787270400, 1787529600], [8500.0, None])
        self.assertEqual(yahoo.settled_session(payload), "2026-08-21")

    def test_settled_session_is_none_when_nothing_has_closed(self):
        self.assertIsNone(yahoo.settled_session(self._payload([1787270400], [None])))
        self.assertIsNone(yahoo.settled_session({"chart": {"result": []}}))

    def test_points_reject_a_payload_with_no_usable_closes(self):
        with self.assertRaises(ValueError):
            indices._points(self._payload([1787270400], [None]))


class PreviousCloseTests(unittest.TestCase):
    """`chartPreviousClose` is window-relative and produced wrong signs.

    Observed on 2026-08-25: S&P 500 at 7677.28 against a settled previous
    close of 7652.86 is +0.32%, but the provider field gave 7691.76 over a
    five-day window and 7674.37 over two, yielding -0.19% and +0.04%.  The
    previous close is therefore derived from the settled bars instead.
    """

    @staticmethod
    def _payload(bars, live_epoch, price, tz="America/New_York"):
        return {"chart": {"result": [{
            "meta": {
                "exchangeTimezoneName": tz,
                "gmtoffset": -14400,
                "regularMarketTime": live_epoch,
                "regularMarketPrice": price,
                "chartPreviousClose": 99999.0,  # must be ignored
                "currency": "USD",
            },
            "timestamp": [epoch for epoch, _ in bars],
            "indicators": {"quote": [{"close": [close for _, close in bars]}]},
        }]}}

    def test_settled_live_session_compares_against_the_prior_session(self):
        # 08-24 and 08-25 both settled; the live price is the 08-25 bar.
        payload = self._payload(
            [(1787578200, 7652.86), (1787664600, 7677.28)],
            live_epoch=1787694229, price=7677.28,
        )
        fields = yahoo.quote_fields(payload)
        self.assertEqual(fields["prev_close"], 7652.86)
        self.assertEqual(fields["session_date"], "2026-08-25")

    def test_unsettled_live_session_compares_against_the_last_settled_close(self):
        # The live session's bar has no close yet, so the last settled bar is
        # the previous close rather than the one before it.
        payload = self._payload(
            [(1787578200, 7652.86), (1787664600, None)],
            live_epoch=1787694229, price=7677.28,
        )
        fields = yahoo.quote_fields(payload)
        self.assertEqual(fields["prev_close"], 7652.86)
        self.assertEqual(fields["session_date"], "2026-08-24")
        self.assertEqual(fields["live_session_date"], "2026-08-25")

    def test_change_percent_keeps_its_sign(self):
        payload = self._payload(
            [(1787578200, 7652.86), (1787664600, 7677.28)],
            live_epoch=1787694229, price=7677.28,
        )
        fields = yahoo.quote_fields(payload)
        change = (fields["price"] - fields["prev_close"]) / fields["prev_close"] * 100
        self.assertGreater(change, 0)
        self.assertAlmostEqual(change, 0.319, places=2)

    def test_a_single_settled_bar_yields_no_previous_close(self):
        payload = self._payload(
            [(1787664600, 7677.28)], live_epoch=1787694229, price=7677.28
        )
        self.assertIsNone(yahoo.quote_fields(payload)["prev_close"])


class CuratedCalendarConversionTests(unittest.TestCase):
    """Every curated event must survive re-derivation from its own source.

    A hand-written calendar is where a daylight-saving mistake hides best:
    the same 08:30 release is 21:30 KST in September and 22:30 KST in
    December, and nothing but the conversion catches a transcription that
    used one offset all year.
    """

    def test_every_timed_event_matches_its_source_conversion(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from app.collectors import curated

        checked = 0
        for event in curated.load():
            source_date = event.get("source_date")
            source_time = event.get("source_time")
            source_zone = event.get("source_timezone")
            if not (source_date and source_time and source_zone):
                continue
            moment = datetime.fromisoformat(
                f"{source_date}T{source_time}"
            ).replace(tzinfo=ZoneInfo(source_zone))
            in_kst = moment.astimezone(KST)
            self.assertEqual(in_kst.date().isoformat(), event["date"], event["title"])
            self.assertEqual(in_kst.strftime("%H:%M"), event["time"], event["title"])
            checked += 1
        self.assertGreater(checked, 20)

    def test_the_calendar_spans_both_sides_of_a_dst_transition(self):
        # Without events on both sides, the check above would pass on a
        # calendar that hard-coded a single offset.
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from app.collectors import curated

        observances = set()
        for event in curated.load():
            if not (event.get("source_date") and event.get("source_timezone")):
                continue
            moment = datetime.fromisoformat(
                f"{event['source_date']}T{event.get('source_time') or '12:00'}"
            ).replace(tzinfo=ZoneInfo(event["source_timezone"]))
            observances.add(bool(moment.dst()))
        self.assertEqual(observances, {True, False})


class InstantMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def test_offsetless_instants_are_stamped_and_migration_is_idempotent(self):
        db.init_db()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO collect_log (ts, category, status, ok, total) "
                "VALUES ('2026-08-23T07:57:57', 'quotes', 'success', 1, 1)"
            )
        db.init_db()
        with db.get_conn() as conn:
            stored = conn.execute("SELECT ts FROM collect_log").fetchone()["ts"]
        self.assertEqual(stored, "2026-08-23T07:57:57+00:00")

        db.init_db()  # a second pass must not append another offset
        with db.get_conn() as conn:
            self.assertEqual(
                conn.execute("SELECT ts FROM collect_log").fetchone()["ts"],
                "2026-08-23T07:57:57+00:00",
            )

    def test_schema_v8_columns_exist(self):
        db.init_db()
        with db.get_conn() as conn:
            quote_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(index_quotes)")
            }
            catalog_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(series_catalog)")
            }
        self.assertIn("session_date", quote_columns)
        self.assertIn("date_kind", catalog_columns)
        self.assertEqual(db.get_meta("schema_version"), "8")

    def test_index_quote_keeps_a_known_session_when_a_refresh_omits_it(self):
        db.init_db()
        db.save_index_quote({
            "symbol": "^KS11", "price": 1.0, "prev_close": 1.0,
            "currency": "KRW", "session_date": "2026-08-25",
            "updated_at": db.utc_now(),
        })
        db.save_index_quote({
            "symbol": "^KS11", "price": 2.0, "prev_close": 1.0,
            "currency": "KRW", "updated_at": db.utc_now(),
        })
        self.assertEqual(
            db.get_index_quotes()["^KS11"]["session_date"], "2026-08-25"
        )


if __name__ == "__main__":
    unittest.main()
