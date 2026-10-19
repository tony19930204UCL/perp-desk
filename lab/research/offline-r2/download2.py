#!/usr/bin/env python3
"""RESEARCH-R2 A: 下載 20 幣 USD-M 1d klines + fundingRate + BTCUSDT 15m klines (data.binance.vision，免費公開、無憑證)。
宇宙規則見 PREREG.md；universe.json 由 exchangeInfo + 24hr ticker 機械選出 (onboardDate<2023-09-01, USDT 永續, TRADING, 非 USDC, 24h quoteVolume 前 20)。
輸出: data/raw/... 、 data/<SYM>_1d.csv 、 data/<SYM>_funding.csv 、 data/BTCUSDT_15m.csv 、 data/MANIFEST.json
"""
import csv, datetime, hashlib, io, json, os, sys, urllib.request, urllib.error, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
BASE = "https://data.binance.vision/data/futures/um"
START, END_M = (2023, 10), (2026, 9)
D_FROM, D_TO = date(2026, 10, 1), date(2026, 10, 18)
M15_START = (2025, 10)


FIXED_UNIVERSE = [{"symbol": "BTCUSDT", "quoteVolume24h": 10510410233.78}, {"symbol": "ETHUSDT", "quoteVolume24h": 7237296451.52}, {"symbol": "ZECUSDT", "quoteVolume24h": 1732457648.62}, {"symbol": "SOLUSDT", "quoteVolume24h": 1410120654.5477}, {"symbol": "NEARUSDT", "quoteVolume24h": 665310681.093}, {"symbol": "XRPUSDT", "quoteVolume24h": 617664474.3903}, {"symbol": "DOGEUSDT", "quoteVolume24h": 321492980.64343}, {"symbol": "WLDUSDT", "quoteVolume24h": 243091285.045}, {"symbol": "BNBUSDT", "quoteVolume24h": 239787768.967}, {"symbol": "SUIUSDT", "quoteVolume24h": 203227528.69852}, {"symbol": "1000PEPEUSDT", "quoteVolume24h": 197060340.3796208}, {"symbol": "UNIUSDT", "quoteVolume24h": 169049312.905}, {"symbol": "GTCUSDT", "quoteVolume24h": 155606112.014726}, {"symbol": "FILUSDT", "quoteVolume24h": 140179861.2545}, {"symbol": "SANDUSDT", "quoteVolume24h": 136004778.69031}, {"symbol": "INJUSDT", "quoteVolume24h": 128534972.8565}, {"symbol": "ADAUSDT", "quoteVolume24h": 126308423.6089}, {"symbol": "AVAXUSDT", "quoteVolume24h": 109496587.282}, {"symbol": "LINKUSDT", "quoteVolume24h": 102301555.153}, {"symbol": "ARBUSDT", "quoteVolume24h": 95044229.336512}]


def get(url):
    err = None
    for i in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except Exception as e:
            err = e
    raise RuntimeError(f"{url}: {err}")


def gj(u):
    return json.loads(get(u))


