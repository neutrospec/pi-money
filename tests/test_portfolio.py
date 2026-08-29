"""Accounts and holdings — and the prohibitions the design turns into tests.

Every fixture here is invented. The repository is public and the design's
strongest rule is that no real account, balance or holding may enter a
committed file; a test that used real numbers would break that rule in the one
place nobody looks.

Most of what follows asserts what the module must *refuse* to do: no single
total, no unknown counted as safe, no proposed figure in a calculation, no
partial import leaving a sold position on the books.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from app import accounts, db, portfolio


class TemporaryDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.tempdir.name) / "money-test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_path
        self.tempdir.cleanup()

    def csv(self, rows, header=None):
        header = header or ("source,dataset,symbol,name,quantity,book_amount,"
                            "stated_value,currency,is_risky_asset,note")
        path = Path(self.tempdir.name) / "holdings.csv"
        path.write_text("\n".join([header, *rows]), encoding="utf-8")
        return str(path)

    def priced(self, dataset, symbol, day, close):
        with db.get_conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO market_instruments
                   (source, dataset, symbol, name, asset_type, first_seen, last_seen)
                   VALUES ('krx',?,?,?,'etf',?,?)""",
                (dataset, symbol, symbol, day, day))
            conn.execute(
                """INSERT OR REPLACE INTO market_daily
                   (source, dataset, symbol, date, name, close, raw_json, retrieved_at)
                   VALUES ('krx',?,?,?,?,?, '{}', ?)""",
                (dataset, symbol, day, symbol, close, day))


class PolicyTests(unittest.TestCase):
    def test_every_policy_item_carries_all_eight_fields(self):
        # A missing `status` turns a bill into a current figure; a missing
        # `scope` turns a per-person ceiling into a per-account one and shows
        # twice the headroom that exists.
        for key, item in accounts.POLICY.items():
            self.assertEqual(set(accounts.POLICY_FIELDS), set(item), key)

    def test_a_proposed_figure_never_answers_a_request_for_a_current_one(self):
        self.assertIsNone(accounts.in_force("isa_contribution_annual_proposed"))
        self.assertIsNotNone(accounts.in_force("isa_contribution_annual"))

    def test_the_current_isa_limit_is_the_one_that_passed(self):
        # The widely-quoted 40 million figure is the 2024 bill, which did not
        # pass. Pinning the real one here is the point of the module.
        self.assertEqual(20_000_000, accounts.POLICY["isa_contribution_annual"]["value"])
        self.assertEqual(accounts.PROPOSED,
                         accounts.POLICY["isa_contribution_annual_proposed"]["status"])

    def test_the_pension_ceiling_is_per_person_not_per_account(self):
        self.assertEqual("user", accounts.POLICY["pension_contribution_annual"]["scope"])
        self.assertEqual("account", accounts.POLICY["isa_contribution_annual"]["scope"])

    def test_buy_gates_are_stated_as_differences_from_a_general_account(self):
        self.assertEqual((), accounts.gates_for("general"))
        self.assertEqual((), accounts.gates_for("managed"))
        self.assertIn("individual_stock", accounts.gates_for("pension_savings"))
        # An ISA may hold domestic individual stocks; only foreign ones are out.
        self.assertNotIn("individual_stock", accounts.gates_for("isa"))
        self.assertIn("foreign_individual_stock", accounts.gates_for("isa"))

    def test_every_gate_has_a_label(self):
        for account_type in accounts.ACCOUNT_TYPES:
            for gate in accounts.gates_for(account_type):
                self.assertIn(gate, accounts.GATE_LABELS)


