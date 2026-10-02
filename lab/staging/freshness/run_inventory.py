"""Run frozen staged test inventory in bounded batches, recording exact IDs."""
import argparse
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent
EVIDENCE=ROOT.parents[1]/'evidence'
sys.path.insert(0,str(ROOT/'tests'))

def flatten(suite):
    for item in suite:
        if isinstance(item,unittest.TestSuite): yield from flatten(item)
        else: yield item

class RecordedResult(unittest.TextTestResult):
    def __init__(self,*a,**kw):
        super().__init__(*a,**kw); self.passed=[]; self.started=[]
    def startTest(self,test):
        self.started.append(test.id()); super().startTest(test)
    def addSuccess(self,test):
        self.passed.append(test.id()); super().addSuccess(test)

parser=argparse.ArgumentParser()
parser.add_argument('--inventory',action='store_true')
parser.add_argument('--batch',type=int)
args=parser.parse_args()
if args.inventory:
    tests=list(flatten(unittest.defaultTestLoader.discover(str(ROOT/'tests'))))
    ids=sorted(t.id() for t in tests)
    assert len(ids)==len(set(ids))
    data={'ids':ids,'batches':[ids[i:i+20] for i in range(0,len(ids),20)]}
    (EVIDENCE/'freshness_inventory.json').write_text(json.dumps(data,indent=2))
    print(json.dumps({'count':len(ids),'batches':len(data['batches'])}))
else:
    data=json.loads((EVIDENCE/'freshness_inventory.json').read_text())
    ids=data['batches'][args.batch-1]
    suite=unittest.defaultTestLoader.loadTestsFromNames(ids)
    begin=time.monotonic()
    with (EVIDENCE/f'freshness_batch{args.batch}.txt').open('w') as output:
        result=unittest.TextTestRunner(stream=output,verbosity=2,resultclass=RecordedResult).run(suite)
    record={'batch':args.batch,'expected':ids,'started':result.started,'passed':result.passed,
            'elapsed_seconds':time.monotonic()-begin,'success':result.wasSuccessful(),
            'failures':[t.id() for t,trace in result.failures],
            'errors':[t.id() for t,trace in result.errors]}
    (EVIDENCE/f'freshness_batch{args.batch}.json').write_text(json.dumps(record,indent=2))
    print(json.dumps(record))
    sys.exit(0 if record['success'] and sorted(record['passed'])==ids else 1)
