"""Reusable adapter contract assertions for future implementations."""
import unittest
from decimal import Decimal as D
from execution_interface import Event, OrderState, IdempotencyLedger, Intent, client_order_id

class ExecutionContractMixin:
    adapter_factory = None

    def test_submit_accepted(self):
        adapter, intent = self.adapter_factory()
        self.assertEqual(adapter.submit(intent).state, 'ACCEPTED')

    def test_same_intent_one_order(self):
        adapter, intent = self.adapter_factory()
        adapter.submit(intent)
        adapter.submit(intent)
        self.assertEqual(len(adapter.broker.orders), 1)

    def test_reject(self):
        adapter, intent = self.adapter_factory(approved=False)
        self.assertEqual(adapter.submit(intent).state, 'REJECTED')

    def test_query_accepted(self):
        adapter, intent = self.adapter_factory()
        adapter.submit(intent)
        self.assertEqual(adapter.query(intent.intent_id).state, 'ACCEPTED')

    def test_cancel(self):
        adapter, intent = self.adapter_factory()
        adapter.submit(intent)
        self.assertEqual(adapter.cancel(intent.intent_id, 101).state, 'CANCELED')

    def test_cancel_idempotent(self):
        adapter, intent = self.adapter_factory()
        adapter.submit(intent)
        adapter.cancel(intent.intent_id, 101)
        self.assertEqual(adapter.cancel(intent.intent_id, 102).state, 'CANCELED')

    def test_conflicting_intent(self):
        from dataclasses import replace
        adapter, intent = self.adapter_factory()
        adapter.submit(intent)
        with self.assertRaises(ValueError):
            adapter.submit(replace(intent, qty=D('2')))

class StateContractTests(unittest.TestCase):
    def setUp(self):
        self.s = OrderState('x', D('2'))

    def event(self, eid, kind, qty='0', price=None, fee='0'):
        return Event(eid, 'x', kind, D(qty), D(price) if price else None, D(fee))

    def test_new_accepted(self):
        self.s.apply(self.event('a', 'ACCEPTED'))
        self.assertEqual(self.s.state, 'ACCEPTED')

    def test_new_rejected(self):
        self.s.apply(self.event('a', 'REJECTED'))
        self.assertEqual(self.s.state, 'REJECTED')

    def test_partial_then_fill(self):
        self.s.apply(self.event('a', 'ACCEPTED'))
        self.s.apply(self.event('b', 'PARTIALLY_FILLED', '1', '100', '.1'))
        self.s.apply(self.event('c', 'FILLED', '1', '101', '.2'))
        self.assertEqual((self.s.filled_qty, self.s.fees), (D('2'), D('.3')))

    def test_duplicate_event(self):
        self.s.apply(self.event('a', 'ACCEPTED'))
        self.assertFalse(self.s.apply(self.event('a', 'ACCEPTED')))

    def test_conflicting_event(self):
        self.s.apply(self.event('a', 'ACCEPTED'))
        with self.assertRaises(ValueError):
            self.s.apply(self.event('a', 'REJECTED'))

    def test_out_of_order(self):
        with self.assertRaises(ValueError):
            self.s.apply(self.event('b', 'FILLED', '2', '100'))

    def test_terminal_immutable(self):
        self.s.apply(self.event('a', 'REJECTED'))
        with self.assertRaises(ValueError):
            self.s.apply(self.event('b', 'ACCEPTED'))

    def test_unknown_to_accepted(self):
        self.s.apply(self.event('a', 'UNKNOWN_OUTCOME'))
        self.s.apply(self.event('b', 'ACCEPTED'))
        self.assertEqual(self.s.state, 'ACCEPTED')

    def test_unknown_to_rejected(self):
        self.s.apply(self.event('a', 'UNKNOWN_OUTCOME'))
        self.s.apply(self.event('b', 'REJECTED'))
        self.assertEqual(self.s.state, 'REJECTED')

    def test_unknown_to_filled(self):
        self.s.apply(self.event('a', 'UNKNOWN_OUTCOME'))
        self.s.apply(self.event('b', 'FILLED', '2', '100'))
        self.assertEqual(self.s.state, 'FILLED')

    def test_unknown_to_canceled(self):
        self.s.apply(self.event('a', 'UNKNOWN_OUTCOME'))
        self.s.apply(self.event('b', 'CANCELED'))
        self.assertEqual(self.s.state, 'CANCELED')

    def test_unknown_to_expired(self):
        self.s.apply(self.event('a', 'UNKNOWN_OUTCOME'))
        self.s.apply(self.event('b', 'EXPIRED'))
        self.assertEqual(self.s.state, 'EXPIRED')

    def test_partial_must_be_smaller(self):
        self.s.apply(self.event('a', 'ACCEPTED'))
        with self.assertRaises(ValueError):
            self.s.apply(self.event('b', 'PARTIALLY_FILLED', '2', '100'))

    def test_no_overfill(self):
        self.s.apply(self.event('a', 'ACCEPTED'))
        with self.assertRaises(ValueError):
            self.s.apply(self.event('b', 'FILLED', '3', '100'))

    def test_invalid_price(self):
        self.s.apply(self.event('a', 'ACCEPTED'))
        with self.assertRaises(ValueError):
            self.s.apply(self.event('b', 'FILLED', '2', '0'))

    def test_client_id_deterministic(self):
        self.assertEqual(client_order_id('x'), client_order_id('x'))

    def test_client_id_attempt(self):
        self.assertNotEqual(client_order_id('x', 0), client_order_id('x', 1))

    def test_client_id_safe(self):
        import re
        self.assertRegex(client_order_id('any characters !'), r'^[A-Za-z0-9_-]{1,36}$')

    def test_ledger_idempotent(self):
        ledger = IdempotencyLedger()
        intent = Intent('x', 'TEST', 'BUY', 'market', D('1'), False, D('90'), False, 100)
        self.assertTrue(ledger.reserve(intent))
        self.assertFalse(ledger.reserve(intent))

    def test_ledger_snapshot_recovery(self):
        ledger = IdempotencyLedger()
        intent = Intent('x', 'TEST', 'BUY', 'market', D('1'), False, D('90'), False, 100)
        ledger.reserve(intent)
        self.assertFalse(IdempotencyLedger(ledger.snapshot()).reserve(intent))