class ValuationTests(TemporaryDatabaseTest):
    def test_there_is_no_single_total_anywhere_in_the_payload(self):
        # The prohibition that shapes the whole module. Adding a live price to
        # a stale one to a number the owner typed, across currencies, produces
        # a figure that means nothing and reads as net worth.
        account = portfolio.add_account("테스트 계좌", "테스트 증권", "general")
        path = self.csv([
            "krx,etf_bydd_trd,AAA,합성 ETF,3,,,KRW,,",
            ",,CASH,합성 예금,,,1000,KRW,,",
            ",,FOREIGN,합성 해외자산,5,,,USD,,",
        ])
        portfolio.import_snapshot(account, "2026-08-30", path)
        payload = portfolio.overview(date(2026, 8, 30))
        banned = {"total", "total_value", "net_worth", "sum", "grand_total"}
        self.assertEqual(set(), banned & set(payload))
        for item in payload["accounts"]:
            self.assertEqual(set(), banned & set(item))
            self.assertEqual(set(), banned & set(item["valuation"]))
            # Every bucket names its currency, so nothing can be added across
            # currencies without the caller noticing it is doing so.
            for bucket in item["valuation"]["buckets"]:
                self.assertIn("currency", bucket)

    def test_a_fresh_close_grades_as_market_and_an_old_one_as_stale(self):
        account = portfolio.add_account("테스트", "테스트 증권", "general")
        self.priced("etf_bydd_trd", "AAA", "2026-08-28", 10.0)
        self.priced("etf_bydd_trd", "BBB", "2026-01-02", 20.0)
        path = self.csv(["krx,etf_bydd_trd,AAA,합성 A,2,,,KRW,,",
                         "krx,etf_bydd_trd,BBB,합성 B,3,,,KRW,,"])
        portfolio.import_snapshot(account, "2026-08-30", path)
        graded = {item["symbol"]: item
                  for item in portfolio.holdings(account, today=date(2026, 8, 30))}
        self.assertEqual(portfolio.MARKET, graded["AAA"]["grade"])
        self.assertEqual(portfolio.STALE, graded["BBB"]["grade"])
        self.assertEqual(20.0, graded["AAA"]["amount"])

    def test_an_unpriced_holding_is_not_counted_as_zero(self):
        account = portfolio.add_account("테스트", "테스트 증권", "general")
        path = self.csv([",,MYSTERY,값을 모르는 자산,7,,,KRW,,"])
        portfolio.import_snapshot(account, "2026-08-30", path)
        items = portfolio.holdings(account, today=date(2026, 8, 30))
        self.assertEqual(portfolio.UNPRICED, items[0]["grade"])
        self.assertIsNone(items[0]["amount"])
        report = portfolio.valuation(items)
        self.assertEqual(1, report["unpriced"])
        # It appears with a count, never folded into an amount.
        bucket = report["buckets"][0]
        self.assertEqual((portfolio.UNPRICED, 1, 0.0),
                         (bucket["grade"], bucket["holdings"], bucket["amount"]))

    def test_an_instrument_master_dataset_is_never_used_as_a_price_source(self):
        # These tables carry every row with a NULL close. Joining to one looks
        # like a price lookup and silently returns nothing for everything.
        account = portfolio.add_account("테스트", "테스트 증권", "general")
        with db.get_conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO market_instruments
                   (source, dataset, symbol, name, asset_type, first_seen, last_seen)
                   VALUES ('krx','stk_isu_base_info','CCC','CCC','stock',
                           '2026-08-28','2026-08-28')""")
            conn.execute(
                """INSERT INTO market_daily
                   (source, dataset, symbol, date, name, close, raw_json, retrieved_at)
                   VALUES ('krx','stk_isu_base_info','CCC','2026-08-28','CCC',
                           NULL,'{}','2026-08-28')""")
        path = self.csv(["krx,stk_isu_base_info,CCC,합성 종목,5,,,KRW,,"])
        portfolio.import_snapshot(account, "2026-08-30", path)
        items = portfolio.holdings(account, today=date(2026, 8, 30))
        self.assertEqual(portfolio.UNPRICED, items[0]["grade"])
        self.assertIn("stk_isu_base_info", portfolio.UNPRICED_DATASETS)


class RiskyAssetTests(TemporaryDatabaseTest):
    """Unknown is not safe. The DC limit's correctness rests on that."""

    def account_with(self, rows):
        account = portfolio.add_account("테스트 DC", "테스트 은행", "retirement_dc")
        portfolio.import_snapshot(account, "2026-08-30", self.csv(rows))
        return account

    def test_an_unclassified_holding_makes_the_share_undecidable(self):
        account = self.account_with([",,A,합성 A,,,1000,KRW,1,",
                                     ",,B,합성 B,,,1000,KRW,,"])
        report = portfolio.risky_share(
            portfolio.account_list()[0],
            portfolio.holdings(account, today=date(2026, 8, 30)))
        self.assertFalse(report["decidable"])
        self.assertEqual(1, report["unclassified"])
        self.assertNotIn("share_pct", report)

    def test_an_explicitly_safe_holding_is_not_the_same_as_an_unknown_one(self):
        # 0 and NULL round-trip differently through SQLite and mean different
        # things; the entire gate depends on the distinction.
        account = self.account_with([",,A,합성 A,,,1000,KRW,1,",
                                     ",,B,합성 B,,,1000,KRW,0,"])
        items = portfolio.holdings(account, today=date(2026, 8, 30))
        self.assertEqual({0, 1}, {item["is_risky_asset"] for item in items})
        report = portfolio.risky_share(portfolio.account_list()[0], items)
        self.assertTrue(report["decidable"])
        self.assertEqual(50.0, report["share_pct"])

    def test_the_unclassified_holding_is_not_dropped_from_the_denominator(self):
        account = self.account_with([",,A,합성 A,,,1000,KRW,1,",
                                     ",,B,합성 B,,,1000,KRW,,"])
        report = portfolio.risky_share(
            portfolio.account_list()[0],
            portfolio.holdings(account, today=date(2026, 8, 30)))
        # Dropping it would give 100%, which is the failure this refuses.
        self.assertNotIn("share_pct", report)

    def test_the_limit_is_shown_without_computing_the_headroom(self):
        # "현재 61% / 한도 70%" is the end of the display. The difference is
        # room to act, which is advice by another name.
        account = self.account_with([",,A,합성 A,,,1000,KRW,0,"])
        report = portfolio.risky_share(
            portfolio.account_list()[0],
            portfolio.holdings(account, today=date(2026, 8, 30)))
        self.assertEqual(70, report["limit_pct"])
        self.assertEqual(set(), {"headroom", "remaining", "room"} & set(report))

    def test_the_limit_does_not_apply_to_other_account_types(self):
        account = portfolio.add_account("테스트 일반", "테스트 증권", "general")
        portfolio.import_snapshot(account, "2026-08-30",
                                  self.csv([",,A,합성 A,,,1000,KRW,1,"]))
        report = portfolio.risky_share(
            portfolio.account_list()[0],
            portfolio.holdings(account, today=date(2026, 8, 30)))
        self.assertFalse(report["applicable"])


