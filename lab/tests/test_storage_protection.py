"""Public-safe isolated acceptance for PAPER v3 storage protection."""
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

LAB=Path(__file__).resolve().parents[1]


class MutableProbe:
    def __init__(self,used=50,free=1000):
        self.used=used;self.free=free
    def __call__(self,state_dir):
        return dict(used_bytes=self.used,free_bytes=self.free,total_bytes=2000,
                    files={'runtime.sqlite3':self.used})


class StorageProtectionTests(unittest.TestCase):
    def policy(self):
        from storage_protection import StoragePolicy
        return StoragePolicy(warning_bytes=100,new_risk_limit_bytes=200,
                             exit_reserve_bytes=100,min_free_bytes=50,
                             max_exit_cycle_bytes=20)

    def test_warning_new_risk_limit_exit_reserve_and_low_space_are_distinct(self):
        from storage_protection import StorageGuard
        with tempfile.TemporaryDirectory() as td:
            probe=MutableProbe();guard=StorageGuard(td,self.policy(),probe=probe)
            self.assertEqual(guard.observe()['level'],'normal')
            probe.used=150
            s=guard.observe()
            self.assertEqual(s['level'],'warning');self.assertTrue(s['new_risk_allowed'])
            self.assertTrue(s['exit_accounting_cycle_allowed']);self.assertFalse(s['disk_full'])
            probe.used=210
            s=guard.observe()
            self.assertEqual(s['level'],'protect');self.assertFalse(s['new_risk_allowed'])
            self.assertTrue(s['exit_accounting_cycle_allowed'])
            self.assertIn('new_risk_namespace_limit',s['reasons'])
            probe.used=285
            s=guard.observe()
            self.assertEqual(s['level'],'halt');self.assertFalse(s['exit_accounting_cycle_allowed'])
            self.assertIn('exit_namespace_reserve_exhausted',s['reasons'])
            self.assertFalse(s['disk_full'])
            probe.used=150;probe.free=60
            s=guard.observe()
            self.assertEqual(s['level'],'halt')
            self.assertIn('insufficient_free_space_for_exit_reserve',s['reasons'])
            self.assertFalse(s['disk_full'],'low configured headroom is not disk full')
            probe.free=0
            self.assertTrue(guard.observe()['disk_full'])

    def test_flat_runtime_stops_growth_at_new_risk_limit_without_research_poll(self):
        from paper_runtime_v3 import PaperRuntime
        with tempfile.TemporaryDirectory() as td:
            probe=MutableProbe(used=210,free=1000)
            r=PaperRuntime(td,LAB/'paper_config_v3.json',clock_ms=lambda:100000000,
                           fixture=True,storage_policy=self.policy(),storage_probe=probe)
            try:
                before={p.name:p.read_bytes() for p in Path(td).glob('*.sqlite3')}
                s=r.poll()
                self.assertFalse(s['storage_protection']['new_risk_allowed'])
                self.assertTrue(s['storage_protection']['exit_accounting_cycle_allowed'])
                self.assertTrue(s['storage_protection']['stop_after_publish'])
                self.assertIn('storage_new_risk_inhibited',s['blockers'])
                self.assertEqual(s['fills_count'],0)
                # No market/research collect occurred; existing durable prefixes remain readable.
                for name,raw in before.items():
                    self.assertTrue((Path(td)/name).read_bytes().startswith(b'SQLite format 3'))
                    self.assertTrue(raw.startswith(b'SQLite format 3'))
            finally:r.close()

    def _runtime_with_position(self,td,probe):
        from paper_runtime_v3 import PaperRuntime
        from sim_broker import Intent
        r=PaperRuntime(td,LAB/'paper_config_v3.json',clock_ms=lambda:100003000,
                       fixture=True,storage_policy=self.policy(),storage_probe=probe)
        spec=dict(symbol='ETHUSDT',maker_fee='0.0002',taker_fee='0.0005',
                  qty_step='0.001',tick_size='0.01',min_notional='20',
                  max_qty='2000',min_qty='0.001',category='crypto')
        r.ensure_broker(spec);b=r.broker;s=b.instruments['ETHUSDT']
        b.on_event(dict(type='mark',event_id='m0',symbol='ETHUSDT',ts=100000000,price='2700'))
        b.on_event(dict(type='funding_status',event_id='f0',symbol='ETHUSDT',ts=100000000,
                        complete=True,valid_until_ts=200000000))
        b.submit(Intent('existing','ETHUSDT','BUY',D('.01'),D('2600'),100000000,
                        s.quantity_step,s.tick,s.min_notional,s.max_quantity,'TAKER',False))
        b.on_event(dict(type='book',event_id='b0',symbol='ETHUSDT',ts=100003000,
                        bids=[['2699.99','1']],asks=[['2700.01','1']]))
        self.assertIn('ETHUSDT',b.positions)
        r.state.setdefault('handled_signals',{})['existing']=dict(status='submitted',target='2800')
        r.save()
        return r

    def test_protection_inhibits_new_risk_but_keeps_durable_reduce_only_exit(self):
        from sim_broker import Intent
        with tempfile.TemporaryDirectory() as td:
            probe=MutableProbe(used=210,free=1000)
            r=self._runtime_with_position(td,probe)
            try:
                before_fills=list(r.broker.fills);before_ledger=list(r.broker.ledger)
                pre=r._storage_preflight()
                self.assertFalse(pre['new_risk_allowed']);self.assertTrue(pre['exit_accounting_cycle_allowed'])
                r.exit_policy(100004000,'2800')
                exit_order=next(o for o in r.broker.orders.values() if o['intent']['reduce_only'])
                self.assertEqual(exit_order['status'],'PENDING')
                r.broker.on_event(dict(type='book',event_id='b1',symbol='ETHUSDT',ts=100007000,
                                       bids=[['2800','1']],asks=[['2800.01','1']]))
                self.assertEqual(r.broker.positions,{})
                self.assertEqual(r.broker.fills[:len(before_fills)],before_fills)
                self.assertEqual(r.broker.ledger[:len(before_ledger)],before_ledger)
                self.assertEqual(len(r.broker.fills),len(before_fills)+1)
                self.assertTrue(any(x['type']=='realized' for x in r.broker.ledger[len(before_ledger):]))
            finally:r.close()

    def test_insufficient_exit_headroom_halts_before_any_exit_write(self):
        from storage_protection import StorageProtectionHalt
        with tempfile.TemporaryDirectory() as td:
            probe=MutableProbe(used=285,free=1000)
            # Build exposure while storage is initially safe, then cross the hard reserve boundary.
            probe.used=50;r=self._runtime_with_position(td,probe)
            try:
                before=(list(r.broker.fills),list(r.broker.ledger),dict(r.broker.positions))
                probe.used=285
                with self.assertRaisesRegex(StorageProtectionHalt,'open PAPER exposure'):
                    r._storage_preflight()
                self.assertEqual((r.broker.fills,r.broker.ledger,r.broker.positions),before)
            finally:r.close()

    def test_interrupted_sqlite_exit_write_rolls_back_and_restart_preserves_prefix_then_recovers(self):
        from sim_broker import SimBroker,InstrumentSettings,ExecutionModel,RiskContract,Intent
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'broker.sqlite3'
            settings=InstrumentSettings('TEST',D('.0002'),D('.0005'),D('.1'),D('.1'),D('5'),D('100'))
            risk=RiskContract(True,'ARTIFICIAL-ONLY',D('30'),D('40'),D('10'),2,D('60'),True)
            def open_broker():
                return SimBroker(path,initial_cash=D('100'),instruments=[settings],
                                 execution=ExecutionModel(10,2,None),risk=risk,
                                 version_id='artificial-storage',forward_start=100)
            b=open_broker()
            b.on_event(dict(type='mark',event_id='m0',symbol='TEST',ts=100,price='100'))
            b.on_event(dict(type='funding_status',event_id='fs',symbol='TEST',ts=100,
                            complete=True,valid_until_ts=1000000))
            b.submit(Intent('open','TEST','BUY',D('1'),D('90'),100,D('.1'),D('.1'),D('5'),D('100'),'TAKER',False))
            b.on_event(dict(type='book',event_id='book0',symbol='TEST',ts=111,
                            bids=[['99','10']],asks=[['100','10']]))
            b.submit(Intent('exit','TEST','SELL',D('1'),None,112,D('.1'),D('.1'),D('5'),D('100'),'TAKER',True))
            prefix_fills=list(b.fills);prefix_ledger=list(b.ledger);prefix_audit=list(b.audit)
            raw=b._db
            class InterruptingConnection:
                def __init__(self,inner):self.inner=inner
                def __enter__(self):self.inner.__enter__();return self
                def __exit__(self,*args):return self.inner.__exit__(*args)
                def execute(self,sql,args=()):
                    cur=self.inner.execute(sql,args)
                    if sql.startswith('INSERT OR REPLACE INTO sim_broker_state'):
                        raise OSError('synthetic interrupted durable write')
                    return cur
            b._db=InterruptingConnection(raw)
            event=dict(type='book',event_id='book-exit',symbol='TEST',ts=123,
                       bids=[['110','10']],asks=[['111','10']])
            with self.assertRaisesRegex(OSError,'interrupted durable write'):
                b.on_event(event)
            self.assertEqual(b.fills,prefix_fills);self.assertEqual(b.ledger,prefix_ledger)
            self.assertEqual(b.audit,prefix_audit)
            b._db=raw;b.close()
            b=open_broker()
            try:
                self.assertEqual(b.fills,prefix_fills);self.assertEqual(b.ledger,prefix_ledger)
                self.assertEqual(b.audit,prefix_audit);self.assertIn('TEST',b.positions)
                b.on_event(event)
                self.assertNotIn('TEST',b.positions)
                self.assertEqual(b.fills[:len(prefix_fills)],prefix_fills)
                self.assertEqual(b.ledger[:len(prefix_ledger)],prefix_ledger)
                self.assertEqual(b.audit[:len(prefix_audit)],prefix_audit)
            finally:b.close()


if __name__=='__main__':unittest.main()
