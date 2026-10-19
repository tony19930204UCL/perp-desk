#!/usr/bin/env python3
"""RESEARCH-R4 資料下載 (data.binance.vision, 免費公開、無憑證)。純 stdlib。逐檔 .CHECKSUM sha256 驗證。
metrics: data/futures/um/daily/metrics/<SYM>/<SYM>-metrics-YYYY-MM-DD.zip  (2023-10-01 .. 2026-10-18)
         欄位原樣保存: create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,
         sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio
premium: premiumIndexKlines 1h 月檔 2023-10..2026-09 + 2026-10 日檔 (至 10-18)。
輸出: data/metrics/<SYM>.csv(原樣列)、data/premium/<SYM>_1h.csv、MANIFEST_metrics.json、MANIFEST_premium.json
用法: python3 download4.py metrics | premium
"""
import csv, hashlib, http.client, io, json, os, sys, threading, urllib.parse, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CDN = "https://data.binance.vision"
PFX = "data/futures/um"
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
D0 = date(2023, 10, 1)
LAST_DAY = date(2026, 10, 18)
_tl = threading.local()


def get(url, tries=6):
    u = urllib.parse.urlsplit(url)
    err = None
    for a in range(tries):
        conns = getattr(_tl, "c", None)
        if conns is None:
            conns = _tl.c = {}
        try:
            c = conns.get(u.netloc)
            if c is None:
                c = conns[u.netloc] = http.client.HTTPSConnection(u.netloc, timeout=60)
            c.request("GET", u.path)
            r = c.getresponse()
            b = r.read()
            if r.status == 404:
                return None
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            return b
        except Exception as e:
            err = e
            try:
                conns.pop(u.netloc).close()
            except Exception:
                pass
    raise RuntimeError(f"{url}: {err}")


def fetch_verified(key):
    z = get(f"{CDN}/{key}")
    if z is None:
        return None, "404", None
    ck = get(f"{CDN}/{key}.CHECKSUM")
    if ck is None:
        return None, "no_checksum", None
    want = ck.decode().split()[0].lower()
    got = hashlib.sha256(z).hexdigest()
    if want != got:
        return None, "sha_mismatch", None
    with zipfile.ZipFile(io.BytesIO(z)) as zf:
        raw = zf.read(zf.namelist()[0]).decode()
    return raw.splitlines(), "ok", got


def days():
    d = D0
    while d <= LAST_DAY:
        yield d
        d += timedelta(days=1)


def one_metrics(args):
    sym, d = args
    key = f"{PFX}/daily/metrics/{sym}/{sym}-metrics-{d.isoformat()}.zip"
    lines, st, sha = fetch_verified(key)
    return sym, d, lines, st, sha


def run_metrics():
    jobs = [(s, d) for s in SYMS for d in days()]
    res = {s: {} for s in SYMS}
    man = {s: {"ok": 0, "missing_404": [], "bad": [], "header": None, "rows": 0} for s in SYMS}
    with ThreadPoolExecutor(8) as ex:
        for sym, d, lines, st, sha in ex.map(one_metrics, jobs):
            m = man[sym]
            if st == "404":
                m["missing_404"].append(d.isoformat()); continue
            if st != "ok":
                m["bad"].append((d.isoformat(), st)); continue
            m["ok"] += 1
            if m["header"] is None:
                m["header"] = lines[0]
            res[sym][d] = lines[1:]
    for s in SYMS:
        os.makedirs(os.path.join(DATA, "metrics"), exist_ok=True)
        with open(os.path.join(DATA, "metrics", f"{s}.csv"), "w") as f:
            f.write(man[s]["header"] + "\n")
            for d in sorted(res[s]):
                for ln in res[s][d]:
                    f.write(ln + "\n"); man[s]["rows"] += 1
    json.dump(man, open(os.path.join(DATA, "MANIFEST_metrics.json"), "w"), indent=1)
    print({s: (man[s]["ok"], len(man[s]["missing_404"]), len(man[s]["bad"]), man[s]["rows"]) for s in SYMS})


def months():
    out, y, m = [], 2023, 10
    while (y, m) <= (2026, 9):
        out.append(f"{y}-{m:02d}"); m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def one_premium(args):
    sym, key = args
    lines, st, sha = fetch_verified(key)
    return sym, key, lines, st


def run_premium():
    jobs = []
    for s in SYMS:
        for ym in months():
            jobs.append((s, f"{PFX}/monthly/premiumIndexKlines/{s}/1h/{s}-1h-{ym}.zip"))
        d = date(2026, 10, 1)
        while d <= LAST_DAY:
            jobs.append((s, f"{PFX}/daily/premiumIndexKlines/{s}/1h/{s}-1h-{d.isoformat()}.zip")); d += timedelta(days=1)
    rows = {s: {} for s in SYMS}
    man = {s: {"ok": 0, "missing_404": [], "bad": []} for s in SYMS}
    with ThreadPoolExecutor(8) as ex:
        for sym, key, lines, st in ex.map(one_premium, jobs):
            m = man[sym]
            if st == "404":
                m["missing_404"].append(key); continue
            if st != "ok":
                m["bad"].append((key, st)); continue
            m["ok"] += 1
            for ln in lines:
                p = ln.split(",")
                if p and p[0].strip().isdigit():
                    rows[sym][int(p[0])] = p[:5]
    for s in SYMS:
        os.makedirs(os.path.join(DATA, "premium"), exist_ok=True)
        with open(os.path.join(DATA, "premium", f"{s}_1h.csv"), "w", newline="") as f:
            w = csv.writer(f); w.writerow(["open_time", "open", "high", "low", "close"])
            for t in sorted(rows[s]):
                w.writerow(rows[s][t])
        man[s]["rows"] = len(rows[s])
    json.dump(man, open(os.path.join(DATA, "MANIFEST_premium.json"), "w"), indent=1)
    print({s: (man[s]["ok"], len(man[s]["missing_404"]), len(man[s]["bad"]), man[s]["rows"]) for s in SYMS})


if __name__ == "__main__":
    {"metrics": run_metrics, "premium": run_premium}[sys.argv[1]]()
