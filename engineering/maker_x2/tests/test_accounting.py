"""Deterministic public-fixture tests for the short-only Maker X2 ledger.

Expected Decimal numbers below are independently hand-derived, not computed
by a second call to the production implementation.
Run in engineering/maker_x2 with:
    python3 -m unittest discover -s tests -v
"""

import unittest
from decimal import Decimal

from accounting import LedgerError, replay


D = Decimal


def fill(seq, ts, side, qty, price, liquidity="maker", event_id=None):
    return {
        "id": event_id if event_id is not None else "fill-{}".format(seq),
        "seq": seq,
        "ts": ts,
        "type": "fill",
        "side": side,
        "qty": qty,
        "price": price,
        "liquidity": liquidity,
    }


def funding(seq, ts, settlement_id, mark, rate, event_id=None):
    return {
        "id": event_id if event_id is not None else "fund-{}".format(seq),
        "seq": seq,
        "ts": ts,
        "type": "funding",
        "settlement_id": settlement_id,
        "mark": mark,
        "rate": rate,
    }


class AccountingReplayTests(unittest.TestCase):
    def assert_money(self, actual, expected):
        for key, literal in expected.items():
            self.assertEqual(actual[key], D(literal), key)
            self.assertIsInstance(actual[key], Decimal, key)

    def test_split_entries_different_prices_and_half_exit(self):
        # 2*100 + 2*110 = 420, weighted entry = 105.
        # Close 2 at 90: gross 30, remaining short = 2 with basis 210.
        # Fees: 200*.0002=.04, 220*.0005=.11, 180*.0005=.09.
        result = replay("1000", [
            fill(0, 1000, "sell", "2", "100", "maker"),
            fill(1, 2000, "sell", "2", "110", "taker"),
            fill(2, 3000, "buy", "2", "90", "taker"),
        ])
        self.assert_money(result, {
            "initial_cash": "1000", "cash": "1029.76",
            "realized_gross": "30", "fees_paid": "0.24",
            "funding_cost": "0", "realized_net": "29.76",
            "remaining_signed_qty": "-2", "remaining_qty": "2",
            "entry_basis": "210", "average_entry_price": "105",
        })
        self.assertFalse(result["is_flat"])
        self.assertEqual(result["event_count"], 3)
        self.assertEqual(len(result["settlements"]), 0)
        self.assertEqual(
            [item["fee"] for item in result["fills"]],
            [D("0.04"), D("0.11"), D("0.09")],
        )
        self.assertEqual(
            [item["fee_rate"] for item in result["fills"]],
            [D("0.0002"), D("0.0005"), D("0.0005")],
        )
        self.assertEqual(
            [item["realized_gross"] for item in result["fills"]],
            [D("0"), D("0"), D("30")],
        )
        self.assertEqual(
            [item["remaining_signed_qty"] for item in result["fills"]],
            [D("-2"), D("-4"), D("-2")],
        )
        self.assertEqual(
            [(item["side"], item["qty"], item["price"]) for item in result["fills"]],
            [("sell", D("2"), D("100")), ("sell", D("2"), D("110")),
             ("buy", D("2"), D("90"))],
        )
        self.assertEqual(
            result["cash"],
            D("1000") + D("30") - D("0.24") - D("0"),
        )

    def test_actual_gap_exit_price_realizes_loss_and_flattens(self):
        # A fill at 130 is never replaced by a wished-for exit/mark of 100.
        # Gross = (100-90)*1 + (100-130)*1 = -20.
        # Fees: 200*.0002=.04, 90*.0005=.045, 130*.0002=.026.
        result = replay("1000", [
            fill(0, 10, "sell", "2", "100"),
            fill(1, 20, "buy", "1", "90", "taker"),
            fill(2, 30, "buy", "1", "130", "maker"),
        ])
        self.assert_money(result, {
            "cash": "979.889", "realized_gross": "-20",
            "fees_paid": "0.111", "funding_cost": "0",
            "realized_net": "-20.111",
            "remaining_signed_qty": "0", "remaining_qty": "0",
            "entry_basis": "0",
        })
        self.assertTrue(result["is_flat"])
        self.assertIsNone(result["average_entry_price"])
        self.assertEqual(result["event_count"], 3)
        self.assertEqual(result["fills"][2]["price"], D("130"))
        self.assertEqual(result["fills"][2]["realized_gross"], D("-30"))
        self.assertEqual(
            [item["fee"] for item in result["fills"]],
            [D("0.04"), D("0.045"), D("0.026")],
        )

    def test_flat_round_trip_does_not_credit_short_sale_notional(self):
        result = replay(1000, [
            fill(0, 10, "sell", 2, 100),
            fill(1, 20, "buy", 2, 100),
        ])
        self.assert_money(result, {
            "cash": "999.92", "realized_gross": "0",
            "fees_paid": "0.08", "funding_cost": "0",
            "realized_net": "-0.08",
            "entry_basis": "0", "remaining_signed_qty": "0",
            "remaining_qty": "0",
        })
        self.assertTrue(result["is_flat"])
        self.assertIsNone(result["average_entry_price"])

    def test_positive_and_negative_funding_signed_cash(self):
        # Short 4; positive 0.01 at 120 credits 4.80;
        # negative -0.005 at 80 debits 1.60; opening fee .08.
        result = replay("1000", [
            fill(0, 100, "sell", "4", "100"),
            funding(1, 200, "funding-A", "120", "0.01"),
            funding(2, 300, "funding-B", "80", "-0.005"),
        ])
        self.assert_money(result, {
            "cash": "1003.12", "realized_gross": "0",
            "fees_paid": "0.08", "funding_cost": "-3.20",
            "realized_net": "3.12",
            "remaining_signed_qty": "-4", "remaining_qty": "4",
            "entry_basis": "400", "average_entry_price": "100",
        })
        self.assertEqual(
            [s["funding_cost"] for s in result["settlements"]],
            [D("-4.80"), D("1.60")],
        )
        self.assertEqual(
            [s["cash_delta"] for s in result["settlements"]],
            [D("4.80"), D("-1.60")],
        )
        self.assertEqual(
            [s["signed_qty"] for s in result["settlements"]],
            [D("-4"), D("-4")],
        )
        self.assertEqual(
            [s["settlement_id"] for s in result["settlements"]],
            ["funding-A", "funding-B"],
        )
        self.assertFalse(result["is_flat"])

    def test_funding_after_half_exit_uses_only_remaining_signed_qty(self):
        # Sell 4 @100 taker=.20; buy 2 @90 maker=.036, gross 20.
        # Funding on remaining -2 @95 and +.01 credits 1.90 (not 3.80).
        result = replay("1000", [
            fill(0, 100, "sell", "4", "100", "taker"),
            fill(1, 200, "buy", "2", "90", "maker"),
            funding(2, 300, "funding-after-half", "95", "0.01"),
        ])
        self.assert_money(result, {
            "cash": "1021.664", "realized_gross": "20",
            "fees_paid": "0.236", "funding_cost": "-1.90",
            "realized_net": "21.664",
            "remaining_signed_qty": "-2", "remaining_qty": "2",
            "entry_basis": "200", "average_entry_price": "100",
        })
        self.assertEqual(result["settlements"][0]["signed_qty"], D("-2"))
        self.assertEqual(result["settlements"][0]["cash_delta"], D("1.90"))
        self.assertFalse(result["is_flat"])

    def test_same_timestamp_sequence_changes_funding_not_execution_order(self):
        # Both event sets execute in given seq order, not by type or timestamp.
        # Sell fee .08; half buy @100 taker fee .10; no gross PnL.
        # Funding before buy credits 4; funding after buy credits 2.
        before = replay("1000", [
            fill(0, 100, "sell", "4", "100"),
            funding(1, 200, "same-before", "100", "0.01"),
            fill(2, 200, "buy", "2", "100", "taker"),
        ])
        after = replay("1000", [
            fill(0, 100, "sell", "4", "100"),
            fill(1, 200, "buy", "2", "100", "taker"),
            funding(2, 200, "same-after", "100", "0.01"),
        ])
        self.assert_money(before, {
            "cash": "1003.82", "realized_gross": "0",
            "fees_paid": "0.18", "funding_cost": "-4",
            "realized_net": "3.82", "remaining_signed_qty": "-2",
            "entry_basis": "200", "average_entry_price": "100",
        })
        self.assert_money(after, {
            "cash": "1001.82", "realized_gross": "0",
            "fees_paid": "0.18", "funding_cost": "-2",
            "realized_net": "1.82", "remaining_signed_qty": "-2",
            "entry_basis": "200", "average_entry_price": "100",
        })
        self.assertEqual(before["settlements"][0]["signed_qty"], D("-4"))
        self.assertEqual(after["settlements"][0]["signed_qty"], D("-2"))
        self.assertEqual(before["settlements"][0]["cash_delta"], D("4"))
        self.assertEqual(after["settlements"][0]["cash_delta"], D("2"))
        self.assertEqual(before["cash"] - after["cash"], D("2"))

    def test_open_short_is_not_synthetically_closed_or_marked(self):
        result = replay("1000", [
            fill(0, 100, "sell", "1", "1000", "taker"),
        ])
        self.assert_money(result, {
            "cash": "999.5", "realized_gross": "0",
            "fees_paid": "0.5", "funding_cost": "0",
            "realized_net": "-0.5", "remaining_signed_qty": "-1",
            "remaining_qty": "1", "entry_basis": "1000",
            "average_entry_price": "1000",
        })
        self.assertFalse(result["is_flat"])
        self.assertEqual(result["event_count"], 1)
        self.assertEqual(len(result["fills"]), 1)
        self.assertNotIn("unrealized_pnl", result)
        self.assertNotIn("equity", result)
        self.assertNotIn("terminal_close", result)

    def test_empty_events_are_flat_without_fabricated_pnl(self):
        result = replay("345.67", [])
        self.assert_money(result, {
            "initial_cash": "345.67", "cash": "345.67",
            "realized_gross": "0", "fees_paid": "0",
            "funding_cost": "0", "realized_net": "0",
            "remaining_signed_qty": "0", "remaining_qty": "0",
            "entry_basis": "0",
        })
        self.assertTrue(result["is_flat"])
        self.assertIsNone(result["average_entry_price"])
        self.assertEqual(result["event_count"], 0)
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["settlements"], [])

    def test_duplicate_event_ids_across_fill_and_funding_are_rejected(self):
        for events in (
            [fill(0, 10, "sell", "1", "100", event_id="dup"),
             fill(1, 20, "sell", "1", "100", event_id="dup")],
            [fill(0, 10, "sell", "1", "100", event_id="dup"),
             funding(1, 20, "settle-A", "100", "0.01", event_id="dup")],
        ):
            with self.subTest(events=events), self.assertRaises(LedgerError):
                replay("1000", events)

    def test_duplicate_settlement_ids_with_distinct_event_ids_rejected(self):
        with self.assertRaisesRegex(LedgerError, "duplicate funding settlement"):
            replay("1000", [
                fill(0, 10, "sell", "1", "100"),
                funding(1, 20, "duplicate", "100", "0.01"),
                funding(2, 30, "duplicate", "105", "-0.01"),
            ])

    def test_sequence_must_start_at_zero_without_gaps_or_duplicates(self):
        cases = (
            [fill(1, 10, "sell", "1", "100")],
            [fill(0, 10, "sell", "1", "100"),
             fill(2, 20, "sell", "1", "100")],
            [fill(0, 10, "sell", "1", "100"),
             fill(0, 20, "sell", "1", "100", event_id="other")],
            [fill(0, 10, "sell", "1", "100"),
             fill(1, 20, "sell", "1", "100"),
             fill(1, 30, "sell", "1", "100", event_id="third")],
        )
        for events in cases:
            with self.subTest(events=events), self.assertRaisesRegex(
                LedgerError, "sequence gap/order"
            ):
                replay("1000", events)

    def test_sequence_rejects_bool_float_and_noninteger(self):
        for value in (True, False, 0.0, "0", D("0")):
            with self.subTest(seq=repr(value)), self.assertRaises(LedgerError):
                replay("1000", [fill(value, 10, "sell", "1", "100")])

    def test_backwards_or_noninteger_timestamp_rejected(self):
        cases = (
            [fill(0, 20, "sell", "1", "100"),
             fill(1, 19, "sell", "1", "100")],
            [fill(0, -1, "sell", "1", "100")],
            [fill(0, True, "sell", "1", "100")],
            [fill(0, 10.0, "sell", "1", "100")],
            [fill(0, "10", "sell", "1", "100")],
        )
        for events in cases:
            with self.subTest(events=events), self.assertRaises(LedgerError):
                replay("1000", events)

    def test_buy_without_short_and_overclose_rejected(self):
        cases = (
            [fill(0, 10, "buy", "1", "100")],
            [fill(0, 10, "sell", "1", "100"),
             fill(1, 20, "buy", "2", "90")],
            [fill(0, 10, "sell", "2", "100"),
             fill(1, 20, "buy", "1", "90"),
             fill(2, 30, "buy", "2", "80")],
            [fill(0, 10, "sell", "1", "100"),
             fill(1, 20, "buy", "1", "100"),
             fill(2, 30, "buy", "1", "100")],
        )
        for events in cases:
            with self.subTest(events=events), self.assertRaises(LedgerError):
                replay("1000", events)

    def test_nonpositive_fill_qty_and_price_rejected_on_actual_legs(self):
        for field in ("qty", "price"):
            for value in ("0", "-1", 0, -1):
                for side in ("sell", "buy"):
                    with self.subTest(field=field, value=value, side=side):
                        event = fill(1 if side == "buy" else 0, 20, side,
                                     "1", "100")
                        event[field] = value
                        prefix = ([fill(0, 10, "sell", "2", "100")]
                                  if side == "buy" else [])
                        with self.assertRaises(LedgerError):
                            replay("1000", prefix + [event])

    def test_nonfinite_values_rejected_for_all_decimal_fields(self):
        bad_values = ("NaN", "Infinity", "-Infinity",
                      D("NaN"), D("Infinity"), D("-Infinity"))
        for bad in bad_values:
            for field in ("qty", "price"):
                with self.subTest(value=repr(bad), field=field):
                    event = fill(0, 10, "sell", "1", "100")
                    event[field] = bad
                    with self.assertRaises(LedgerError):
                        replay("1000", [event])
            for field in ("mark", "rate"):
                with self.subTest(value=repr(bad), field=field):
                    event = funding(1, 20, "settle", "100", "0.01")
                    event[field] = bad
                    with self.assertRaises(LedgerError):
                        replay("1000", [
                            fill(0, 10, "sell", "1", "100"), event
                        ])
            with self.subTest(value=repr(bad), field="initial_cash"):
                with self.assertRaises(LedgerError):
                    replay(bad, [])

    def test_binary_float_and_bool_rejected_for_all_decimal_fields(self):
        for bad in (True, False, 1.0, 0.5):
            for field in ("qty", "price"):
                with self.subTest(value=repr(bad), field=field):
                    event = fill(0, 10, "sell", "1", "100")
                    event[field] = bad
                    with self.assertRaises(LedgerError):
                        replay("1000", [event])
            for field in ("mark", "rate"):
                with self.subTest(value=repr(bad), field=field):
                    event = funding(1, 20, "settle", "100", "0.01")
                    event[field] = bad
                    with self.assertRaises(LedgerError):
                        replay("1000", [
                            fill(0, 10, "sell", "1", "100"), event
                        ])
            with self.subTest(value=repr(bad), field="initial_cash"):
                with self.assertRaises(LedgerError):
                    replay(bad, [])

    def test_invalid_ids_and_settlement_identity_rejected(self):
        for event_id in ("", None, 1, True):
            with self.subTest(event_id=repr(event_id)):
                with self.assertRaises(LedgerError):
                    replay("1000", [
                        fill(0, 10, "sell", "1", "100", event_id=event_id)
                    ])
        for settlement_id in ("", None, 1, True):
            with self.subTest(settlement_id=repr(settlement_id)):
                with self.assertRaises(LedgerError):
                    replay("1000", [
                        fill(0, 10, "sell", "1", "100"),
                        funding(1, 20, settlement_id, "100", "0.01"),
                    ])

    def test_exact_event_schema_and_enum_values_rejected(self):
        good_fill = fill(0, 10, "sell", "1", "100")
        missing = dict(good_fill)
        del missing["price"]
        extra = dict(good_fill)
        extra["account"] = "fictional"
        wrong_side = dict(good_fill)
        wrong_side["side"] = "long"
        wrong_liquidity = dict(good_fill)
        wrong_liquidity["liquidity"] = "post-only"
        wrong_type = dict(good_fill)
        wrong_type["type"] = "transfer"
        bad_funding = funding(0, 10, "settle", "100", "0.01")
        del bad_funding["rate"]
        extra_funding = funding(0, 10, "settle", "100", "0.01")
        extra_funding["qty"] = "1"
        for event in (missing, extra, wrong_side, wrong_liquidity,
                      wrong_type, bad_funding, extra_funding):
            with self.subTest(event=event), self.assertRaises(LedgerError):
                replay("1000", [event])

    def test_initial_cash_and_event_container_validation(self):
        for initial in ("-0.01", -1, "not-a-decimal", " 10", ""):
            with self.subTest(initial=initial), self.assertRaises(LedgerError):
                replay(initial, [])
        for events in ("events", b"events", {}, 42, [None], [["fill"]]):
            with self.subTest(events=repr(events)), self.assertRaises(LedgerError):
                replay("1000", events)


if __name__ == "__main__":
    unittest.main()
