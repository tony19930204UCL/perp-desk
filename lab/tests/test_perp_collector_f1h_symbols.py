"""Tests for F1H-U3: ALLOWED_SYMBOLS on PublicClient and the v5 runtime subclass.

All tests are network-free. The symbol validation gate in PublicClient.get() fires
before any HTTP call is attempted. For tests that assert a symbol IS accepted we
stub the transport with an opener that raises a distinct RuntimeError so we can
distinguish 'rejected at symbol check (ValueError)' from 'passed validation,
reached network (RuntimeError)'. sleep and wall_ms are also stubbed so no real
time is consumed.
"""
import sys
import tempfile
from pathlib import Path
import unittest

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

from perp_collector import PublicClient

ENDPOINT = '/fapi/v1/ticker/bookTicker'


def _network_sentinel(*args, **kwargs):
    """Opener stub that signals 'symbol passed validation, transport was reached'."""
    raise RuntimeError('network_reached')


def _make_stubbed_client(cls=PublicClient):
    """Return an instance of cls with transport stubbed out (no real HTTP, no sleep)."""
    return cls(
        opener=_network_sentinel,
        sleep=lambda s: None,
        wall_ms=lambda: 0,
    )


class TestPublicClientAllowedSymbols(unittest.TestCase):
    """Unit tests for the ALLOWED_SYMBOLS class attribute on the base PublicClient."""

    def test_allowed_symbols_is_frozenset(self):
        self.assertIsInstance(PublicClient.ALLOWED_SYMBOLS, frozenset)

    def test_allowed_symbols_contains_exactly_eth_and_xau(self):
        self.assertEqual(PublicClient.ALLOWED_SYMBOLS, frozenset({'ETHUSDT', 'XAUUSDT'}))

    def test_base_client_rejects_btcusdt_before_network(self):
        """BTCUSDT must raise ValueError with 'unsupported observation symbol'."""
        client = _make_stubbed_client(PublicClient)
        with self.assertRaises(ValueError) as ctx:
            client.get(ENDPOINT, {'symbol': 'BTCUSDT'})
        self.assertIn('unsupported observation symbol', str(ctx.exception))

    def test_base_client_accepts_ethusdt_at_validation_stage(self):
        """ETHUSDT passes the symbol check; the stubbed opener then raises RuntimeError."""
        client = _make_stubbed_client(PublicClient)
        with self.assertRaises(RuntimeError) as ctx:
            client.get(ENDPOINT, {'symbol': 'ETHUSDT'})
        self.assertEqual(str(ctx.exception), 'network_reached')

    def test_base_client_accepts_xauusdt_at_validation_stage(self):
        """XAUUSDT passes the symbol check; the stubbed opener then raises RuntimeError."""
        client = _make_stubbed_client(PublicClient)
        with self.assertRaises(RuntimeError) as ctx:
            client.get(ENDPOINT, {'symbol': 'XAUUSDT'})
        self.assertEqual(str(ctx.exception), 'network_reached')

    def test_base_client_rejects_solusdt_before_network(self):
        """SOLUSDT is not in the base set; must raise ValueError before any network."""
        client = _make_stubbed_client(PublicClient)
        with self.assertRaises(ValueError) as ctx:
            client.get(ENDPOINT, {'symbol': 'SOLUSDT'})
        self.assertIn('unsupported observation symbol', str(ctx.exception))


class TestPaperPublicClientSubclass(unittest.TestCase):
    """Unit tests for the _PaperPublicClient subclass defined in paper_runtime_v5."""

    def setUp(self):
        from paper_runtime_v5 import _PaperPublicClient
        self.cls = _PaperPublicClient

    def test_subclass_allowed_symbols_contains_f1h_symbols(self):
        expected = frozenset({'ETHUSDT', 'XAUUSDT', 'BTCUSDT', 'SOLUSDT', 'XRPUSDT'})
        self.assertEqual(self.cls.ALLOWED_SYMBOLS, expected)

    def test_subclass_accepts_btcusdt_at_validation_stage(self):
        client = _make_stubbed_client(self.cls)
        with self.assertRaises(RuntimeError) as ctx:
            client.get(ENDPOINT, {'symbol': 'BTCUSDT'})
        self.assertEqual(str(ctx.exception), 'network_reached')

    def test_subclass_accepts_solusdt_at_validation_stage(self):
        client = _make_stubbed_client(self.cls)
        with self.assertRaises(RuntimeError) as ctx:
            client.get(ENDPOINT, {'symbol': 'SOLUSDT'})
        self.assertEqual(str(ctx.exception), 'network_reached')

    def test_subclass_accepts_xrpusdt_at_validation_stage(self):
        client = _make_stubbed_client(self.cls)
        with self.assertRaises(RuntimeError) as ctx:
            client.get(ENDPOINT, {'symbol': 'XRPUSDT'})
        self.assertEqual(str(ctx.exception), 'network_reached')

    def test_subclass_rejects_dogeusdt_before_network(self):
        client = _make_stubbed_client(self.cls)
        with self.assertRaises(ValueError) as ctx:
            client.get(ENDPOINT, {'symbol': 'DOGEUSDT'})
        self.assertIn('unsupported observation symbol', str(ctx.exception))

    def test_subclass_still_rejects_unknown_symbol(self):
        client = _make_stubbed_client(self.cls)
        with self.assertRaises(ValueError) as ctx:
            client.get(ENDPOINT, {'symbol': 'BNBUSDT'})
        self.assertIn('unsupported observation symbol', str(ctx.exception))


class TestV5RuntimeDefaultClientIsSubclass(unittest.TestCase):
    """Assert that a v5 PaperRuntime built with client=None uses _PaperPublicClient."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_v5_runtime_no_client_uses_paper_public_client(self):
        """When client=None the v5 runtime must create a _PaperPublicClient instance."""
        from paper_runtime_v5 import PaperRuntime, _PaperPublicClient

        START = 1790812800000
        HOUR = 3600000

        # We only need to construct the runtime, not poll it.  The __init__ does
        # not call the client, so no network is attempted.
        import json
        from datetime import datetime, timezone

        def iso(ms):
            return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()

        # Build a minimal FakeClient for the constructor (which does call the
        # client only to persist reference data when the fixture flag is set).
        # We pass client=None so the runtime allocates its own.
        root = Path(self.temp.name) / 'v5_no_client'
        config_path = LAB / 'paper_config_f1h_btcusdt.json'

        r = PaperRuntime(root, config_path, client=None, clock_ms=lambda: START, fixture=True)
        try:
            self.assertIsInstance(r.client, _PaperPublicClient)
        finally:
            r.close()


class TestV4RuntimeDefaultClientIsBaseRuntimeClient(unittest.TestCase):
    """Assert that a v4 PaperRuntime built with client=None uses the base RuntimeClient."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_v4_runtime_no_client_uses_runtime_client_not_paper_public_client(self):
        """v4 must use RuntimeClient (base), not _PaperPublicClient from v5."""
        from paper_runtime_v4 import PaperRuntime as V4Runtime
        from paper_runtime_v2 import RuntimeClient
        from paper_runtime_v5 import _PaperPublicClient

        START = 1790812800000
        root = Path(self.temp.name) / 'v4_no_client'
        config_path = LAB / 'paper_config_v4.json'

        r = V4Runtime(root, config_path, client=None, clock_ms=lambda: START, fixture=True)
        try:
            self.assertIsInstance(r.client, RuntimeClient)
            # Must NOT be the expanded-symbols subclass from v5.
            self.assertNotIsInstance(r.client, _PaperPublicClient)
        finally:
            r.close()


if __name__ == '__main__':
    unittest.main()
