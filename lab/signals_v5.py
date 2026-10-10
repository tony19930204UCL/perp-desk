"""Symbol-bound closed-hour paper detector with v4-compatible contracts."""
import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal

from paper_market import receipt_ms, decimal_string
from signals_v2 import canonical
from signals_v4 import Detector as HourDetector


class Detector(HourDetector):
    """Exactly one registered USDT-M symbol per dedicated detector store."""

    def __init__(self, store_path, version_id, forward_start, symbols):
        if (not isinstance(symbols, dict) or len(symbols) != 1
                or next(iter(symbols.values())) != 'crypto'
                or not next(iter(symbols)).endswith('USDT')):
            raise ValueError('one registered USDT crypto symbol required')
        super().__init__(store_path, version_id, forward_start, symbols)

    @property
    def symbol(self):
        return next(iter(self._registration['symbols']))

    def seed(self, receipt, *, cutoff_ms, now_ms):
        if self.seeded(cutoff_ms):
            return self.manifest()
        end = (cutoff_ms - 1) // self.interval_ms * self.interval_ms
        expected = dict(symbol=self.symbol, interval=self.interval_name,
                        startTime=end - 61 * self.interval_ms,
                        endTime=end - 1, limit=61)
        if receipt.get('endpoint') != '/fapi/v1/klines' or receipt.get('params') != expected:
            raise ValueError('invalid bootstrap source request')
        if not 0 <= now_ms - receipt_ms(receipt) <= 15000:
            raise ValueError('stale bootstrap receipt')
        rows = receipt['payload']
        if len(rows) != 61:
            raise ValueError('incomplete bootstrap history')
        bars = []
        saved_registration, saved_bars = self._registration, self._bars
        self._registration = {**saved_registration,
                              'forward_start': datetime.fromtimestamp(0, timezone.utc).isoformat()}
        self._bars = {}
        try:
            for i, row in enumerate(rows):
                expected_open = end - 61 * self.interval_ms + i * self.interval_ms
                if (type(row[0]) is not int or type(row[6]) is not int
                        or row[0] != expected_open
                        or row[6] != expected_open + self.interval_ms - 1):
                    raise ValueError('discontinuous bootstrap history')
                if row[6] + 1 >= cutoff_ms or row[6] + 1 > now_ms:
                    raise ValueError('unclosed/preactivation boundary bootstrap history')
                bar = dict(symbol=self.symbol, open_time_ms=row[0],
                           close_time_ms=row[6] + 1, closed=True,
                           **{key: Decimal(decimal_string(row[index], key != 'volume'))
                              for key, index in [('open', 1), ('high', 2), ('low', 3),
                                                 ('close', 4), ('volume', 5)]})
                result = super()._process(
                    bar, now=datetime.fromtimestamp(now_ms / 1000, timezone.utc))
                if result['diagnostic'] != 'warmup' or result['intent'] is not None:
                    raise ValueError('invalid bootstrap bar')
                bars.append(bar)
        finally:
            self._registration, self._bars = saved_registration, saved_bars
        manifest = dict(
            decision_cutoff_ms=cutoff_ms, context_start_ms=bars[0]['open_time_ms'],
            context_end_ms=end, bars_count=61,
            receipt_sha256=hashlib.sha256(canonical(receipt).encode()).hexdigest(),
            bars_sha256=hashlib.sha256(canonical(bars).encode()).hexdigest(),
            received_at=receipt['received_at'],
            source_close_times_ms=[bar['close_time_ms'] for bar in bars],
            performance_sample=False)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('ATTACH DATABASE ? AS contextdb', (str(self.context_path),))
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO h1_state VALUES(?,?,?)',
                       (self._registration['version_id'], self.symbol, canonical(bars)))
            db.execute('INSERT OR REPLACE INTO contextdb.context VALUES(1,?)',
                       (canonical(manifest),))
        return manifest