def months(start, end=END_M):
    y, m = start
    while (y, m) <= end:
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def select_universe():
    ei = gj("https://fapi.binance.com/fapi/v1/exchangeInfo")
    tk = {x["symbol"]: float(x["quoteVolume"]) for x in gj("https://fapi.binance.com/fapi/v1/ticker/24hr")}
    cut = int(datetime.datetime(2023, 9, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    c = []
    for s in ei["symbols"]:
        if s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT" and s["status"] == "TRADING" and s["onboardDate"] < cut and s["symbol"] != "USDCUSDT":
            c.append((tk.get(s["symbol"], 0), s["symbol"]))
    c.sort(reverse=True)
    return [{"symbol": s, "quoteVolume24h": v} for v, s in c[:20]], len(c)


def jobs(syms):
    out = []
    for s in syms:
        for y, m in months(START):
            out.append(("klines1d", s, f"{s}-1d-{y}-{m:02d}", f"{BASE}/monthly/klines/{s}/1d/{s}-1d-{y}-{m:02d}.zip"))
            out.append(("fundingRate", s, f"{s}-fundingRate-{y}-{m:02d}", f"{BASE}/monthly/fundingRate/{s}/{s}-fundingRate-{y}-{m:02d}.zip"))
        d = D_FROM
        while d <= D_TO:
            out.append(("klines1d", s, f"{s}-1d-{d.isoformat()}", f"{BASE}/daily/klines/{s}/1d/{s}-1d-{d.isoformat()}.zip"))
            d += timedelta(days=1)
    s = "BTCUSDT"
    for y, m in months(M15_START):
        out.append(("klines15m", s, f"{s}-15m-{y}-{m:02d}", f"{BASE}/monthly/klines/{s}/15m/{s}-15m-{y}-{m:02d}.zip"))
    d = D_FROM
    while d <= D_TO:
        out.append(("klines15m", s, f"{s}-15m-{d.isoformat()}", f"{BASE}/daily/klines/{s}/15m/{s}-15m-{d.isoformat()}.zip"))
        d += timedelta(days=1)
    return out


def do(job):
    kind, sym, name, url = job
    d = os.path.join(DATA, "raw", kind, sym)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name + ".zip")
    if os.path.exists(p):
        return (name, "cached")
    z = get(url)
    if z is None:
        return (name, "404")
    cs = get(url + ".CHECKSUM")
    if cs and hashlib.sha256(z).hexdigest() != cs.decode().split()[0]:
        return (name, "CHECKSUM_MISMATCH")
    with open(p, "wb") as f:
        f.write(z)
    return (name, "ok" if cs else "ok_nochecksum")


def parse(kind, sym, tag):
    rows = {}
    d = os.path.join(DATA, "raw", kind, sym)
    for fn in sorted(os.listdir(d)):
        with zipfile.ZipFile(os.path.join(d, fn)) as zf:
            for n in zf.namelist():
                for line in io.TextIOWrapper(zf.open(n)):
                    p = line.strip().split(",")
                    if not p or not p[0][0].isdigit():
                        continue
                    t = int(p[0])
                    if kind == "fundingRate":
                        rows[t] = (t, float(p[2]))
                    else:
                        rows[t] = (t, float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5]), float(p[7]), int(p[8]))
    out = sorted(rows.values())
    with open(os.path.join(DATA, f"{sym}_{tag}.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["funding_time_ms", "rate"] if kind == "fundingRate" else ["open_time_ms", "open", "high", "low", "close", "volume", "quote_volume", "trades"])
        w.writerows(out)
    return out


def fund_rest(sym, since):
    out, start = {}, since
    while True:
        arr = gj(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}&startTime={start}&limit=1000")
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
    # 宇宙於 2026-10-19 以 select_universe() 機械選出並在看報酬前寫入 PREREG.md；此處固定使用該快照，避免 24h 成交額漂移
    uni, n_elig = FIXED_UNIVERSE, 140
    json.dump({"rule": "USDT PERPETUAL TRADING onboardDate<2023-09-01 not USDCUSDT, top20 by 24h quoteVolume at selection time",
               "selected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "n_eligible": n_elig, "universe": uni},
              open(os.path.join(DATA, "universe.json"), "w"), indent=1)
    syms = [u["symbol"] for u in uni]
    J = jobs(syms)
    stat = {}
    with ThreadPoolExecutor(16) as ex:
        for name, st in ex.map(do, J):
            stat.setdefault(st, []).append(name)
    print({k: len(v) for k, v in stat.items()})
    if "CHECKSUM_MISMATCH" in stat:
        print("CHECKSUM MISMATCH", stat["CHECKSUM_MISMATCH"]); sys.exit(1)
    man = {"jobs": len(J), "status_counts": {k: len(v) for k, v in stat.items()}, "404": stat.get("404", []), "symbols": {}}
    for s in syms:
        k = parse("klines1d", s, "1d")
        f = {t: r for t, r in parse("fundingRate", s, "funding")}
        extra = fund_rest(s, max(f) + 1)
        f.update(extra)
        with open(os.path.join(DATA, f"{s}_funding.csv"), "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["funding_time_ms", "rate"])
            for t in sorted(f): w.writerow([t, f[t]])
        ts = [r[0] for r in k]
        gaps = [(ts[i - 1], ts[i]) for i in range(1, len(ts)) if ts[i] - ts[i - 1] != 86400000]
        man["symbols"][s] = {"bars_1d": len(k), "first": ts[0], "last": ts[-1], "gaps_n": len(gaps), "gaps_first5": gaps[:5],
                             "funding_rows": len(f), "funding_rest_extra": len(extra),
                             "bad_ohlc": sum(1 for r in k if not (r[3] <= min(r[1], r[4]) and r[2] >= max(r[1], r[4]) and r[3] > 0))}
    k = parse("klines15m", "BTCUSDT", "15m")
    ts = [r[0] for r in k]
    gaps = [(ts[i - 1], ts[i]) for i in range(1, len(ts)) if ts[i] - ts[i - 1] != 900000]
    man["BTCUSDT_15m"] = {"bars": len(k), "first": ts[0], "last": ts[-1], "gaps_n": len(gaps), "gaps_first5": gaps[:5]}
    json.dump(man, open(os.path.join(DATA, "MANIFEST.json"), "w"), indent=1)
    print(json.dumps(man, indent=1)[:3500])


if __name__ == "__main__":
    main()
