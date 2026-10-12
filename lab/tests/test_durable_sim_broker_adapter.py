"""Restart and crash-boundary tests using isolated real SQLite databases."""
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from execution_interface import Intent
from execution_state_store import ExecutionStateStore
from durable_sim_broker_adapter import DurableSimBrokerAdapter
from sim_broker_adapter import SimBrokerAdapter
from sim_broker import SimBroker, InstrumentSettings, ExecutionModel, RiskContract


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        self.brokers = []
        self.stores = []
        self.risk = RiskContract(True, 'ARTIFICIAL', D('30'), D('40'), D('10'),
                                 2, D('60'), True)

    def pair(self, name='a', *, approved=True, hook=None):
        settings = InstrumentSettings('TEST', D('.0002'), D('.0005'),
                                      D('.1'), D('.1'), D('5'), D('100'))
        risk = self.risk if approved else RiskContract()
        broker = SimBroker(self.p / (name + '.broker.sqlite'),
                           initial_cash=D('100'), instruments=[settings],
                           execution=ExecutionModel(10, 2, None), risk=risk,
                           version_id='artificial', forward_start=100)
        store = ExecutionStateStore(self.p / (name + '.store.sqlite'),
                                    namespace='artificial:' + name)
        self.brokers.append(broker)
        self.stores.append(store)
        return DurableSimBrokerAdapter(broker, store, fault_hook=hook)

    def reopen(self, adapter, name='a'):
        adapter.broker.close()
        adapter.store.close()
        return self.pair(name)

    def intent(self, name='one', **kwargs):
        return replace(Intent(name, 'TEST', 'BUY', 'market', D('1'),
                              False, D('90'), False, 100), **kwargs)

    def prime(self, a):
        a.broker.on_event(dict(type='mark', event_id='mark', symbol='TEST',
                               ts=100, price='100'))
        a.broker.on_event(dict(type='funding_status', event_id='fund',
                               symbol='TEST', ts=100, complete=True,
                               valid_until_ts=1000000))

    def book(self, a, suffix='book', asks=None, bids=None):
        a.broker.on_event(dict(type='book', event_id=suffix, symbol='TEST', ts=111,
                               asks=asks or [['100', '10']],
                               bids=bids or [['99', '10']]))

    def spy(self, a):
        original = a.broker.submit
        calls = []
        def counted(intent):
            calls.append(intent)
            return original(intent)
        a.broker.submit = counted
        return calls

    def test_reservation_visible_before_send(self):
        a = self.pair()
        self.prime(a)
        i = self.intent()
        original = a.broker.submit
        def inspect(bi):
            second = ExecutionStateStore(self.p / 'a.store.sqlite',
                                         namespace='artificial:a')
            try:
                self.assertEqual(second.load_intent(i.intent_id), i)
                self.assertEqual(second.load_state(i.intent_id).state, 'NEW')
            finally:
                second.close()
            return original(bi)
        a.broker.submit = inspect
        self.assertEqual(a.submit(i).state, 'ACCEPTED')

    def test_reopen_duplicate_zero_sends(self):
        a = self.pair()
        self.prime(a)
        i = self.intent()
        a.submit(i)
        a = self.reopen(a)
        calls = self.spy(a)
        self.assertEqual(a.submit(i).state, 'ACCEPTED')
        self.assertEqual(len(calls), 0)

    def test_reservation_only_never_sends(self):
        a = self.pair()
        i = self.intent()
        a.store.reserve(i)
        a = self.reopen(a)
        calls = self.spy(a)
        for _ in range(3):
            self.assertEqual(a.submit(i).state, 'UNKNOWN_OUTCOME')
            self.assertEqual(a.query(i.intent_id).state, 'UNKNOWN_OUTCOME')
        self.assertEqual(len(calls), 0)

    def test_committed_broker_before_checkpoint(self):
        a = self.pair()
        self.prime(a)
        i = self.intent()
        a.store.reserve(i)
        a.broker.submit(a._convert(i))
        a = self.reopen(a)
        calls = self.spy(a)
        self.assertEqual(a.query(i.intent_id).state, 'ACCEPTED')
        self.assertEqual(a.submit(i).state, 'ACCEPTED')
        self.assertFalse(calls)

    def test_exception_after_broker_commit(self):
        a = self.pair()
        self.prime(a)
        i = self.intent()
        original = a.broker.submit
        def fail(bi):
            original(bi)
            raise RuntimeError('response lost')
        a.broker.submit = fail
        with self.assertRaisesRegex(RuntimeError, 'response lost'):
            a.submit(i)
        self.assertEqual(a.store.load_state(i.intent_id).state, 'UNKNOWN_OUTCOME')
        a = self.reopen(a)
        self.assertEqual(a.query(i.intent_id).state, 'ACCEPTED')

    def test_conflicting_intent(self):
        a = self.pair()
        self.prime(a)
        a.submit(self.intent())
        with self.assertRaises(ValueError):
            a.submit(self.intent(qty=D('2')))

    def test_broker_intent_conflict_read_only(self):
        a = self.pair()
        self.prime(a)
        i = self.intent()
        a.store.reserve(i)
        a.broker.submit(a._convert(self.intent(qty=D('2'))))
        calls = self.spy(a)
        with self.assertRaisesRegex(ValueError, 'conflicting broker'):
            a.submit(i)
        self.assertFalse(calls)

    def test_accepted_and_rejected_recovery(self):
        for approved in (True, False):
            name = 'case' + str(approved)
            a = self.pair(name, approved=approved)
            self.prime(a)
            i = self.intent()
            target = 'ACCEPTED' if approved else 'REJECTED'
            self.assertEqual(a.submit(i).state, target)
            a = self.reopen(a, name)
            for _ in range(3):
                self.assertEqual(a.query(i.intent_id).state, target)

    def test_partial_and_final_fees_exact(self):
        a = self.pair()
        self.prime(a)
        i = self.intent()
        a.submit(i)
        self.book(a, asks=[['100', '.4'], ['100.1', '.6']])
        a = self.reopen(a)
        s = a.query(i.intent_id)
        self.assertEqual(s.state, 'FILLED')
        self.assertEqual(s.filled_qty, D('1'))
        self.assertEqual(s.fees, sum((D(f['fee']) for f in a.broker.fills), D(0)))
        self.assertEqual([e.fill_id for e in s.events.values() if e.fill_id],
                         [f['fill_id'] for f in a.broker.fills])
        self.assertEqual(len(s.events), len(a.query(i.intent_id).events))

    def test_cancel_and_reopen(self):
        a = self.pair()
        self.prime(a)
        i = self.intent()
        a.submit(i)
        self.assertEqual(a.cancel(i.intent_id, 101).state, 'CANCELED')
        a = self.reopen(a)
        self.assertEqual(a.cancel(i.intent_id, 102).state, 'CANCELED')

    def test_expiry(self):
        a = self.pair()
        self.prime(a)
        i = self.intent(expires_ts=110)
        a.submit(i)
        self.book(a)
        a = self.reopen(a)
        self.assertEqual(a.query(i.intent_id).state, 'EXPIRED')

    def test_fill_cancel_race(self):
        a = self.pair()
        self.prime(a)
        i = self.intent()
        a.submit(i)
        self.book(a)
        self.assertEqual(a.cancel(i.intent_id, 112).state, 'FILLED')
        a = self.reopen(a)
        self.assertEqual(a.query(i.intent_id).state, 'FILLED')

    def test_fault_hook_unknown_then_reconcile(self):
        a = self.pair(hook=lambda _: True)
        self.prime(a)
        i = self.intent()
        self.assertEqual(a.submit(i).state, 'UNKNOWN_OUTCOME')
        self.assertEqual(a.store.load_state(i.intent_id).state, 'UNKNOWN_OUTCOME')
        a = self.reopen(a)
        self.assertEqual(a.query(i.intent_id).state, 'ACCEPTED')

    def test_missing_unreserved_and_cancel(self):
        a = self.pair()
        with self.assertRaises(KeyError):
            a.query('missing')
        with self.assertRaises(KeyError):
            a.cancel('missing', 105)
        i = self.intent()
        a.store.reserve(i)
        self.assertEqual(a.cancel(i.intent_id, 105).state, 'UNKNOWN_OUTCOME')
        self.assertFalse(a.broker.orders)

    def test_unapproved_and_protective_exit(self):
        a = self.pair()
        self.prime(a)
        i = self.intent()
        with self.assertRaises(ValueError):
            a.submit(self.intent('invalid', protective=True))
        a.submit(i)
        self.book(a)
        a.broker.risk = RiskContract()
        blocked = self.intent('blocked')
        self.assertEqual(a.submit(blocked).state, 'REJECTED')
        exit_i = Intent('exit', 'TEST', 'SELL', 'market', D('1'), True,
                        None, True, 112)
        self.assertEqual(a.submit(exit_i).state, 'ACCEPTED')

    def test_differential_normal_fills_and_cash(self):
        a = self.pair('durable')
        b = self.pair('original')
        self.prime(a)
        self.prime(b)
        i = self.intent()
        regular = SimBrokerAdapter(b.broker)
        a.submit(i)
        regular.submit(i)
        self.book(a)
        self.book(b)
        self.assertEqual(a.query(i.intent_id).state, 'FILLED')
        self.assertEqual(regular.query(i.intent_id).state, 'FILLED')
        self.assertTrue(a.broker.fills)
        self.assertEqual(a.broker.fills, b.broker.fills)
        self.assertEqual(a.broker.ledger, b.broker.ledger)
        self.assertEqual(a.broker.cash, b.broker.cash)
