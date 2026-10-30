"""Regression for R4 high-precision cash reconciliation."""
import unittest
from decimal import Decimal, localcontext, ROUND_DOWN

from accounting import LedgerError, _check_cash, replay


class CashContext(unittest.TestCase):
    def test_funding_52_digits(self):
        events = [
            {"id": "e0", "seq": 0, "ts": 0, "type": "fill",
             "side": "sell", "qty": "1", "price": "100",
             "liquidity": "maker"},
        ]
        rates = (
            "0.00012345678901234567890123456789012345678901234567890123",
            "-0.00000001234567890123456789012345678901234567890123456789",
            "0.00000000000098765432109876543210987654321098765432109876",
        )
        for n, rate in enumerate(rates, 1):
            events.append({
                "id": "e" + str(n), "seq": n, "ts": n * 1000,
                "type": "funding", "settlement_id": "s" + str(n),
                "mark": "101.23456789", "rate": rate,
            })
        with localcontext() as ctx:
            ctx.prec = 8
            ctx.rounding = ROUND_DOWN
            result = replay("150", events)
        self.assertEqual(result["event_count"], 4)
        self.assertEqual(len(result["settlements"]), 3)
        self.assertEqual(result["fees_paid"], Decimal("0.02"))
        self.assertLessEqual(
            abs(result["cash"] - (
                result["initial_cash"] + result["realized_gross"]
                - result["fees_paid"] - result["funding_cost"])),
            Decimal("1E-30"))
        self.assertEqual(result["remaining_signed_qty"], Decimal("-1"))

    def test_tampered_cash_rejected(self):
        with self.assertRaisesRegex(LedgerError, "cash reconciliation"):
            _check_cash(Decimal("150.000001"), Decimal("150"))

    def test_dust_is_bounded(self):
        _check_cash(Decimal("150") + Decimal("1E-30"), Decimal("150"))
        with self.assertRaises(LedgerError):
            _check_cash(Decimal("150") + Decimal("1.0001E-30"),
                        Decimal("150"))


if __name__ == "__main__":
    unittest.main()
