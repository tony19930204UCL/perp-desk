"""Causal matched-universe controls and deliberately broken sampler modes."""
import os
import sys
import random
import unittest
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import matched_signals as ms
from test_signal_features import bars as synthetic_minutes
from maker_exit_lab import X2Error


def universe(n, tf=1):
    return [{"signal_time": (i+1)*tf*60000,
             "framework_minutes": tf, "triggered": i % 19 == 0}
            for i in range(n)]


class Control(unittest.TestCase):
    def test_canonical_sampling_order_seed_without_replacement_rng(self):
        records = universe(6005)
        saved = random.getstate()
        a = ms.select_matched(records, 1, 0, 10**12)
        b = ms.select_matched(list(reversed(records)), 1, 0, 10**12)
        self.assertEqual(a, b)
        self.assertEqual(random.getstate(), saved)
        self.assertEqual(a["seed"], 20261021)
        self.assertEqual(a["requested_count"], 6000)
        self.assertEqual(a["eligible_count"], 6005)
        self.assertEqual(a["status"], "READY")
        self.assertEqual(len(a["selected_timestamps"]), 6000)
        self.assertEqual(len(set(a["selected_timestamps"])), 6000)

    def test_same_shared_sample_across_twelve_variants(self):
        records = universe(15, 15)
        standard = ms.select_matched(records, 15, 0, 10**12, count=6)
        results = [(k, ttl, exit_kind,
                    ms.select_matched(records, 15, 0, 10**12, count=6))
                   for k in (".25", ".50") for ttl in (2, 5)
                   for exit_kind in ("X1", "X2", "X3")]
        self.assertEqual(len(results), 12)
        self.assertTrue(all(s == standard for _, _, _, s in results))
        self.assertEqual(standard["seed"], 20261035)

    def test_insufficient_explicit_no_replacement_no_clipping(self):
        result = ms.select_matched(universe(3), 1, 0, 10**12, count=4)
        self.assertEqual(result["status"], "INSUFFICIENT_UNIVERSE")
        self.assertEqual(result["eligible_count"], 3)
        self.assertEqual(result["requested_count"], 4)
        self.assertEqual(result["selected_timestamps"], [])

    def test_duplicate_rejected_and_half_open_interval(self):
        with self.assertRaises(X2Error):
            ms.select_matched(universe(2)+universe(1), 1, 0, 10**12, count=2)
        selected = ms.select_matched(universe(5), 1, 2*60000, 5*60000, count=3)
        self.assertEqual(selected["selected_timestamps"],
                         [2*60000, 3*60000, 4*60000])

    def test_universe_independent_of_signal_trigger_and_future(self):
        data = universe(10)
        a = ms.select_matched(data, 1, 0, 10**12, count=5)
        altered = [dict(r, triggered=not r["triggered"],
                        future_profit="fictional-unobserved") for r in data]
        b = ms.select_matched(altered, 1, 0, 10**12, count=5)
        self.assertEqual(a, b)


    def test_sample_completed_real_1m_tail_trigger_independence(self):
        data = synthetic_minutes(302, spike=False)
        start, end = 290*60000, 301*60000
        result = ms.sample_completed(data, 1, start, end, count=5)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["eligible_count"], 11)
        self.assertEqual(len(result["selected_timestamps"]), 5)
        # A future suffix and a current signal trigger outside the fixed
        # interval must not alter the eligible historical universe.
        suffix = [dict(b, ts=(302+i)*60000, quote_volume="9999")
                  for i,b in enumerate(synthetic_minutes(3))]
        appended = ms.sample_completed(data+suffix, 1, start, end, count=5)
        self.assertEqual(result, appended)
        self.assertEqual(
            ms.sample_completed(data, 1, 300*60000, 302*60000, count=2)
              ["eligible_count"], 2)

    def test_sample_completed_real_15m_and_shared_variants(self):
        # Complete UTC buckets: non-triggered closed decisions remain in
        # the matched universe; no future execution bars are required.
        data = synthetic_minutes(15*294)
        start, end = 290*15*60000, 294*15*60000
        result = ms.sample_completed(data, 15, start, end, count=3)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["eligible_count"], 4)
        self.assertEqual(result["seed"], 20261035)
        for k in (".25", ".50"):
            for ttl in (2,5):
                for exit_kind in ("X1","X2","X3"):
                    self.assertEqual(
                        ms.sample_completed(data, 15, start, end, count=3),
                        result, (k, ttl, exit_kind))


class Mutation(unittest.TestCase):
    def mut_replacement(self):
        original = ms.select_matched
        def broken(records, tf, start, end, count=ms.DEFAULT_COUNT):
            result = original(records, tf, start, end, count=count)
            if result["status"] == "READY":
                result["selected_timestamps"] = (
                    [result["selected_timestamps"][0]] * count)
            return result
        with patch.object(ms, "select_matched", broken):
            Control("test_canonical_sampling_order_seed_without_replacement_rng").debug()

    def mut_resample(self):
        original = ms.select_matched
        invocations = [0]
        def broken(records, tf, start, end, count=ms.DEFAULT_COUNT):
            result = original(records, tf, start, end, count=count)
            invocations[0] += 1
            if result["status"] == "READY" and invocations[0] % 2 == 0:
                # The returned seed stays truthful to the advertised
                # contract, but one repeated invocation actually
                # resamples its selected times from the same universe.
                all_times = sorted(r["signal_time"] for r in records)
                replacement = next(ts for ts in all_times
                                   if ts not in result["selected_timestamps"])
                result["selected_timestamps"][0] = replacement
                result["selected_timestamps"].sort()
            return result
        with patch.object(ms, "select_matched", broken):
            Control("test_same_shared_sample_across_twelve_variants").debug()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv)>1 else "control"
    names = {"mut-replacement":"mut_replacement", "mut-resample":"mut_resample"}
    suite = (unittest.defaultTestLoader.loadTestsFromTestCase(Control)
             if mode == "control" else unittest.TestSuite([Mutation(names[mode])]))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
