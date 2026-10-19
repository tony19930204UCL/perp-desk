#!/usr/bin/env python3
"""RESEARCH-R1 A: 從 data.binance.vision 下載 USD-M 期貨 1h klines 與 fundingRate (免費公開、無憑證)。
月檔 2023-10..2026-09，加 2026-10 日檔 (01..18)。每個 zip 以 .CHECKSUM 驗 sha256。
輸出: data/raw/<kind>/<SYM>/*.zip  與  data/<SYM>_1h.csv , data/<SYM>_funding.csv ; data/MANIFEST.json
"""
import csv, hashlib, io, json, os, sys, urllib.request, urllib.error, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
BASE = "https://data.binance.vision/data/futures/um"
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
START = (2023, 10)
END_M = (2026, 9)
DAILY_FROM, DAILY_TO = date(2026, 10, 1), date(2026, 10, 18)


def months():
    y, m = START
    while (y, m) <= END_M:
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def get(url):
    for i in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except Exception as e:  # noqa
            err = e
    raise RuntimeError(f"{url}: {err}")


def jobs():
    out = []
    for s in SYMS:
        for y, m in months():
            out.append(("monthly", "klines", s, f"{s}-1h-{y}-{m:02d}", f"{BASE}/monthly/klines/{s}/1h/{s}-1h-{y}-{m:02d}.zip"))
            out.append(("monthly", "fundingRate", s, f"{s}-fundingRate-{y}-{m:02d}", f"{BASE}/monthly/fundingRate/{s}/{s}-fundingRate-{y}-{m:02d}.zip"))
        d = DAILY_FROM
        while d <= DAILY_TO:
            out.append(("daily", "klines", s, f"{s}-1h-{d.isoformat()}", f"{BASE}/daily/klines/{s}/1h/{s}-1h-{d.isoformat()}.zip"))
            d += timedelta(days=1)
        # fundingRate 沒有 daily 檔 -> 2026-10 資金費率另由公開 REST 補 (見 fetch_funding_oct)
    return out


def do(job):
    freq, kind, sym, name, url = job
    d = os.path.join(DATA, "raw", kind, sym)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name + ".zip")
    if os.path.exists(p):
        return (name, "cached", None)
    z = get(url)
    if z is None:
        return (name, "404", None)
    cs = get(url + ".CHECKSUM")
    ok = None
    if cs:
        want = cs.decode().split()[0]
        ok = hashlib.sha256(z).hexdigest() == want
        if not ok:
            return (name, "CHECKSUM_MISMATCH", False)
    with open(p, "wb") as f:
        f.write(z)
    return (name, "ok", ok)


def parse_klines(sym):
    rows = {}
    d = os.path.join(DATA, "raw", "klines", sym)
    for fn in sorted(os.listdir(d)):
        with zipfile.ZipFile(os.path.join(d, fn)) as zf:
            for n in zf.namelist():
                for line in io.TextIOWrapper(zf.open(n)):
                    p = line.strip().split(",")
                    if not p or not p[0][0].isdigit():
                        continue
                    t = int(p[0])
                    # open_time,open,high,low,close,volume,close_time,quote_vol,trades,tb_base,tb_quote,ignore
                    rows[t] = (t, float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5]), float(p[7]), int(p[8]))
    out = sorted(rows.values())
    with open(os.path.join(DATA, f"{sym}_1h.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open_time_ms", "open", "high", "low", "close", "volume", "quote_volume", "trades"])
        w.writerows(out)
    return out


def parse_funding(sym):
    rows = {}
    d = os.path.join(DATA, "raw", "fundingRate", sym)
    for fn in sorted(os.listdir(d)):
        with zipfile.ZipFile(os.path.join(d, fn)) as zf:
            for n in zf.namelist():
                for line in io.TextIOWrapper(zf.open(n)):
                    p = line.strip().split(",")
                    if not p or not p[0][0].isdigit():
                        continue
                    rows[int(p[0])] = float(p[2])  # calc_time, funding_interval_hours, last_funding_rate
    return rows


def fetch_funding_oct(sym, since_ms):
    """月檔結束後的資金費率 (2026-10) 用公開 REST /fapi/v1/fundingRate (免金鑰)。"""
    out = {}
    start = since_ms
    while True:
        u = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}&startTime={start}&limit=1000"
        b = get(u)
        arr = json.loads(b) if b else []
        if not arr:
            break
        for r in arr:
            out[int(r["fundingTime"])] = float(r["fundingRate"])
        start = int(arr[-1]["fundingTime"]) + 1
        if len(arr) < 1000:
            break
    return out


def main():
    os.makedirs(DATA, exist_ok=True)
    J = jobs()
    stat = {}
    with ThreadPoolExecutor(8) as ex:
        for name, st, ok in ex.map(do, J):
            stat.setdefault(st, []).append(name)
    print({k: len(v) for k, v in stat.items()})
    if "CHECKSUM_MISMATCH" in stat:
        print("CHECKSUM MISMATCH", stat["CHECKSUM_MISMATCH"])
        sys.exit(1)
    manifest = {"jobs": len(J), "status_counts": {k: len(v) for k, v in stat.items()}, "404": stat.get("404", []), "symbols": {}}
    for s in SYMS:
        k = parse_klines(s)
        f = parse_funding(s)
        last_f = max(f) if f else 0
        extra = fetch_funding_oct(s, last_f + 1)
        f.update(extra)
        with open(os.path.join(DATA, f"{s}_funding.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["funding_time_ms", "rate"])
            for t in sorted(f):
                w.writerow([t, f[t]])
        ts = [r[0] for r in k]
        gaps = [(ts[i - 1], ts[i]) for i in range(1, len(ts)) if ts[i] - ts[i - 1] != 3600000]
        manifest["symbols"][s] = {
            "bars": len(k), "first": ts[0], "last": ts[-1],
            "gaps_n": len(gaps), "gaps_first5": gaps[:5],
            "funding_rows": len(f), "funding_rest_extra": len(extra),
            "bad_ohlc": sum(1 for r in k if not (r[3] <= min(r[1], r[4]) and r[2] >= max(r[1], r[4]) and r[3] > 0)),
            "zero_volume_bars": sum(1 for r in k if r[5] == 0),
        }
    with open(os.path.join(DATA, "MANIFEST.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print(json.dumps(manifest, indent=1)[:3000])


if __name__ == "__main__":
    main()
