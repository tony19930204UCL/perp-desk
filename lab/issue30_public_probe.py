#!/usr/bin/env python3
"""Bounded Issue #30 public timing probe. Public GET only; no orders/private API."""
import hashlib,json,time
from urllib.parse import urlencode
from urllib.request import Request,urlopen

BASE='https://fapi.binance.com'
TARGETS=[
    ('/fapi/v1/time',{}),
    ('/fapi/v1/depth',{'symbol':'ETHUSDT','limit':5}),
    ('/fapi/v1/premiumIndex',{'symbol':'ETHUSDT'}),
]
def get(endpoint,params):
    url=BASE+endpoint+('?' + urlencode(params) if params else '')
    start=int(time.time()*1000)
    try:
        req=Request(url,headers={'User-Agent':'Issue30BoundedPublicProbe/1.0','Cache-Control':'no-cache'},method='GET')
        with urlopen(req,timeout=10) as response:
            raw=response.read();status=getattr(response,'status',response.getcode())
        receipt=int(time.time()*1000);payload=json.loads(raw)
        out={'outcome':'success','http_status':status,'request_start_ms':start,'receipt_ms':receipt,
             'elapsed_ms':receipt-start,'payload_sha256':hashlib.sha256(raw).hexdigest(),
             'payload_type':type(payload).__name__}
        if isinstance(payload,dict):
            out['payload_keys']=sorted(payload)
            for key in ('serverTime','E','T','time','lastUpdateId'):
                if key in payload:out[key]=payload[key]
            source=payload.get('time',payload.get('E',payload.get('serverTime')))
            if type(source) is int:
                out['source_minus_receipt_ms']=source-receipt
                out['source_minus_request_start_ms']=source-start
                out['requires_causal_wait']=source>receipt
                out['within_existing_future_bound']=source-receipt<=5000
        return out
    except Exception as exc:
        receipt=int(time.time()*1000)
        return {'outcome':'blocked','error_type':type(exc).__name__,'error':str(exc),
                'request_start_ms':start,'receipt_ms':receipt,'elapsed_ms':receipt-start}
def main():
    result={'label':'ISSUE30 BOUNDED PUBLIC ENGINEERING PROBE - NOT MARKET PERFORMANCE',
            'private_api':False,'orders':False,'clock_changed':False,'rounds':[]}
    for _ in range(2):
        row={}
        for endpoint,params in TARGETS:row[endpoint]=get(endpoint,params)
        result['rounds'].append(row)
    results=[v['outcome'] for row in result['rounds'] for v in row.values()]
    result['result']='success' if all(x=='success' for x in results) else 'blocked'
    print(json.dumps(result,sort_keys=True,separators=(',',':')))
if __name__=='__main__':main()
