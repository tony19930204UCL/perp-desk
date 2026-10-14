"""Offline artificial fixtures for SimBrokerAdapter."""
import sys
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal as D
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim_broker import SimBroker, InstrumentSettings, ExecutionModel, RiskContract
from sim_broker_adapter import SimBrokerAdapter
from execution_interface import Intent
from test_execution_contract import ExecutionContractMixin

class SimAdapterTests(ExecutionContractMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.brokers = []

    def adapter_factory(self, approved=True, fault_hook=None):
        settings = InstrumentSettings('TEST', D('.0002'), D('.0005'), D('.1'), D('.1'), D('5'), D('100'))
        risk = RiskContract(approved=approved, version='ARTIFICIAL', max_loss_per_trade_usdt=D('30'),
                            max_daily_loss_usdt=D('40'), max_effective_exposure_x=D('10'),
                            max_positions=2, total_loss_limit_usdt=D('60'), require_stop=True)
        broker = SimBroker(Path(self.tmp.name)/('broker'+str(len(self.brokers))+'.sqlite'),
                           initial_cash=D('100'), instruments=[settings],
                           execution=ExecutionModel(10, 2, None), risk=risk,
                           version_id='artificial', forward_start=100)
        self.brokers.append(broker)
        self.addCleanup(broker.close)
        broker.on_event(dict(type='mark', event_id='mark', symbol='TEST', ts=100, price='100'))
        broker.on_event(dict(type='funding_status', event_id='fund', symbol='TEST', ts=100,
                             complete=True, valid_until_ts=1000000))
        intent = Intent('one', 'TEST', 'BUY', 'market', D('1'), False, D('90'), False, 100)
        return SimBrokerAdapter(broker, fault_hook=fault_hook), intent

    def test_unknown_never_resends(self):
        adapter, intent = self.adapter_factory(fault_hook=lambda _: True)
        self.assertEqual(adapter.submit(intent).state, 'UNKNOWN_OUTCOME')
        self.assertEqual(adapter.submit(intent).state, 'ACCEPTED')
        self.assertEqual(len(adapter.broker.orders), 1)

    def test_unknown_query_reject(self):
        adapter, intent = self.adapter_factory(approved=False, fault_hook=lambda _: True)
        self.assertEqual(adapter.submit(intent).state, 'UNKNOWN_OUTCOME')
        self.assertEqual(adapter.query(intent.intent_id).state, 'REJECTED')

    def test_differential_fills_fees_ledger(self):
        adapter, intent = self.adapter_factory()
        direct, _ = self.adapter_factory()
        adapter.submit(intent)
        direct.broker.submit(direct._convert(intent))
        event = dict(type='book', event_id='book', symbol='TEST', ts=111,
                     bids=[['99', '10']], asks=[['100', '10']])
        adapter.broker.on_event(event)
        direct.broker.on_event(event)
        adapter.query(intent.intent_id)
        self.assertEqual(adapter.broker.fills, direct.broker.fills)
        self.assertEqual(adapter.broker.ledger, direct.broker.ledger)
        self.assertEqual(adapter.broker.cash, direct.broker.cash)

    def test_protective_exit_when_entry_risk_blocked(self):
        adapter, intent = self.adapter_factory()
        adapter.submit(intent)
        adapter.broker.on_event(dict(type='book', event_id='book', symbol='TEST', ts=111,
                                     bids=[['99', '10']], asks=[['100', '10']]))
        adapter.query(intent.intent_id)
        adapter.broker.risk = RiskContract()
        exit_intent = Intent('exit', 'TEST', 'SELL', 'market', D('1'), True, None, True, 112)
        self.assertEqual(adapter.submit(exit_intent).state, 'ACCEPTED')

    def test_fill_then_cancel_race(self):
        adapter, intent = self.adapter_factory()
        adapter.submit(intent)
        adapter.broker.on_event(dict(type='book', event_id='book', symbol='TEST', ts=111,
                                     bids=[['99', '10']], asks=[['100', '10']]))
        self.assertEqual(adapter.cancel(intent.intent_id, 112).state, 'FILLED')

    def test_unknown_then_filled_query(self):
        adapter, intent = self.adapter_factory(fault_hook=lambda _: True)
        adapter.submit(intent)
        adapter.broker.on_event(dict(type='book', event_id='book', symbol='TEST', ts=111,
                                     bids=[['99', '10']], asks=[['100', '10']]))
        self.assertEqual(adapter.query(intent.intent_id).state, 'FILLED')
