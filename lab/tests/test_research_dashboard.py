import json,os,sqlite3,tempfile,threading,unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from http.client import HTTPConnection

import dashboard
from research_dashboard import load_research_config,read_research

LAB=Path(__file__).resolve().parents[1]

def broker(path,cash,initial='100',positions=None,marks=None,pending=0):
    positions=positions or {};marks=marks or {}
    orders={}
    for i in range(pending):
        orders['o'+str(i)]={'status':'RESTING','intent':{'reduce_only':False}}
    state=dict(meta={},cash=str(cash),initial_cash=str(initial),orders=orders,positions=positions,
               fills=[],ledger=[],audit=[],marks=marks,funding_events={},seen_events={},
               funding_status={},day_baselines={},last_ts=0)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sim_broker_state(singleton INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
        db.execute('INSERT INTO sim_broker_state VALUES(1,?)',(json.dumps(state),))

def feed(path,source,receipt,dispatch,kind='book'):
    event=dict(type=kind,event_id=kind+':1',source_ts=source,receipt_ts=receipt,ts=dispatch,source_valid=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE feed_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)')
        db.execute('INSERT INTO feed_events(event_id,payload,sha256) VALUES(?,?,?)',('e1',json.dumps(event),'0'*64))

def report(now):
    def arm(a):
        return dict(cash={'A':'101','B':'99.5','C':'102'}[a],initial_cash='100',positions=1 if a!='B' else 0,
                    raw_candidates={'A':5,'B':6,'C':7}[a],cost_qualified={'A':4,'B':4,'C':5}[a],
                    submitted={'A':2,'B':3,'C':4}[a],filled_entry_orders={'A':2,'B':1,'C':3}[a],
                    flat_to_flat_count={'A':1,'B':0,'C':2}[a],risk_rejections=1,fees_usdt='-0.020000000000000000',
                    funding_pnl_usdt='0.001',gross_realized_pnl_usdt='1.1',net_ledger_usdt={'A':'1.001','B':'-0.501','C':'2.001'}[a],
                    checkpoint=dict(arm=a,covered=440,expected=480,coverage='0.916666666666666666',
                                    status='pending',stop_new_entries=False),
                    queue_stress_2x={'q':1} if a=='B' else {},
                    queue_stress_sensitivity=[{'status':'WAITING'},{'status':'PARTIAL'}] if a=='B' else [],
                    direction_counts={'long':{'raw':4,'cost_qualified':3},'short':{'raw':3,'cost_qualified':2}},
                    expired=0,nonfilled=0,protective_taker_orders=0,completed_episode_metrics=[],open_episode_metric=None)
    return dict(version_id='ETH-DISCOVERY-LAB-001',activated=True,start_ms=now-3600000,deadline_ms=now+47*3600000,
                shared_window=True,capital_pooled=False,default_account_touched=False,
                storage_used_bytes=1024,storage_budget_bytes=33554432,source_gaps=0,unknown_inputs=0,
                operator_stop_requested=False,causal_evidence={},paired_ab=[],
                runner=dict(polls=8,last_poll_ms=now-1000,last_error=None,poll_failures=2,last_failure_ms=now-10000,
                            source_failure=False,feed={}),arms={a:arm(a) for a in 'ABC'})

class ResearchFixture:
    def __init__(self,td):
        self.root=Path(td);now=int(datetime.now(timezone.utc).timestamp()*1000);self.now=now
        (self.root/'reports').mkdir();self.report=self.root/'reports/latest.json';self.report.write_text(json.dumps(report(now)))
        feed(self.root/'shared-feed.sqlite3',now-900,now-800,now-700)
        broker(self.root/'arm-A.sqlite3','101',positions={'ETHUSDT':{'qty':'1','entry':'100'}},marks={'ETHUSDT':'101'},pending=1)
        broker(self.root/'arm-B.sqlite3','99.5',positions={},marks={'ETHUSDT':'101'},pending=2)
        broker(self.root/'arm-C.sqlite3','102',positions={'ETHUSDT':{'qty':'-1','entry':'103'}},marks={'ETHUSDT':'101'},pending=0)
        self.proof=self.root/'process.json';self.proof.write_text(json.dumps(dict(schema_version=1,active=True,observed_at_ms=now-500,proof_kind='owned_process')))
        self.cfg=self.root/'research.json';self.cfg.write_text(json.dumps(dict(schema_version=1,root=str(self.root.resolve()),current=dict(
            id='eth-discovery',label='ETH-DISCOVERY-LAB-001',fresh_seconds=15,report='reports/latest.json',
            feed_db='shared-feed.sqlite3',process_proof='process.json',
            arms={'A':'arm-A.sqlite3','B':'arm-B.sqlite3','C':'arm-C.sqlite3'}))))

class ResearchDashboardTests(unittest.TestCase):
    def setUp(self):
        self.td=tempfile.TemporaryDirectory();self.addCleanup(self.td.cleanup);self.f=ResearchFixture(self.td.name)

    def test_three_arms_are_isolated_and_2x_is_diagnostic_only(self):
        d=read_research(load_research_config(self.f.cfg),datetime.fromtimestamp(self.f.now/1000,timezone.utc))
        self.assertTrue(d['available']);self.assertEqual(d['state'],'fresh');self.assertFalse(d['capital_pooled'])
        self.assertEqual(set(d['arms']),set('ABC'))
        self.assertEqual(d['arms']['A']['account']['equity'],'102')
        self.assertEqual(d['arms']['B']['account']['pending_orders'],2)
        self.assertEqual(d['arms']['C']['account']['positions'][0]['direction'],'short')
        self.assertEqual(d['arms']['C']['account']['equity'],'104')
        self.assertEqual(d['arms']['A']['net_ledger_usdt'],'1.001')
        self.assertEqual(d['arms']['B']['net_ledger_usdt'],'-0.501')
        self.assertIn('not an account',d['queue_diagnostic_role'])
        self.assertEqual(len(d['arms']['B']['queue_stress_sensitivity']),2)

    def test_missing_marks_make_equity_unknown_not_cash(self):
        broker_path=self.f.root/'arm-A.sqlite3';broker_path.unlink()
        broker(broker_path,'101',positions={'ETHUSDT':{'qty':'1','entry':'100'}},marks={},pending=0)
        d=read_research(load_research_config(self.f.cfg),datetime.fromtimestamp(self.f.now/1000,timezone.utc))
        self.assertIsNone(d['arms']['A']['account']['equity']);self.assertFalse(d['arms']['A']['account']['equity_confirmed'])

    def test_fresh_stale_future_source_gap_storage_and_process_states_are_distinct(self):
        cfg=load_research_config(self.f.cfg)
        d=read_research(cfg,datetime.fromtimestamp((self.f.now+20000)/1000,timezone.utc));self.assertEqual(d['state'],'stale')
        d=read_research(cfg,datetime.fromtimestamp((self.f.now-5000)/1000,timezone.utc));self.assertEqual(d['state'],'future')
        raw=json.loads(self.f.report.read_text());raw['runner']['source_failure']=True;raw['source_gaps']=3
        raw['storage_used_bytes']=31_000_000;self.f.report.write_text(json.dumps(raw))
        d=read_research(cfg,datetime.fromtimestamp(self.f.now/1000,timezone.utc))
        self.assertEqual(d['state'],'source_gap');self.assertIn('source_gaps_recorded',d['blockers']);self.assertIn('storage_entry_stop',d['blockers'])
        self.proof=self.f.proof;self.proof.write_text(json.dumps(dict(schema_version=1,active=True,observed_at_ms=self.f.now-60000,proof_kind='owned_process')))
        d=read_research(cfg,datetime.fromtimestamp(self.f.now/1000,timezone.utc));self.assertFalse(d['process_proof']['confirmed']);self.assertEqual(d['process_proof']['state'],'stale')

    def test_report_future_and_source_future_are_distinct_fail_closed_states(self):
        cfg=load_research_config(self.f.cfg)
        future=(self.f.now+20_000)/1000
        os.utime(self.f.report,(future,future))
        d=read_research(cfg,datetime.fromtimestamp(self.f.now/1000,timezone.utc))
        self.assertEqual(d['report_state'],'future');self.assertEqual(d['state'],'future')
        self.assertIn('report_future',d['blockers'])
        os.utime(self.f.report,None)
        with sqlite3.connect(self.f.root/'shared-feed.sqlite3') as db:
            event=dict(type='book',event_id='book:future',source_ts=self.f.now+10_000,
                       receipt_ts=self.f.now,ts=self.f.now,source_valid=False)
            db.execute('INSERT INTO feed_events(event_id,payload,sha256) VALUES(?,?,?)',
                       ('future',json.dumps(event),'1'*64))
        d=read_research(cfg,datetime.fromtimestamp(self.f.now/1000,timezone.utc))
        self.assertIn('source_evidence_future',d['blockers'])

    def test_missing_and_malformed_are_honest_and_no_paths_leak(self):
        cfg=load_research_config(self.f.cfg);self.f.report.unlink()
        d=read_research(cfg);self.assertEqual(d['state'],'missing');self.assertNotIn(str(self.f.root),json.dumps(d))
        self.f.report.write_text('{}');d=read_research(cfg);self.assertEqual(d['state'],'malformed');self.assertNotIn(str(self.f.root),json.dumps(d))

    def test_config_rejects_escape_and_symlink(self):
        data=json.loads(self.f.cfg.read_text());data['current']['report']='../outside.json';self.f.cfg.write_text(json.dumps(data))
        with self.assertRaises(ValueError):load_research_config(self.f.cfg)
        data['current']['report']='reports/link.json';target=self.f.root/'target.json';target.write_text('{}');(self.f.root/'reports/link.json').symlink_to(target)
        self.f.cfg.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'symlink'):load_research_config(self.f.cfg)

    def test_http_research_is_readonly_and_default_status_binding_unchanged(self):
        status=self.f.root/'status.json'
        from test_dashboard import fixture
        status.write_text(json.dumps(fixture()))
        before={p:p.read_bytes() for p in [status,self.f.report,self.f.root/'arm-A.sqlite3',self.f.root/'arm-B.sqlite3',self.f.root/'arm-C.sqlite3']}
        server=dashboard.make_server(0,status,LAB/'dashboard.html',None,self.f.cfg.resolve())
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        try:
            c=HTTPConnection('127.0.0.1',server.server_port,timeout=3);c.request('GET','/api/research');r=c.getresponse();d=json.loads(r.read());c.close()
            self.assertEqual(r.status,200);self.assertEqual(d['label'],'ETH-DISCOVERY-LAB-001')
            c=HTTPConnection('127.0.0.1',server.server_port,timeout=3);c.request('GET','/api/ledgers');r=c.getresponse();led=json.loads(r.read());c.close()
            self.assertEqual(led['default'],'default');self.assertEqual([x['id'] for x in led['ledgers']],['default'])
        finally:server.shutdown();server.server_close();t.join()
        for p,b in before.items():self.assertEqual(p.read_bytes(),b)

if __name__=='__main__':unittest.main()
