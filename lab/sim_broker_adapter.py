"""SimBroker-backed offline adapter. Broker remains the risk authority."""
from decimal import Decimal
from dataclasses import replace
from execution_interface import ExecutionAdapter, Event, OrderState, IdempotencyLedger
from sim_broker import Intent as BrokerIntent

class SimBrokerAdapter(ExecutionAdapter):
    def __init__(self, broker, *, fault_hook=None):
        self.broker = broker
        self.fault_hook = fault_hook
        self.ledger = IdempotencyLedger()
        self.states = {}
        self._intents = {}

    def _convert(self, intent):
        settings = self.broker.instruments[intent.symbol]
        kind = {'market': 'TAKER', 'post_only_limit': 'MAKER'}[intent.type]
        return BrokerIntent(intent.intent_id, intent.symbol, intent.side, intent.qty,
                            intent.stop_price, intent.created_ts, settings.quantity_step,
                            settings.tick, settings.min_notional, settings.max_quantity,
                            kind, intent.reduce_only, limit_price=intent.limit_price,
                            queue_ahead_qty=intent.queue_ahead_qty, expires_ts=intent.expires_ts)

    def submit(self, intent):
        if intent.protective and not intent.reduce_only:
            raise ValueError('protective order must be reduce_only')
        if intent.intent_id in self._intents:
            if self._intents[intent.intent_id] != intent:
                raise ValueError('conflicting intent id')
            return self.query(intent.intent_id)
        # Only the broker decides whether an entry or exit is permissible.
        order = self.broker.submit(self._convert(intent))
        self.ledger.reserve(intent)
        self._intents[intent.intent_id] = intent
        self.states[intent.intent_id] = OrderState(intent.intent_id, intent.qty)
        if self.fault_hook is not None and self.fault_hook(intent):
            state = self.states[intent.intent_id]
            state.apply(Event('unknown:'+intent.intent_id, intent.intent_id, 'UNKNOWN_OUTCOME'))
            return state
        return self.query(intent.intent_id)

    def query(self, intent_id):
        state = self.states[intent_id]
        order = self.broker.orders['order:' + intent_id]
        if state.state in ('NEW', 'UNKNOWN_OUTCOME'):
            if order['status'] == 'REJECTED':
                state.apply(Event('reject:'+intent_id, intent_id, 'REJECTED', reason=order['reason']))
                return state
            state.apply(Event('accept:'+intent_id, intent_id, 'ACCEPTED'))
        for fill in self.broker.fills:
            if fill['intent_id'] != intent_id or fill['fill_id'] in state.events:
                continue
            qty = Decimal(fill['qty'])
            typ = 'FILLED' if state.filled_qty + qty == state.qty else 'PARTIALLY_FILLED'
            state.apply(Event(fill['fill_id'], intent_id, typ, qty, Decimal(fill['price']),
                              Decimal(fill['fee']), fill_id=fill['fill_id']))
        status = order['status']
        if status in ('REJECTED', 'CANCELED', 'EXPIRED') and state.state not in ('REJECTED', 'CANCELED', 'EXPIRED', 'FILLED'):
            state.apply(Event(status.lower()+':'+intent_id, intent_id, status, reason=order.get('reason')))
        return state

    def cancel(self, intent_id, ts):
        self.broker.cancel('order:'+intent_id, ts=ts)
        return self.query(intent_id)