class ImportTests(TemporaryDatabaseTest):
    def test_reimporting_the_same_date_replaces_the_snapshot_entirely(self):
        # A merge would leave a sold position on the books forever.
        account = portfolio.add_account("테스트", "테스트 증권", "general")
        portfolio.import_snapshot(account, "2026-08-30", self.csv([
            ",,A,합성 A,,,1000,KRW,,", ",,B,합성 B,,,2000,KRW,,"]))
        result = portfolio.import_snapshot(account, "2026-08-30",
                                           self.csv([",,A,합성 A,,,1500,KRW,,"]))
        self.assertEqual((2, 1), (result["removed"], result["added"]))
        items = portfolio.holdings(account, today=date(2026, 8, 30))
        self.assertEqual(["A"], [item["symbol"] for item in items])
        self.assertEqual(1500.0, items[0]["stated_value"])

    def test_a_dry_run_reports_the_replacement_without_performing_it(self):
        account = portfolio.add_account("테스트", "테스트 증권", "general")
        portfolio.import_snapshot(account, "2026-08-30",
                                  self.csv([",,A,합성 A,,,1000,KRW,,"]))
        result = portfolio.import_snapshot(
            account, "2026-08-30", self.csv([",,B,합성 B,,,2000,KRW,,"]),
            dry_run=True)
        self.assertEqual((1, 1), (result["would_remove"], result["would_add"]))
        self.assertEqual(["A"], [item["symbol"] for item
                                 in portfolio.holdings(account, today=date(2026, 8, 30))])

    def test_a_different_date_is_a_new_snapshot_not_a_replacement(self):
        account = portfolio.add_account("테스트", "테스트 증권", "general")
        portfolio.import_snapshot(account, "2026-08-29",
                                  self.csv([",,A,합성 A,,,1000,KRW,,"]))
        portfolio.import_snapshot(account, "2026-08-30",
                                  self.csv([",,B,합성 B,,,2000,KRW,,"]))
        self.assertEqual("2026-08-30", portfolio.latest_as_of(account))
        self.assertEqual(["A"], [item["symbol"] for item in
                                 portfolio.holdings(account, "2026-08-29",
                                                    date(2026, 8, 30))])

    def test_an_absent_risk_classification_stays_unknown(self):
        account = portfolio.add_account("테스트", "테스트 증권", "general")
        portfolio.import_snapshot(account, "2026-08-30",
                                  self.csv([",,A,합성 A,,,1000,KRW,,"]))
        self.assertIsNone(
            portfolio.holdings(account, today=date(2026, 8, 30))[0]["is_risky_asset"])


class WriteSurfaceTests(TemporaryDatabaseTest):
    """The repository is public and an asset picture cannot be recalled."""

    def test_no_account_number_column_exists(self):
        with db.get_conn() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
        for banned in ("account_number", "account_no", "number", "iban"):
            self.assertNotIn(banned, columns)

    def test_the_web_layer_offers_no_write_path_for_assets(self):
        from app import main

        for route in main.app.routes:
            path = getattr(route, "path", "")
            if "portfolio" in path or "account" in path:
                self.assertEqual({"GET", "HEAD"},
                                 set(getattr(route, "methods", []) or {"GET"}) & {"POST", "PUT", "DELETE", "PATCH"} or {"GET", "HEAD"},
                                 path)

    def test_an_unknown_account_type_is_refused_rather_than_stored(self):
        with self.assertRaises(ValueError):
            portfolio.add_account("테스트", "테스트 증권", "crypto_casino")

    def test_an_unknown_exposure_tag_is_refused(self):
        with self.assertRaises(ValueError):
            portfolio.tag_instrument("krx", "etf_bydd_trd", "AAA", "moon")

    def test_an_unknown_cashflow_kind_is_refused(self):
        account = portfolio.add_account("테스트", "테스트 증권", "isa")
        with self.assertRaises(ValueError):
            portfolio.record_flow(account, "2026-08-30", "gift", 1000)

    def test_transfers_are_recorded_apart_from_deposits(self):
        # An ISA maturity rolled into a pension account is excluded from the
        # contribution ceiling; merging the two overstates remaining room.
        account = portfolio.add_account("테스트", "테스트 증권", "pension_savings")
        portfolio.record_flow(account, "2026-08-30", "deposit", 1000)
        portfolio.record_flow(account, "2026-08-30", "transfer_in", 2000)
        with db.get_conn() as conn:
            kinds = {row["kind"] for row in
                     conn.execute("SELECT kind FROM account_cashflows")}
        self.assertEqual({"deposit", "transfer_in"}, kinds)


if __name__ == "__main__":
    unittest.main()
