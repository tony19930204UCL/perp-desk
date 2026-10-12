"""Isolated SQLite persistence for prepared execution intents."""
import json
import sqlite3
from decimal import Decimal
from dataclasses import fields
from execution_interface import Intent, Event, OrderState

def _pack(record):
    values = {}
    for field in fields(record):
        value = getattr(record, field.name)
        if isinstance(value, Decimal):
            value = str(value)
        values[field.name] = value
    return json.dumps(values, sort_keys=True)

def _unpack(text, record_type):
    values = json.loads(text)
    if set(values) != {field.name for field in fields(record_type)}:
        raise ValueError('incompatible data')
    names = ('qty', 'stop_price', 'limit_price', 'queue_ahead_qty', 'price', 'fee')
    for name in names:
        if name in values and values[name] is not None:
            if not isinstance(values[name], str):
                raise ValueError('invalid number')
            values[name] = Decimal(values[name])
    return record_type(**values)
