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


class WriteGateTests(TemporaryDatabaseTest):
    """A browser will POST to localhost for any page the owner is visiting."""

    def client(self, **kwargs):
        from fastapi.testclient import TestClient
        from app import main

        # TestClient defaults to client "testclient" and Host "testserver",
        # which the gate correctly refuses — so a local request has to be
        # constructed deliberately.
        return TestClient(main.app, client=("127.0.0.1", 1234),
                          base_url="http://127.0.0.1:8077", **kwargs)

    def headers(self, **overrides):
        from app import webwrite

        base = {"Content-Type": "application/json",
                webwrite.TOKEN_HEADER: webwrite.token()}
        base.update(overrides)
        return base

    def body(self):
        return {"label": "테스트", "institution": "테스트 증권",
                "account_type": "general"}

    def test_the_happy_path_writes(self):
        response = self.client().post("/api/portfolio/account",
                                      json=self.body(), headers=self.headers())
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, len(portfolio.account_list()))

    def test_a_request_without_the_token_is_refused(self):
        from app import webwrite

        response = self.client().post(
            "/api/portfolio/account", json=self.body(),
            headers={"Content-Type": "application/json"})
        self.assertEqual(403, response.status_code)
        self.assertEqual([], portfolio.account_list())

    def test_a_request_with_the_wrong_token_is_refused(self):
        from app import webwrite

        response = self.client().post(
            "/api/portfolio/account", json=self.body(),
            headers=self.headers(**{webwrite.TOKEN_HEADER: "guessed"}))
        self.assertEqual(403, response.status_code)

    def test_a_form_content_type_is_refused_before_the_token_matters(self):
        # HTML forms can only send urlencoded, multipart or text/plain, and
        # those are exactly the request kinds that cross origins without a
        # preflight. Requiring JSON removes the whole class.
        response = self.client().post(
            "/api/portfolio/account", data="label=x",
            headers=self.headers(**{"Content-Type": "application/x-www-form-urlencoded"}))
        self.assertEqual(415, response.status_code)

    def test_a_non_local_client_address_is_refused(self):
        # The server binds 127.0.0.1 today, but a binding is a launch argument.
        # This is a property of the request, so it still holds the day someone
        # runs it on 0.0.0.0 or behind a proxy.
        from fastapi.testclient import TestClient
        from app import main

        remote = TestClient(main.app, client=("203.0.113.9", 4321),
                            base_url="http://127.0.0.1:8077")
        response = remote.post("/api/portfolio/account", json=self.body(),
                               headers=self.headers())
        self.assertEqual(403, response.status_code)
        self.assertIn("이 컴퓨터에서만", response.json()["detail"])
        self.assertEqual([], portfolio.account_list())

    def test_a_non_local_host_header_is_refused(self):
        # DNS rebinding: the attacker's domain resolves to 127.0.0.1, so the
        # client address looks local and only the Host header gives it away.
        response = self.client().post(
            "/api/portfolio/account", json=self.body(),
            headers=self.headers(Host="evil.example"))
        self.assertEqual(403, response.status_code)

    def test_the_host_check_ignores_the_port(self):
        from app import webwrite

        for host in ("127.0.0.1:9999", "localhost:1", "[::1]:8077", "localhost"):
            self.assertIn(webwrite._host_of(host), webwrite.LOCAL_HOSTS, host)
        self.assertNotIn(webwrite._host_of("evil.example:8077"), webwrite.LOCAL_HOSTS)

    def test_the_token_survives_a_restart(self):
        from app import webwrite

        first = webwrite.token()
        self.assertEqual(first, webwrite.token())
        self.assertEqual(first, db.get_meta(webwrite.TOKEN_KEY))

    def test_an_empty_paste_is_refused_rather_than_wiping_the_snapshot(self):
        account = portfolio.add_account("테스트", "테스트 증권", "general")
        portfolio.import_rows(account, "2026-08-30",
                              "source,dataset,symbol,name,stated_value\n,,A,합성,1000")
        with self.assertRaises(ValueError):
            portfolio.import_rows(account, "2026-08-30", "")
        self.assertEqual(1, len(portfolio.holdings(account, today=date(2026, 8, 30))))

    def test_an_empty_risk_flag_stays_unknown_through_the_web_path(self):
        account = portfolio.add_account("테스트", "테스트 증권", "retirement_dc")
        response = self.client().post("/api/portfolio/holdings", headers=self.headers(),
            json={"account_id": account, "as_of": "2026-08-30",
                  "csv": "source,dataset,symbol,name,stated_value,is_risky_asset\n"
                         ",,A,합성 A,1000,"})
        self.assertEqual(200, response.status_code, response.text)
        self.assertIsNone(
            portfolio.holdings(account, today=date(2026, 8, 30))[0]["is_risky_asset"])

    def test_a_preview_reports_the_replacement_without_writing(self):
        account = portfolio.add_account("테스트", "테스트 증권", "general")
        portfolio.import_rows(account, "2026-08-30",
                              "source,dataset,symbol,name,stated_value\n,,A,합성,1000")
        response = self.client().post("/api/portfolio/holdings", headers=self.headers(),
            json={"account_id": account, "as_of": "2026-08-30", "preview": True,
                  "csv": "source,dataset,symbol,name,stated_value\n,,B,합성 B,2000"})
        self.assertEqual(200, response.status_code)
        self.assertEqual((1, 1), (response.json()["would_remove"],
                                  response.json()["would_add"]))
        self.assertEqual(["A"], [item["symbol"] for item in
                                 portfolio.holdings(account, today=date(2026, 8, 30))])


class WriteSurfaceTests(TemporaryDatabaseTest):
    """The repository is public and an asset picture cannot be recalled."""

    def test_no_account_number_column_exists(self):
        with db.get_conn() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
        for banned in ("account_number", "account_no", "number", "iban"):
            self.assertNotIn(banned, columns)

    def test_every_asset_write_route_sits_behind_the_local_write_gate(self):
        # This test used to assert that no write route existed at all. Entry
        # moved to the browser on 2026-08-30 under the condition the design
        # set, so the assertion changed shape rather than being deleted: what
        # must hold now is that no write route can be reached without the gate.
        from app import main, webwrite

        writes = [
            route for route in main.app.routes
            if "portfolio" in getattr(route, "path", "")
            and {"POST", "PUT", "PATCH", "DELETE"} & set(getattr(route, "methods", []) or [])
        ]
        self.assertTrue(writes, "쓰기 라우트를 찾지 못했습니다")
        for route in writes:
            guards = [
                dependency.call
                for dependency in route.dependant.dependencies
            ]
            self.assertIn(webwrite.require_local_write, guards, route.path)

    def test_no_delete_route_exists_for_assets(self):
        # Deferred deliberately: a full-replace import is recoverable by
        # re-importing, but a deleted account is not, and nobody asked for it.
        from app import main

        for route in main.app.routes:
            if "portfolio" in getattr(route, "path", ""):
                self.assertNotIn("DELETE", set(getattr(route, "methods", []) or []))

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
