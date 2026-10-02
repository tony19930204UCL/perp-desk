"""Synthetic sizing fixtures, not observed trading outcomes."""
from pathlib import Path
import importlib.util
import json
from decimal import Decimal as D
import unittest
LAB = Path(__file__).resolve().parents[1]

class SizingTests(unittest.TestCase):
    def test_size_is_capped_by_full_cost_risk_and_exposure(self):
        self.assertTrue((LAB/'paper_sizing.py').exists(), 'paper risk sizing not implemented')
        spec = importlib.util.spec_from_file_location('sizing', LAB/'paper_sizing.py')
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        config = json.loads((LAB/'paper_config.json').read_text())
        instrument = dict(tick_size='0.01', qty_step='0.001', market_qty_step='0.001',
                          min_qty='0.001', max_qty='2000', min_notional='20', taker_fee='0.0005')
        result = mod.size_long(config, instrument, equity='100', bid='99.99', ask='100', target='102')
        self.assertEqual(result['status'], 'accepted')
        quantity = D(result['qty']); entry = D(result['entry_price_bound']); stop = D(result['stop_price'])
        self.assertLessEqual(quantity*D(result['planned_loss_per_unit']),D('1'))
        self.assertLessEqual(quantity*entry,D('300'))
        self.assertEqual(quantity % D('0.001'), 0)
        self.assertEqual(stop % D('0.01'), 0)
        self.assertLess(stop, entry)
        self.assertGreaterEqual(quantity*entry,D('20'))
        self.assertGreater(D(result['planned_loss_per_unit']), entry-stop)

    def test_nonfinite_unapproved_costly_or_below_filters_reject(self):
        import copy
        spec = importlib.util.spec_from_file_location('sizing', LAB/'paper_sizing.py')
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        config = json.loads((LAB/'paper_config.json').read_text())
        ins = dict(tick_size='0.01', qty_step='0.001', market_qty_step='0.003',
                   min_qty='0.001', max_qty='2000', min_notional='20', taker_fee='0.0005')
        for field, bad in [('equity','NaN'), ('bid','101'), ('ask','0'), ('target','100.01')]:
            args=dict(equity='100', bid='99.99', ask='100', target='102');args[field]=bad
            result=mod.size_long(config,ins,**args)
            self.assertEqual(result['status'],'rejected',f'invalid {field} accepted')
        unapproved=copy.deepcopy(config);unapproved['approved']=False
        self.assertEqual(mod.size_long(unapproved,ins,equity='100',bid='99.99',ask='100',target='102')['status'],'rejected')
        impossible=copy.deepcopy(ins);impossible['min_notional']='500'
        self.assertEqual(mod.size_long(config,impossible,equity='100',bid='99.99',ask='100',target='102')['status'],'rejected')
        good=mod.size_long(config,ins,equity='100',bid='99.99',ask='100',target='102')
        self.assertEqual(D(good['qty'])%D('0.003'),0)

if __name__ == '__main__': unittest.main()
