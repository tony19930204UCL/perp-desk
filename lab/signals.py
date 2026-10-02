"""Standalone H1 research detector. No broker, trading, or risk defaults."""
import json
import sqlite3
import hashlib
from decimal import Decimal, localcontext, Context, ROUND_HALF_EVEN, DecimalException
from pathlib import Path
from contextlib import closing
from datetime import datetime, timezone, timedelta

class Detector:
    interval_ms = 300000
    downside_sigma = Decimal("2")
    volume_multiple = Decimal("2")
    def __init__(self, store_path, version_id, forward_start, symbols):
        if not isinstance(version_id, str) or not version_id.strip():
            raise ValueError('explicit nonempty version_id required')
        if not isinstance(forward_start, datetime) or forward_start.utcoffset() != timedelta(0):
            raise ValueError('forward_start must be timezone-aware UTC')
        if (not isinstance(symbols, dict) or not symbols
                or any(not isinstance(s, str) or not s.strip() or c not in ('crypto', 'TradFi')
                       for s, c in symbols.items())):
            raise ValueError('explicit symbols and categories crypto or TradFi required')
        self.path = Path(store_path)
        registration = dict(version_id=version_id, forward_start=forward_start.isoformat(), symbols=dict(symbols))
        with closing(sqlite3.connect(self.path)) as db, db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if names - {'h1_versions', 'h1_state', 'h1_signals'}:
                raise ValueError('dedicated H1 SQLite store required; foreign tables present')
            db.execute('CREATE TABLE IF NOT EXISTS h1_versions (version_id TEXT PRIMARY KEY, registration TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS h1_state (version_id TEXT, symbol TEXT, bars TEXT NOT NULL, PRIMARY KEY(version_id, symbol))')
            db.execute('CREATE TABLE IF NOT EXISTS h1_signals (signal_id TEXT PRIMARY KEY, version_id TEXT NOT NULL, intent TEXT NOT NULL)')
            row = db.execute('SELECT registration FROM h1_versions WHERE version_id=?', (version_id,)).fetchone()
            if row and json.loads(row[0]) != registration:
                raise ValueError('immutable registration conflict')
            if not row:
                db.execute('INSERT INTO h1_versions VALUES (?, ?)', (version_id, json.dumps(registration, sort_keys=True)))
        self._registration = registration
        self._bars = {}

    def ingest(self, symbol, category, *, now_ms, session_open=None, **bar_fields):
        """Integration adapter. session_open is a bounded context dict, NOT a bool."""
        if (not isinstance(symbol, str) or category not in ('crypto', 'TradFi')
                or self._registration['symbols'].get(symbol) != category):
            return dict(diagnostic='reject_category', signal=None)
        if type(now_ms) is not int or now_ms < 0:
            return dict(diagnostic='reject_clock', signal=None)
        try:
            now = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=now_ms)
        except OverflowError:
            return dict(diagnostic='reject_clock', signal=None)
        if 'symbol' in bar_fields:
            return dict(diagnostic='reject_invalid_bar', signal=None)
        result = self.process(dict(symbol=symbol, **bar_fields), now=now, session=session_open)
        result['signal'] = result.pop('intent')
        return result

    def process(self, bar, *, now, session=None):
        # Cursor and signal insertion commit together before an intent is returned.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._bars = {}
            for symbol, payload in db.execute('SELECT symbol, bars FROM h1_state WHERE version_id=?', (self._registration['version_id'],)):
                bars = json.loads(payload)
                for saved_bar in bars:
                    for key in ('open', 'high', 'low', 'close', 'volume'):
                        saved_bar[key] = Decimal(saved_bar[key])
                self._bars[symbol] = bars
            try:
                result = self._process(bar, now=now, session=session)
            except DecimalException:
                return dict(diagnostic='reject_arithmetic', intent=None)
            if result['diagnostic'] in ('warmup', 'no_signal', 'gap_reset', 'research_intent'):
                symbol = bar['symbol']
                bars = self._bars[symbol][-61:]
                db.execute('INSERT INTO h1_state VALUES (?, ?, ?) ON CONFLICT(version_id, symbol) DO UPDATE SET bars=excluded.bars',
                           (self._registration['version_id'], symbol, json.dumps(bars, default=str, sort_keys=True)))
                if result['intent'] is not None:
                    intent = result['intent']
                    seen = db.execute('SELECT signal_id FROM h1_signals WHERE signal_id=?', (intent['signal_id'],)).fetchone()
                    if seen:
                        result = dict(diagnostic='duplicate', intent=None)
                    else:
                        db.execute('INSERT INTO h1_signals VALUES (?, ?, ?)',
                                   (intent['signal_id'], self._registration['version_id'], json.dumps(intent, sort_keys=True)))
            return result

    def _process(self, bar, *, now, session=None):
        if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
            return dict(diagnostic='reject_clock', intent=None)
        if not isinstance(bar, dict):
            return dict(diagnostic='reject_invalid_bar', intent=None)
        expected_keys = {'symbol', 'open_time_ms', 'close_time_ms', 'closed', 'open', 'high', 'low', 'close', 'volume'}
        valid_metadata = (set(bar) == expected_keys and isinstance(bar.get('symbol'), str)
                          and bar['symbol'] in self._registration['symbols']
                          and bar.get('closed') is True
                          and type(bar.get('open_time_ms')) is int
                          and type(bar.get('close_time_ms')) is int
                          and bar['open_time_ms'] >= 0
                          and bar['open_time_ms'] % self.interval_ms == 0
                          and bar['close_time_ms'] == bar['open_time_ms'] + self.interval_ms)
        if not valid_metadata:
            return dict(diagnostic='reject_invalid_bar', intent=None)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = now - epoch
        now_ms = (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
        if bar['close_time_ms'] > now_ms:
            return dict(diagnostic='reject_future', intent=None)
        if epoch + timedelta(milliseconds=bar['open_time_ms']) < datetime.fromisoformat(self._registration['forward_start']):
            return dict(diagnostic='reject_before_forward_start', intent=None)
        values = [bar.get(k) for k in ('open', 'high', 'low', 'close', 'volume')]
        valid = (all(isinstance(v, Decimal) and v.is_finite() for v in values)
                 and all(v > 0 for v in values[:4]) and values[4] >= 0
                 and bar['low'] <= min(bar['open'], bar['close'])
                 and bar['high'] >= max(bar['open'], bar['close'])
                 and bar['low'] <= bar['high'])
        if not valid:
            return dict(diagnostic='reject_invalid_bar', intent=None)
        if self._registration['symbols'][bar['symbol']] == 'TradFi':
            valid = (isinstance(session, dict) and session.get('is_open') is True
                     and session.get('symbol') == bar['symbol']
                     and isinstance(session.get('source'), str) and bool(session['source'].strip())
                     and type(session.get('valid_from_ms')) is int
                     and type(session.get('valid_until_ms')) is int
                     and session['valid_from_ms'] <= bar['open_time_ms']
                     and session['valid_until_ms'] >= bar['close_time_ms'])
            if not valid:
                return dict(diagnostic='reject_session', intent=None)
        bars = self._bars.setdefault(bar['symbol'], [])
        if bars and bar == bars[-1]:
            return dict(diagnostic='duplicate', intent=None)
        if bars and bar['open_time_ms'] <= bars[-1]['open_time_ms']:
            return dict(diagnostic='reject_out_of_order', intent=None)
        if bars and bar['open_time_ms'] != bars[-1]['open_time_ms'] + self.interval_ms:
            expected = bars[-1]['open_time_ms'] + self.interval_ms
            bars[:] = [dict(bar)]
            return dict(diagnostic='gap_reset', intent=None,
                        gap=dict(expected_open_ms=str(expected), actual_open_ms=str(bar['open_time_ms'])))
        prior = bars[-61:]
        bars.append(dict(bar))
        if len(prior) < 61:
            return dict(diagnostic='warmup', intent=None)
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)) as ctx:
            returns = [b['close'] / a['close'] - 1 for a, b in zip(prior, prior[1:])]
            mean = sum(returns, Decimal(0)) / 60
            stdev = (sum((r - mean) ** 2 for r in returns) / 59).sqrt()
            volumes = sorted(b['volume'] for b in prior[-60:])
            median = (volumes[29] + volumes[30]) / 2
            current_return = bar['close'] / prior[-1]['close'] - 1
            triggered = current_return < mean - self.downside_sigma * stdev and bar['volume'] > self.volume_multiple * median
            features = dict(mean=str(mean), sample_stdev=str(stdev), median_volume=str(median),
                            current_return=str(current_return), reference_close=str(prior[-1]['close']),
                            current_close=str(bar['close']), current_volume=str(bar['volume']),
                            return_threshold=str(mean - self.downside_sigma * stdev), volume_threshold=str(self.volume_multiple * median),
                            decimal_precision='50', rounding=ctx.rounding,
                            prior_closes=[str(b['close']) for b in prior],
                            prior_volumes=[str(b['volume']) for b in prior[-60:]],
                            bar_open_ms=str(bar['open_time_ms']), bar_close_ms=str(bar['close_time_ms']),
                            cutoff_ms=str(bar['close_time_ms']), prior_start_ms=str(prior[0]['open_time_ms']),
                            prior_end_ms=str(prior[-1]['close_time_ms']))
        if not triggered:
            return dict(diagnostic='no_signal', intent=None)
        identity = json.dumps([self._registration['version_id'], bar['symbol'], bar['open_time_ms']], separators=(',', ':'))
        intent = dict(signal_id=hashlib.sha256(identity.encode()).hexdigest(),
                      version_id=self._registration['version_id'], symbol=bar['symbol'],
                      side='long', entry_reference=str(prior[-1]['close']),
                      reversion_target=str(prior[-1]['close']), features=features)
        return dict(diagnostic='research_intent', intent=intent)

    @property
    def registration(self):
        return json.loads(json.dumps(self._registration))


