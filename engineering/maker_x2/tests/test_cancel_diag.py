"""Raw cancellation categories and unrecognized reason conservation."""
import unittest
from cancel_diag import CATS, RAW_MAP, cancel_table, share


class Control(unittest.TestCase):
    def test_known_reasons(self):
        # Kills incorrect TTL, cross, admission, X1 and H2 mappings.
        reasons=["EXPIRED","POST_ONLY_REJECT","ADMISSION_REJECTED",
                 "INSUFFICIENT_CASH_OR_MARGIN","X1_ADMISSION_REJECTED",
                 "H2_SKIP","single slot occupied","gap/halt blocks entry"]
        table=cancel_table(reasons,10)
        self.assertEqual(table["cancelled"],8)
        self.assertEqual(table["filled_or_open"],2)
        self.assertEqual(table["counts"]["TTL_EXPIRED"],1)
        self.assertEqual(table["counts"]["POST_ONLY_CROSS"],1)
        self.assertEqual(table["counts"]["RISK_GUARD"],4)
        self.assertEqual(table["counts"]["X1_ADMISSION"],1)
        self.assertEqual(table["counts"]["H2_SKIP"],1)
        self.assertEqual(sum(table["counts"].values()),table["cancelled"])
        self.assertEqual(set(table["counts"]),set(CATS))

    def test_unknown_and_overflow(self):
        # Kills silently discarded OTHER and incorrect total bound.
        table=cancel_table(["UNSEEN","UNSEEN","EXPIRED"],3)
        self.assertEqual(table["counts"]["OTHER"],2)
        self.assertEqual(table["other_raw"],{"UNSEEN":2})
        self.assertEqual(sum(table["counts"].values()),3)
        with self.assertRaises(ValueError):
            cancel_table(["EXPIRED","EXPIRED"],1)

    def test_share_zero(self):
        # Kills zero division and wrong cancelled denominator.
        empty=cancel_table([],2)
        self.assertEqual(share(empty),{cat:0.0 for cat in CATS})
        full=cancel_table(["EXPIRED","POST_ONLY_REJECT"],3)
        self.assertEqual(share(full)["TTL_EXPIRED"],.5)
        self.assertEqual(share(full)["POST_ONLY_CROSS"],.5)


    def test_full_funnel(self):
        # Detects discarding signals without an intent or other terminal.
        reasons=["EXPIRED","POST_ONLY_REJECT","UNSEEN"]
        result=cancel_table(reasons,10,intents=8,unprocessed=2)
        self.assertEqual(result["signals"],10)
        self.assertEqual(result["intents"],8)
        self.assertEqual(result["unprocessed"],2)
        self.assertEqual(result["cancelled"],3)
        self.assertEqual(result["filled_or_open"],5)
        self.assertEqual(result["other_raw"],{"UNSEEN":1})
        self.assertEqual(result["cancelled"]+result["filled_or_open"]+
                         result["unprocessed"],result["total"])
        self.assertAlmostEqual(result["signal_shares"]["OTHER"],.1)
        self.assertEqual(result["stage_denoms"]["intents"],8)
        with self.assertRaises(ValueError):
            cancel_table(reasons,3,intents=2)
        with self.assertRaises(ValueError):
            cancel_table(reasons,3,unprocessed=1)

    def test_real_raw_only(self):
        # Kills fake keys replacing actual existing raw reason strings.
        expected={"EXPIRED","POST_ONLY_REJECT","ADMISSION_REJECTED",
                  "INSUFFICIENT_CASH_OR_MARGIN","X1_ADMISSION_REJECTED"}
        self.assertTrue(expected.issubset(RAW_MAP))
        self.assertEqual(RAW_MAP["pending/position occupies single slot"],
                         "RISK_GUARD")
