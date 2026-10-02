import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.client import HTTPConnection
import dashboard

SCRATCH=Path('/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch')
class WorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.server=dashboard.make_server(0,self.root/'paper_status.json',self.root/'page.html')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.addCleanup(self.stop)
    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
    def get(self,headers=None,method='GET'):
        c=HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        c.request(method,'/api/work',headers=headers or {})
        r=c.getresponse(); data=r.read(); c.close(); return r.status,json.loads(data) if data.startswith(b'{') else data
    def test_work_reads_independently_of_missing_market_snapshot(self):
        now=datetime.now(timezone.utc).isoformat()
        task=dict(id='job-1',title='歷史暖機',state='running',current_step='RED 測試',next_step='GREEN 實作',updated_at=now,started_at=now,evidence=[])
        (self.root/'work_status.json').write_text(json.dumps(dict(schema_version=1,tasks=[task])))
        status,data=self.get(); self.assertEqual(status,200); self.assertEqual(data['tasks'][0]['next_step'],'GREEN 實作'); self.assertFalse(data['tasks'][0]['activity_unconfirmed'])
    def test_old_active_update_is_unconfirmed_not_completed(self):
        now=datetime.now(timezone.utc); old=(now-timedelta(minutes=6)).isoformat()
        task=dict(id='job-1',title='warmup',state='running',current_step='tests',next_step='deploy',updated_at=old,started_at=old,evidence=[])
        path=self.root/'work_status.json'; path.write_text(json.dumps(dict(schema_version=1,tasks=[task])))
        data=self.get()[1]; self.assertTrue(data['tasks'][0]['activity_unconfirmed']); self.assertEqual(data['tasks'][0]['state'],'running')
    def test_missing_invalid_and_security_fail_closed(self):
        self.assertEqual(self.get()[0],503)
        for value in ('{}','[]','{"schema_version":1,"tasks":[{}]}'):
            (self.root/'work_status.json').write_text(value); self.assertEqual(self.get()[0],503)
        self.assertEqual(self.get(headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.get(method='POST')[0],405)
    def test_update_is_persistent_preserves_other_tasks_and_evidence(self):
        from work_status import update,load_status
        path=self.root/'work_status.json'
        update(path,'one','工程','testing','測試','驗收',['evidence/test.txt'])
        update(path,'two','另一工作','queued','已排入','開始')
        update(path,'one','工程','completed','驗收通過','無待辦')
        data=load_status(path); self.assertEqual(len(data['tasks']),2); self.assertEqual(data['tasks'][0]['evidence'],['evidence/test.txt']); self.assertFalse(data['tasks'][0]['activity_unconfirmed'])
    def test_asset_shows_steps_and_separate_work_fetch_without_html_injection(self):
        html=(Path(__file__).resolve().parents[1]/'dashboard.html').read_text()
        self.assertIn('id="workTasks"',html); self.assertIn('/api/work',html)
        self.assertIn('activity_unconfirmed',html); self.assertIn('next_step',html)
        self.assertNotIn('innerHTML',html)
if __name__=='__main__': unittest.main()
