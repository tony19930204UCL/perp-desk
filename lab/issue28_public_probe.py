#!/usr/bin/env python3
"""Bounded public evidence probe for Issue #28. Public GET only; no trading."""
import hashlib,json,time
from datetime import datetime,timezone
from urllib.parse import urlencode
from urllib.request import Request,urlopen

BASE='https://fapi.binance.com'
TARGETS=[
    ('/fapi/v1/time',{}),
    ('/fapi/v1/depth',{'symbol':'ETHUSDT','limit':5}),
    ('/fapi/v1/premiumIndex',{'symbol':'ETHUSDT'}),
]
def iso(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat().replace('+00:00','Z')
def one(endpoint,params):
    url=BASE+endpoint+('?' + urlencode(params) if params else '')
    start=int(time.time()*1000)
    try:
        req=Request(url,headers={'User-Agent':'Issue28BoundedPublicProbe/1.0','Cache-Control':'no-cache'},method='GET')
        with urlopen(req,timeout=10) as response:
            raw=response.read();status=getattr(response,'status',response.getcode())
        end=int(time.time()*1000);payload=json.loads(raw)
        evidence={'outcome':'success','http_status':status,'request_start_ms':start,
                  'receipt_ms':end,'elapsed_ms':end-start,'payload_sha256':hashlib.sha256(raw).hexdigest(),
                  'payload_type':type(payload).__name__}
        if isinstance(payload,dict):
            evidence['payload_keys']=sorted(payload)
            for key in ('serverTime','E','T','time','lastUpdateId'):
                if key in payload:evidence[key]=payload[key]
        return evidence
    except Exception as exc:
        end=int(time.time()*1000)
        return {'outcome':'blocked','error_type':type(exc).__name__,'error':str(exc),
                'request_start_ms':start,'receipt_ms':end,'elapsed_ms':end-start}
def main():
    result={'label':'ISSUE28 BOUNDED PUBLIC ENGINEERING PROBE - NOT MARKET PERFORMANCE',
            'private_api':False,'orders':False,'clock_changed':False,'targets':{}}
    for endpoint,params in TARGETS:
        result['targets'][endpoint]=one(endpoint,params)
    outcomes=[v['outcome'] for v in result['targets'].values()]
    result['result']='success' if all(x=='success' for x in outcomes) else 'blocked'
    print(json.dumps(result,sort_keys=True,separators=(',',':')))
if __name__=='__main__':main()
