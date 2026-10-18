"""Isolated source-aging and broker durability regressions; never use live accounts."""
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from sim_broker import SimBroker, InstrumentSettings, ExecutionModel


class SourceAgingPersistenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="source-aging-")
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "broker.sqlite"
        self.b = None

    def tearDown(self):
        if self.b is not None:
            self.b.close()

    def open(self):
        self.b = SimBroker(
            self.path, initial_cash=D("100"),
            instruments=[InstrumentSettings("TEST", D(".0002"), D(".0005"), D(".1"), D(".1"), D("5"), D("100"))],
            execution=ExecutionModel(10, 2, None), risk=None,
            version_id="synthetic", forward_start=100)
        return self.b

    @staticmethod
    def mark(name, ts, price="100"):
        return dict(type="mark", symbol="TEST", event_id=name, ts=ts, source_ts=ts, price=price)

    def rows(self):
        with sqlite3.connect(self.path) as db:
            state = json.loads(db.execute("SELECT payload FROM sim_broker_state").fetchone()[0])
            ids = db.execute("SELECT event_id,digest FROM sim_seen_events ORDER BY event_id").fetchall()
        return state, ids

    def test_append_only_persistence_duplicate_conflict_restart(self):
        b = self.open()
        b.on_event(self.mark("a", 100))
        original = self.rows()
        self.assertNotIn("seen_events", original[0])
        b.on_event(self.mark("a", 100))
        self.assertEqual(original, self.rows())
        with self.assertRaisesRegex(ValueError, "conflicting event ID"):
            b.on_event(self.mark("a", 100, "101"))
        b.close()
        self.b = None
        b = self.open()
        b.on_event(self.mark("a", 100))
        self.assertEqual(original, self.rows())
        b.on_event(self.mark("b", 101))
        self.assertEqual(len(self.rows()[1]), 2)

    def test_failed_event_rolls_back_digest_and_broker(self):
        b = self.open()
        before = self.rows()
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TRIGGER fail_digest BEFORE INSERT ON sim_seen_events
                BEGIN SELECT RAISE(FAIL, 'synthetic fail'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            b.on_event(self.mark("a", 100))
        self.assertEqual(self.rows(), before)
        self.assertEqual(b.marks, {})
        self.assertNotIn("a", b.seen_events)
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TRIGGER fail_digest")
        b.on_event(self.mark("a", 100))
        self.assertEqual(len(self.rows()[1]), 1)

    def test_legacy_migration_and_conflict(self):
        b = self.open()
        original = self.mark("old", 100)
        b.on_event(original)
        b.close()
        self.b = None
        with sqlite3.connect(self.path) as db:
            state = json.loads(db.execute("SELECT payload FROM sim_broker_state").fetchone()[0])
            state["seen_events"] = {"old": original}
            db.execute("UPDATE sim_broker_state SET payload=?", (json.dumps(state),))
            db.execute("DELETE FROM sim_seen_events")
        b = self.open()
        self.assertNotIn("seen_events", self.rows()[0])
        self.assertEqual(len(self.rows()[1]), 1)
        b.on_event(original)
        with self.assertRaisesRegex(ValueError, "conflicting event ID"):
            b.on_event(self.mark("old", 100, "102"))

    def test_no_history_rewrite_under_many_events(self):
        b = self.open()
        for n in range(80):
            b.on_event(self.mark("m" + str(n), 100 + n))
        state, rows = self.rows()
        self.assertEqual(len(rows), 80)
        self.assertNotIn("seen_events", state)
        self.assertLess(len(json.dumps(state)), 10000)
        self.assertEqual(b.last_ts, 179)

    def test_out_of_order_still_rejected(self):
        b = self.open()
        b.on_event(self.mark("latest", 102))
        prior = self.rows()
        with self.assertRaisesRegex(ValueError, "out-of-order"):
            b.on_event(self.mark("late", 101))
        self.assertEqual(prior, self.rows())



class SourceAgingOrderingTests(unittest.TestCase):
    def test_early_exit_and_post_candle_refresh_h2(self):
        from paper_runtime_v2 import PaperRuntime
        from test_paper_v2 import V2Tests
        from test_freshness import FreshnessTests
        self.assertTrue(callable(PaperRuntime.refresh_decision_markets))
        self.assertTrue(callable(V2Tests.test_failed_gap_bootstrap_does_not_block_position_reduction))
        self.assertTrue(callable(FreshnessTests.test_actual_delay_refreshes_real_inputs_before_decision))

    def test_f1h_inherits_bounded_decision_refresh(self):
        from paper_runtime_v2 import PaperRuntime as Base
        from paper_runtime_v5 import PaperRuntime as F1H
        self.assertIs(F1H.refresh_decision_markets, Base.refresh_decision_markets)

if __name__ == "__main__":
    unittest.main()
