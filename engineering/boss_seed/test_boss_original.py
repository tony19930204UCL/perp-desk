import unittest, tempfile, json, sys, time, pathlib, os
sys.path.insert(0,str(pathlib.Path(__file__).parent/'candidate/lab'))
class AccountTests(unittest.TestCase):
 def test_streamed_actual_cash_reconciliation(self):
  import boss_readonly as B
  with tempfile.TemporaryDirectory() as d:
   root=pathlib.Path(d); p=root/'lab/data/tactical-sniper';p.mkdir(parents=True)
   s={'schema':1,'cash':150.,'position':None,'used':[],'seq':1}
   row={'seq':1,'events':[],'state':s.copy()}
   s.update(cash=149.8,seq=2,used=['a'])
   row2={'seq':2,'events':[{'kind':'ENTRY','fee':.2,'t':10,'symbol':'X'}],'state':s.copy()}
   s.update(cash=152.8,seq=3)
   row3={'seq':3,'events':[{'kind':'TP2','pnl_after_exit_fee':3.,'fee':.1,'t':20,'symbol':'X'}],'state':s.copy()}
   (p/'journal.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in [row,row2,row3]))
   result=B.journal(root,s)
   self.assertAlmostEqual(result['realized'],2.8)
   self.assertAlmostEqual(result['fees'],.3)
   self.assertEqual(result['count'],3)
class ProjectionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name);self.now=1793816683501
 def tearDown(self):self.tmp.cleanup()
 def put(self,key,data):
  import boss_readonly as B
  p=self.root/B.FILES[key];p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data))
 def flat(self):
  s={'schema':1,'cash':150.,'position':None,'used':[],'seq':1}
  self.put('state',s);self.put('tactical',{'mode':'PAPER_ONLY','status':'OK','updated_ms':self.now,'cash':150.,'used_order_ids':[],'open_positions':0,'equity':150.,'unrealized_after_exit_cost':None})
  import boss_readonly as B
  p=self.root/B.FILES['journal'];p.write_text(json.dumps({'seq':1,'events':[],'state':s})+'\n')
 def test_flat_zero_distinct_missing(self):
  import boss_readonly as B
  self.assertIsNone(B.read_boss(self.root,now_ms=self.now)['tactical']['unrealized'])
  self.flat();x=B.read_boss(self.root,now_ms=self.now)['tactical'];self.assertEqual(x['unrealized'],0.);self.assertEqual(x['available_margin'],150.)
 def test_prediction_missing_is_not_fake_bankroll(self):
  import boss_readonly as B
  x=B.read_boss(self.root,now_ms=self.now)['prediction'];self.assertEqual(x['state'],'紙盤尚未啟動');self.assertIsNone(x['bankroll'])
 def test_position_missing_valuation_does_not_use_cash_as_equity(self):
  import boss_readonly as B
  self.flat();s=json.loads((self.root/B.FILES['state']).read_text());s['position']={'symbol':'X','isolated_margin':10};self.put('state',s)
  t=json.loads((self.root/B.FILES['tactical']).read_text());t['open_positions']=1;self.put('tactical',t)
  x=B.read_boss(self.root,now_ms=self.now)['tactical'];self.assertIsNone(x['equity']);self.assertIsNone(x['unrealized'])
 def test_weekly_entry_and_observed_dates_distinct(self):
  import boss_readonly as B
  self.put('weekly',{'updated':'2026-11-04T18:17:57Z','gross_usdt_assumed':150,'tranches':[{'entry_day':1793664000000,'exit_day':1794268800000,'books':{'F2_raw_quint':{'long_leg_bp':100,'short_leg_pnl_bp':-20,'funding_bp':2,'net_usdt_at_150_gross':.42,'missing':0}}}]})
  self.put('weekly_state',{'open':[{'entry_day':1793664000000,'obs':{'X':{'t':1793750476024}}}]})
  x=B.read_boss(self.root,now_ms=self.now)['weekly']['books'][0];self.assertNotEqual(x['entry_day'],x['observed_start_ms']);self.assertAlmostEqual(x['long_pnl'],.75)
 def test_corrupt_journal_nulls_realized(self):
  import boss_readonly as B
  self.flat();p=self.root/B.FILES['journal'];p.write_text(p.read_text()+'broken\n');self.assertIsNone(B.read_boss(self.root,now_ms=self.now)['tactical']['realized'])
 def test_unknown_transfer_not_blind_cash_minus_150(self):
  import boss_readonly as B
  self.flat();s=json.loads((self.root/B.FILES['state']).read_text());s.update(seq=2,cash=250);self.put('state',s)
  with (self.root/B.FILES['journal']).open('a') as f:f.write(json.dumps({'seq':2,'state':s,'events':[{'kind':'CASH_TRANSFER','amount':100}]})+'\n')
  self.assertIsNone(B.read_boss(self.root,now_ms=self.now)['tactical']['realized'])
 def test_symlink_rejected(self):
  import boss_readonly as B
  p=self.root/B.FILES['state'];p.parent.mkdir(parents=True);target=self.root/'target';target.write_text('{}');p.symlink_to(target)
  with self.assertRaises(ValueError):B.load(self.root,'state')
class TransportTests(unittest.TestCase):
 def test_new_api_and_unchanged_old_route_allowlist(self):
  import dashboard_v2_server as S, threading, urllib.request, urllib.error
  with tempfile.TemporaryDirectory() as d:
   root=pathlib.Path(d);server=S.create_server(scorecard_path=str(root/'s'),journal_path=str(root/'j'),ledger_path=str(root/'l'),port=0)
   server.configure_monitor(profile_root=d);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
   base='http://127.0.0.1:'+str(server.server_port)
   try:
    result=json.load(urllib.request.urlopen(base+'/api/boss-dashboard'));self.assertEqual(result['schema'],'boss-dashboard.v1')
    home=urllib.request.urlopen(base+'/').read();self.assertIn('三戰場'.encode(),home)
    self.assertEqual(urllib.request.urlopen(base+'/engineering').status,200)
    for route in ['/api/boss-dashboard?file=x','/api/boss-dashboard/../state.json','/boss.js?x=1']:
     with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(base+route)
     self.assertEqual(e.exception.code,404)
    for headers in [{'Host':'evil.invalid'},{'Origin':'http://evil.invalid'}]:
     with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(urllib.request.Request(base+'/api/boss-dashboard',headers=headers))
     self.assertEqual(e.exception.code,403)
    with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(urllib.request.Request(base+'/api/boss-dashboard',data=b'{}'))
    self.assertEqual(e.exception.code,405)
   finally:server.shutdown();server.server_close();thread.join()
if __name__=='__main__':unittest.main(verbosity=2)
