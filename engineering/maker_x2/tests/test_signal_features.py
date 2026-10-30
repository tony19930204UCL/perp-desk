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
        suffix = [dict(item, ts=(300+j)*M, quote_volume="5000",
                       base_volume="1") for j, item in enumerate(bars(2))]
        extended = sf.eligible_decisions(original + suffix, 1)
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


def independent_std(values):
    """Test-only ddof1 reference without invoking production sample_std."""
    with localcontext() as ctx:
        ctx.prec = 50
        mean = sum(values, D("0")) / D(len(values))
        squared = sum(((x-mean)*(x-mean) for x in values), D("0"))
        return (squared / D(len(values)-1)).sqrt()


def framework_fixture(prices):
    """Each 15m UTC bucket has 15 complete fictional identical-price minutes."""
    out = []
    for index, price in enumerate(prices):
        for minute in range(15):
            out.append({
                "ts": (index*15+minute)*M, "open": str(price),
                "high": str(price+D(".4")), "low": str(price-D(".4")),
                "close": str(price), "base_volume": "2",
                "quote_volume": str(price*2), "closed": True,
            })
    return out


class StrategyApiControls(unittest.TestCase):
    def test_s1_real_api_below_above_and_controlled_exact_equality(self):
        low = sf.eligible_decisions(bars(300), 1)[-1]
        high_bars = bars(300, spike=True)
        high = sf.eligible_decisions(high_bars, 1)[-1]
        self.assertLessEqual(low["z"], D("2.5"))
        self.assertFalse(low["triggered"])
        self.assertGreater(high["z"], D("2.5"))
        self.assertTrue(high["triggered"])
        self.assertEqual(sf.signal_records(high_bars, 1)[-1]["signal_id"],
                         high["signal_id"])
        # Unmocked 50-digit finite prices do not guarantee irrational
        # sqrt(6) yields literal z==2.5. Inject ONLY the comparison
        # threshold, keeping actual 1m bars, returns, r6, sigma, z,
        # ATR/VWAP and the production > predicate executing normally.
        factory = sf.D
        def comparison_threshold(value):
            return high["z"] if value == "2.5" else factory(value)
        with patch.object(sf, "D", side_effect=comparison_threshold):
            equality = sf.eligible_decisions(high_bars, 1)[-1]
        self.assertEqual(equality["z"], high["z"])
        self.assertFalse(equality["triggered"])
        self.assertEqual(equality["signal_time"], 300*M)

    def test_s2_real_api_positive_close_equality_and_volatility_gate(self):
        previous = [D("100")+ (D(".05") if j%2 else D("-.05"))
                    for j in range(277)] + [D("100")]*13
        self.assertEqual(len(previous), 290)
        positive = sf.eligible_decisions(
            framework_fixture(previous+[D("98")]), 15)[-1]
        self.assertEqual(positive["signal_type"], "S2")
        self.assertTrue(positive["triggered"])
        self.assertEqual(positive["std12"], D("0"))
        self.assertLessEqual(positive["std12"],
                             positive["sigma288"]*D(".5"))
        self.assertEqual(positive["previous_l12"], D("99.6"))
        self.assertEqual(positive["atr14"], D(".8"))
        self.assertEqual(positive["vwap60"], D("99.5"))
        self.assertEqual(positive["signal_time"], 291*15*M)
        self.assertEqual(positive["final_minute_index"], 291*15-1)
        self.assertEqual(
            sf.signal_records(framework_fixture(previous+[D("98")]), 15)[-1]
              ["signal_id"], positive["signal_id"])
        with localcontext() as ctx:
            ctx.prec = 50
            reference_returns = [
                previous[j]/previous[j-1]-1 for j in range(2, 290)]
        self.assertEqual(len(reference_returns), 288)
        self.assertEqual(positive["sigma288"],
                         independent_std(reference_returns))
        self.assertEqual(positive["std12"],
                         independent_std(reference_returns[-12:]))
        equality = sf.eligible_decisions(
            framework_fixture(previous+[D("99.6")]), 15)[-1]
        self.assertEqual(equality["signal_close"], equality["previous_l12"])
        self.assertFalse(equality["triggered"])
        volatile = previous[:277] + [
            D("100") if i%2 else D("102") for i in range(13)]
        blocked = sf.eligible_decisions(
            framework_fixture(volatile+[D("98")]), 15)[-1]
        self.assertLess(blocked["signal_close"], blocked["previous_l12"])
        self.assertGreater(blocked["std12"],
                           blocked["sigma288"]*D(".5"))
        self.assertFalse(blocked["triggered"])


class Mutation(unittest.TestCase):
    def mut_lookahead(self):
        original = sf.eligible_decisions
        original_vwap = sf.vwap60

        def leaked(values, framework):
            # A deliberately invalid future read: latest input minute's
            # quote/base ratio contaminates EACH historical VWAP feature.
            last = values[-1]
            future = D(last["quote_volume"]) / D(last["base_volume"])
            def future_vwap(window):
                return original_vwap(window) + future
            with patch.object(sf, "vwap60", future_vwap):
                return original(values, framework)

        with patch.object(sf, "eligible_decisions", leaked):
            Control("test_prefix_future_invariance_and_tail_decision").debug()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv)>1 else "control"
    suite = (unittest.TestSuite([
                 unittest.defaultTestLoader.loadTestsFromTestCase(Control),
                 unittest.defaultTestLoader.loadTestsFromTestCase(StrategyApiControls)])
             if mode == "control" else unittest.TestSuite([Mutation("mut_lookahead")]))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
