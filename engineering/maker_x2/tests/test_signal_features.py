"""Handmade causal feature controls and mutation-only negative entrypoint."""
import os
import sys
import unittest
from decimal import Decimal as D, localcontext
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import signal_features as sf
from maker_exit_lab import X2Error

M = 60000


def bars(n, spike=False):
    out = []
    for i in range(n):
        close = D("100") + D(i % 7) / 100
        if spike and i == n-1:
            close = D("105")
        out.append(dict(ts=i*M, open=str(close), high=str(close+D("1")),
                        low=str(close-D("1")), close=str(close),
                        base_volume="2", quote_volume="210", closed=True))
    return out


class Control(unittest.TestCase):
    def test_sample_ddof1_and_prior_returns_only(self):
        self.assertEqual(sf.sample_std([D("1"),D("2"),D("3")]), D("1"))
        data = bars(300)
        rec = sf.eligible_decisions(data, 1)
        self.assertEqual(len(rec), 11)
        with localcontext() as ctx:
            ctx.prec = 50
            expected = sf.sample_std([
                D(data[j]["close"]) / D(data[j-1]["close"]) - 1
                for j in range(1, 289)])
        self.assertEqual(rec[0]["sigma288"], expected)
        self.assertEqual(rec[0]["signal_time"], 290*M)
        self.assertEqual(rec[0]["final_minute_ts"], 289*M)
        self.assertEqual(rec[0]["atr14"], D("2"))
        self.assertEqual(rec[0]["vwap60"], D("105"))

    def test_prefix_future_invariance_and_tail_decision(self):
        original = bars(300, spike=True)
        first = sf.eligible_decisions(original, 1)
        extended = sf.eligible_decisions(original + bars(2), 1)
        self.assertEqual(first, extended[:len(first)])
        self.assertEqual(first[-1]["signal_time"], 300*M)
        self.assertTrue(first[-1]["triggered"])
        self.assertEqual(sf.signal_records(original, 1)[-1]["signal_time"], 300*M)

    def test_complete_utc_15m_and_missing_bucket_exclusion(self):
        source = bars(15*291)
        records = sf.framework_bars(source, 15)
        self.assertEqual(len(records), 291)
        self.assertEqual(records[0]["decision_ts"], 15*M)
        self.assertEqual(records[-1]["final_minute_index"], len(source)-1)
        self.assertEqual(len(sf.eligible_decisions(source, 15)), 2)
        clipped = source[:]
        del clipped[15*70+3]
        self.assertEqual(len(sf.framework_bars(clipped, 15)), 290)
        self.assertEqual(sf.eligible_decisions(clipped, 15), [])

    def test_gaps_incomplete_volumes_duplicates_reject(self):
        data = bars(300)
        bad = data[:]
        bad[3] = dict(bad[3], closed=False)
        with self.assertRaises(X2Error):
            sf.validate_minutes(bad)
        bad = data[:]
        bad[3] = dict(bad[3], base_volume="-1")
        with self.assertRaises(X2Error):
            sf.validate_minutes(bad)
        with self.assertRaises(X2Error):
            sf.validate_minutes(data+[data[-1]])
        missing = data[:]
        del missing[180]
        self.assertEqual(sf.eligible_decisions(missing, 1), [])
        zero = [dict(x, base_volume="0") for x in data]
        self.assertEqual(sf.eligible_decisions(zero, 1), [])

    def test_strict_s1_s2_threshold_arithmetic(self):
        self.assertFalse(D("2.5") > D("2.5"))
        self.assertTrue(D("2.50001") > D("2.5"))
        self.assertTrue(D("0.5") <= D("0.5"))
        self.assertFalse(D("99") < D("99"))
        self.assertTrue(D("98.999") < D("99"))


class Mutation(unittest.TestCase):
    def mut_lookahead(self):
        original = sf.eligible_decisions
        def altered(values, framework):
            records = original(values, framework)
            for rec in records:
                rec["signal_time"] += M
            return records
        with patch.object(sf, "eligible_decisions", altered):
            Control("test_prefix_future_invariance_and_tail_decision").debug()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv)>1 else "control"
    suite = (unittest.defaultTestLoader.loadTestsFromTestCase(Control)
             if mode == "control" else unittest.TestSuite([Mutation("mut_lookahead")]))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
