"""Offline execution contract; all quantities and fees are exact decimals."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import re

TERMINAL = frozenset(('REJECTED', 'FILLED', 'CANCELED', 'EXPIRED'))
TRANSITIONS = {
    'NEW': {'ACCEPTED', 'REJECTED', 'UNKNOWN_OUTCOME'},
    'ACCEPTED': {'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED', 'REJECTED', 'UNKNOWN_OUTCOME'},
    'PARTIALLY_FILLED': {'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED', 'UNKNOWN_OUTCOME'},
    'UNKNOWN_OUTCOME': {'ACCEPTED', 'REJECTED', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'EXPIRED'},
}

@dataclass(frozen=True)
class Intent:
    intent_id: str
    symbol: str
    side: str
    type: str
    qty: Decimal
    reduce_only: bool
    stop_price: Decimal | None
    protective: bool
    created_ts: int
    limit_price: Decimal | None = None
    queue_ahead_qty: Decimal | None = None
    expires_ts: int | None = None

@dataclass(frozen=True)
class Event:
    event_id: str
    intent_id: str
    type: str
    qty: Decimal = Decimal(0)
    price: Decimal | None = None
    fee: Decimal = Decimal(0)
    reason: str | None = None
    fill_id: str | None = None

def client_order_id(intent_id, attempt=0):
    if not isinstance(intent_id, str) or not intent_id or type(attempt) is not int or attempt < 0:
        raise ValueError('invalid intent id or attempt')
    digest = hashlib.sha256((intent_id + ':' + str(attempt)).encode()).hexdigest()[:28]
    result = 'exec_' + digest
    assert len(result) <= 36 and re.fullmatch(r'[A-Za-z0-9_-]+', result)
    return result

class OrderState:
    def __init__(self, intent_id, qty):
        self.intent_id = intent_id
        self.qty = Decimal(qty)
        if not self.qty.is_finite() or self.qty <= 0:
            raise ValueError('positive finite quantity required')
        self.state = 'NEW'
        self.filled_qty = Decimal(0)
        self.fees = Decimal(0)
        self.events = {}

    def apply(self, event):
        if event.intent_id != self.intent_id or not event.event_id:
            raise ValueError('event identity mismatch')
        if event.event_id in self.events:
            if self.events[event.event_id] != event:
                raise ValueError('conflicting event id')
            return False
        if self.state in TERMINAL or event.type not in TRANSITIONS.get(self.state, set()):
            raise ValueError('illegal transition: ' + self.state + ' -> ' + event.type)
        if event.type == 'PARTIALLY_FILLED':
            if event.qty <= 0 or self.filled_qty + event.qty >= self.qty:
                raise ValueError('invalid partial fill quantity')
        if event.type == 'FILLED':
            if event.qty <= 0 or self.filled_qty + event.qty != self.qty:
                raise ValueError('invalid final fill quantity')
        if event.type in ('FILLED', 'PARTIALLY_FILLED'):
            if event.price is None or event.price <= 0 or event.fee < 0:
                raise ValueError('invalid fill price or fee')
            self.filled_qty += event.qty
            self.fees += event.fee
        elif event.qty != 0:
            raise ValueError('non-fill event has quantity')
        self.state = event.type
        self.events[event.event_id] = event
        return True

class IdempotencyLedger:
    def __init__(self, records=None):
        self.records = dict(records or {})

    def reserve(self, intent):
        old = self.records.get(intent.intent_id)
        fingerprint = repr(intent)
        if old is not None:
            if old != fingerprint:
                raise ValueError('conflicting intent id')
            return False
        self.records[intent.intent_id] = fingerprint
        return True

    def snapshot(self):
        return dict(self.records)

class ExecutionAdapter(ABC):
    @abstractmethod
    def submit(self, intent): ...
    @abstractmethod
    def query(self, intent_id): ...
    @abstractmethod
    def cancel(self, intent_id, ts): ...
