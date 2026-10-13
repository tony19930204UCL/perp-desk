"""Durable, offline SimBroker adapter; no cross-database atomicity is assumed.

A reservation is an irreversible permission for *one* sender only.  On restart,
a missing broker order is ambiguous and must never be sent again.
"""
from dataclasses import asdict
from decimal import Decimal
import json

from execution_interface import Event, OrderState, TERMINAL
from sim_broker_adapter import SimBrokerAdapter


class DurableSimBrokerAdapter(SimBrokerAdapter):
    def __init__(self, broker, store, *, fault_hook=None):
        super().__init__(broker, fault_hook=fault_hook)
        self.store = store  # Caller owns both SQLite connections.

    def _restore(self, intent_id):
        intent = self.store.load_intent(intent_id)
        if intent is None:
            raise KeyError('unreserved intent: ' + str(intent_id))
        state = self.store.load_state(intent_id)
        if state is None:  # Crash between reserve() and initial checkpoint.
            state = OrderState(intent_id, intent.qty)
            self.store.save_state(state)
        self._intents[intent_id] = intent
        self.states[intent_id] = state
        return intent, state

    def _apply(self, state, event):
        if state.apply(event):
            self.store.save_state(state)

    def _unknown(self, state):
        if state.state not in TERMINAL and state.state != 'UNKNOWN_OUTCOME':
            eid = 'unknown:' + state.intent_id
            if eid in state.events:
                eid += ':' + str(len(state.events))
            self._apply(state, Event(eid, state.intent_id, 'UNKNOWN_OUTCOME'))
        return state

    def _broker_order(self, intent):
        """Check identity/content without using the broker's mutating submit."""
        order = self.broker.orders.get('order:' + intent.intent_id)
        if order is None:
            return None
        converted = asdict(self._convert(intent))
        stored = order.get('intent')
        if not isinstance(stored, dict):
            raise ValueError('invalid broker intent record')
        stored = dict(stored)
        # SimBroker.submit explicitly defaults these fields for older records.
        stored.setdefault('risk_limited', False)
        stored.setdefault('risk_quantity_step', None)
        stored.setdefault('maker_queue_from_arrival', False)
        canonical = lambda value: json.dumps(value, default=str, sort_keys=True)
        if (order.get('order_id') != 'order:' + intent.intent_id
                or order.get('intent_id') != intent.intent_id
                or canonical(stored) != canonical(converted)):
            raise ValueError('conflicting broker intent ID')
        return order

    def _reconcile(self, intent, state):
        ident = intent.intent_id
        order = self._broker_order(intent)
        if order is None:
            return self._unknown(state)
        if state.state in TERMINAL:
            return state  # Never roll back a committed terminal checkpoint.

        status = order['status']
        if status == 'REJECTED' and state.filled_qty == 0:
            self._apply(state, Event('reject:' + ident, ident, 'REJECTED',
                                     reason=order.get('reason')))
            return state

        if state.state in ('NEW', 'UNKNOWN_OUTCOME'):
            accept_id = 'accept:' + ident
            if accept_id in state.events:
                # After a missing-order observation, an earlier accept remains
                # committed. A fresh recovery event restores a legal transition.
                accept_id = 'recover:' + ident + ':' + str(len(state.events))
            self._apply(state, Event(accept_id, ident, 'ACCEPTED'))

        # Exact same fill/event mapping as E1; no synthetic fees or rounding.
        for fill in self.broker.fills:
            if fill['intent_id'] != ident or fill['fill_id'] in state.events:
                continue
            if fill.get('order_id') != 'order:' + ident:
                raise ValueError('fill/order identity mismatch')
            qty = Decimal(fill['qty'])
            kind = 'FILLED' if state.filled_qty + qty == state.qty else 'PARTIALLY_FILLED'
            self._apply(state, Event(fill['fill_id'], ident, kind, qty,
                                     Decimal(fill['price']), Decimal(fill['fee']),
                                     fill_id=fill['fill_id']))
        if status in ('REJECTED', 'CANCELED', 'EXPIRED') and state.state not in TERMINAL:
            if status == 'REJECTED' and state.filled_qty:
                # The frozen OrderState disallows PARTIALLY_FILLED -> REJECTED.
                # Preserve committed fills and surface ambiguity, not rollback.
                return self._unknown(state)
            self._apply(state, Event(status.lower() + ':' + ident, ident, status,
                                     reason=order.get('reason')))
        if status == 'FILLED' and state.state != 'FILLED':
            raise ValueError('broker FILLED order has incomplete fill records')
        return state

    def submit(self, intent):
        if intent.protective and not intent.reduce_only:
            raise ValueError('protective order must be reduce_only')
        # Conversion/validation happens before committing a new reservation.
        self._convert(intent)
        new = self.store.reserve(intent)
        _, state = self._restore(intent.intent_id)
        if not new:
            return self._reconcile(intent, state)

        # A pre-existing compatible order can be recovered without a send.
        if self._broker_order(intent) is not None:
            return self._reconcile(intent, state)
        try:
            self.broker.submit(self._convert(intent))
            if self.fault_hook is not None and self.fault_hook(intent):
                return self._unknown(state)
        except Exception:
            # The broker might have committed before the exception. Preserve the
            # original error while leaving durable evidence of that ambiguity.
            try:
                self._unknown(state)
            except Exception:
                pass
            raise
        return self._reconcile(intent, state)

    def query(self, intent_id):
        intent, state = self._restore(intent_id)
        return self._reconcile(intent, state)

    def cancel(self, intent_id, ts):
        intent, state = self._restore(intent_id)
        state = self._reconcile(intent, state)
        order = self._broker_order(intent)
        if order is None or state.state in TERMINAL:
            return state
        if order['status'] not in ('PENDING', 'RESTING'):
            return state
        self.broker.cancel('order:' + intent_id, ts=ts)
        return self.query(intent_id)
