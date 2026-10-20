"""Synthetic Decimal projection regression coverage; no external sources."""
import copy
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dashboard_multiarm_model as m

SAMPLE=json.loads((Path(__file__).parent/'fixtures/dashboard_multiarm_browser.json').read_text())
NOW=SAMPLE['last_ts']+120000


class ModelTests(unittest.TestCase):
    def source(self):
        return copy.deepcopy(SAMPLE)
    def test_exact_equity(self):
        row=m.project('w1-xrpusdt',self.source(),now_ms=NOW)
        self.assertEqual(row['account']['unrealized'],'0.03030')
        self.assertEqual(row['account']['equity'],'99.69854579690005316')
    def test_stale(self):
        self.assertTrue(m.project('x',self.source(),now_ms=NOW)['stale'])
    def test_fresh(self):
        self.assertFalse(m.project('x',self.source(),now_ms=SAMPLE['last_ts'])['stale'])
    def test_twelve(self):
        self.assertEqual(len(m.ARMS),12)
        self.assertEqual(len({x for x,_ in m.ARMS}),12)
    def test_exact_orders(self):
        a=m.project('x',self.source(),now_ms=NOW)
        self.assertEqual(a['open_orders']['total'],2)
    def test_short_position(self):
        s=self.source();s['positions']['XRPUSDT']['qty']='-2'
        a=m.project('x',s,now_ms=NOW)
        self.assertEqual(a['positions']['rows'][0]['side'],'SHORT')
        self.assertEqual(a['account']['unrealized'],'-0.0006')
    def test_missing_mark_unknown_equity(self):
        s=self.source();s['marks']={}
        a=m.project('x',s,now_ms=NOW)
        self.assertIsNone(a['account']['equity'])
        self.assertEqual(a['positions']['rows'][0]['state'],'unknown')
    def test_reconcile(self):
        a=m.project('x',self.source(),now_ms=NOW)
        self.assertEqual(a['reconcile']['state'],'consistent')
    def test_bad_reconcile(self):
        s=self.source();s['cash']='99'
        self.assertEqual(m.project('x',s,now_ms=NOW)['reconcile']['state'],'inconsistent')
    def test_partial_close(self):
        s=self.source();s['positions']={};s['marks']={}
        s['fills']=[dict(fill_id='a',symbol='X',qty='3',side='BUY',price='2',ts=1,order_id='o1'),
                    dict(fill_id='b',symbol='X',qty='1',side='SELL',price='3',ts=2,order_id='o2')]
        self.assertEqual(m.project('x',s,now_ms=NOW)['closed']['total'],0)
    def test_completed_close(self):
        s=self.source();s['positions']={};s['marks']={}
        s['fills']=[dict(fill_id='a',symbol='X',qty='3',side='BUY',price='2',ts=1,order_id='o1'),
                    dict(fill_id='b',symbol='X',qty='3',side='SELL',price='3',ts=2,order_id='o2')]
        a=m.project('x',s,now_ms=NOW)
        self.assertEqual(a['closed']['total'],1)
        self.assertEqual(a['closed']['rows'][0]['state'],'known')
    def test_zero_cross_unknown(self):
        s=self.source();s['positions']={};s['marks']={}
        s['fills']=[dict(fill_id='a',symbol='X',qty='3',side='BUY',price='2',ts=1,order_id='o1'),
                    dict(fill_id='b',symbol='X',qty='5',side='SELL',price='3',ts=2,order_id='o2')]
        self.assertEqual(m.project('x',s,now_ms=NOW)['closed']['rows'][0]['state'],'unknown')
    def test_wrong_type(self):
        self.assertEqual(m.project('x',[],now_ms=NOW)['state'],'unknown')
    def test_empty_tables(self):
        s=self.source();s['orders']={};s['fills']=[]
        a=m.project('x',s,now_ms=NOW)
        self.assertEqual(a['orders']['total'],0)
        self.assertEqual(a['fills']['total'],0)
    def test_unknown_status(self):
        s=self.source();s['orders']['order:pending']['status']='NEW-STATE'
        a=m.project('x',s,now_ms=NOW)
        self.assertEqual(a['open_orders']['total'],1)
        self.assertTrue(any(r['status']=='NEW-STATE' for r in a['orders']['rows']))


def make_field_case(field):
    def test(self):
        s=self.source();s.pop(field)
        a=m.project('x',s,now_ms=NOW)
        self.assertEqual(a['state'],'unknown')
    return test


for ix,name in enumerate(('cash','initial_cash','last_ts','positions','marks','orders','fills','ledger')):
    setattr(ModelTests,'test_missing_%02d'%ix,make_field_case(name))


def make_decimal_case(ix,value):
    def test(self):
        self.assertIsNone(m.dec(value))
    return test


for ix,value in enumerate((None,True,False,0,1,0.1,[],{},'','NaN','Infinity',
                            '-Infinity','abc','1e9999999999999','--1','1,2')):
    setattr(ModelTests,'test_invalid_decimal_%02d'%ix,make_decimal_case(ix,value))


def make_status_case(ix,status,expected):
    def test(self):
        s=self.source();s['orders']['order:pending']['status']=status
        a=m.project('x',s,now_ms=NOW)
        self.assertEqual(any(r['id']=='order:pending' for r in a['open_orders']['rows']),expected)
    return test


for ix,(status,expected) in enumerate([('PENDING',True),('RESTING',True),('FILLED',False),
    ('EXPIRED',False),('CANCELED',False),('REJECTED',False),('OTHER',False),('',False)]):
    setattr(ModelTests,'test_status_%02d'%ix,make_status_case(ix,status,expected))


def make_invalid_position(ix,field,value):
    def test(self):
        s=self.source();s['positions']['XRPUSDT'][field]=value
        a=m.project('x',s,now_ms=NOW)
        self.assertIsNone(a['account']['equity'])
    return test


for ix,(field,value) in enumerate([('qty',None),('entry',None),('qty','zero'),('entry','NaN'),
                                    ('qty',0),('entry',1.0),('qty',[]),('entry',{})]):
    setattr(ModelTests,'test_position_invalid_%02d'%ix,make_invalid_position(ix,field,value))


    def test_extreme_zero_exponent(self):
        self.assertIsNone(m.dec('0E+999'))
        self.assertIsNone(m.dec('0E-999'))
        self.assertIsNone(m.exact(m.Decimal('0E+999')))

    def test_extreme_project_fields(self):
        for field in ('cash', 'initial_cash'):
            s=self.source()
            s[field]='1E+999'
            result=m.project('x',s,now_ms=NOW)
            self.assertIsNone(result['account']['cash' if field=='cash' else 'initial_cash'])
        s=self.source()
        s['positions']['XRPUSDT']['qty']='1E+999'
        result=m.project('x',s,now_ms=NOW)
        self.assertIsNone(result['account']['equity'])


if __name__=='__main__':
    unittest.main()
