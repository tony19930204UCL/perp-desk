#!/usr/bin/env python3
"""RESEARCH-R1 D: Deribit 公開 API DVOL(波動率指數) 1h 歷史，免金鑰。分頁往回抓到 2023-10-01。輸出 data/DVOL_{BTC,ETH}_1h.csv"""
import urllib.request, json, time, csv, os
D=os.path.join(os.path.dirname(os.path.abspath(__file__)),'data')
def g(u):
    for i in range(4):
        try: return json.loads(urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'Mozilla/5.0'}),timeout=40).read())
        except Exception as e: err=e; time.sleep(2)
    raise RuntimeError(err)
for cur in ('BTC','ETH'):
    rows={}; end=int(time.time()*1000); calls=0
    while True:
        j=g(f"https://www.deribit.com/api/v2/public/get_volatility_index_data?currency={cur}&start_timestamp=1696118400000&end_timestamp={end}&resolution=3600"); calls+=1
        r=j['result']; 
        for t,o,h,l,c in r['data']: rows[t]=(o,h,l,c)
        cont=r.get('continuation')
        if not cont or calls>60: break
        end=cont; time.sleep(0.2)
    ts=sorted(rows)
    with open(f"{D}/DVOL_{cur}_1h.csv",'w',newline='') as f:
        w=csv.writer(f); w.writerow(['open_time_ms','open','high','low','close'])
        for t in ts: w.writerow([t,*rows[t]])
    gaps=sum(1 for i in range(1,len(ts)) if ts[i]-ts[i-1]!=3600000)
    print(cur,'calls',calls,'rows',len(ts),'first',ts[0],'last',ts[-1],'gaps',gaps)