def self_check():
    """One handcrafted unit fixture, never a market-performance simulation."""
    import tempfile
    scratch = Path('/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch')
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    base = (start - epoch).days * 86400000
    with tempfile.TemporaryDirectory(prefix='h1-self-check-', dir=scratch) as directory:
        engine = Detector(Path(directory) / 'self-check.sqlite3', 'H1-HANDCRAFTED-SELF-CHECK', start, {'FIXTURE': 'crypto'})
        for i in range(62):
            price = Decimal('100') if i < 61 else Decimal('90')
            fields = dict(open_time_ms=base + i * 300000, close_time_ms=base + (i + 1) * 300000,
                          closed=True, open=price, high=price, low=price, close=price,
                          volume=Decimal('10') if i < 61 else Decimal('21'))
            result = engine.ingest('FIXTURE', 'crypto', now_ms=base+100*300000, **fields)
            if i < 61 and result['diagnostic'] != 'warmup':
                raise RuntimeError('self-check warmup failure')
        repeated = engine.ingest('FIXTURE', 'crypto', now_ms=base+100*300000, **fields)
        if result['diagnostic'] != 'research_intent' or repeated['diagnostic'] != 'duplicate':
            raise RuntimeError('self-check intent/idempotency failure')
        for row in (result, repeated):
            print(json.dumps(dict(kind='diagnostic', fixture='handcrafted-unit-only', **row), sort_keys=True))
        print(json.dumps(dict(kind='self_check', fixture='handcrafted-unit-only', passed=True, fills=0), sort_keys=True))
    return 0


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Offline H1 engineering self-check; no broker or live modes.')
    parser.add_argument('--self-check', action='store_true', required=True)
    parser.parse_args(argv)
    return self_check()


if __name__ == '__main__':
    raise SystemExit(main())
