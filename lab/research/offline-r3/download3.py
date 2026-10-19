#!/usr/bin/env python3
"""RESEARCH-R3 資料下載 (data.binance.vision, 免費公開、無憑證)。純 stdlib。
A: 全部 USD-M USDT 永續 (含已下架) 的 1d klines (月檔 + 當月日檔)，逐檔 .CHECKSUM sha256 驗證。
   輸出 data/1d/<SYM>.csv (open_time,open,high,low,close,volume,quote_volume)、data/MANIFEST_1d.json
B: BTC/ETH/SOL/XRP 1m klines 2023-10..2026-09 月檔 + 2026-10 日檔。輸出 data/1m/<SYM>.csv、data/MANIFEST_1m.json
用法: python3 download3.py 1d | 1m
"""
import csv, hashlib, io, json, os, re, sys, urllib.request, urllib.error, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
CDN = "https://data.binance.vision"
PFX = "data/futures/um"
LAST_DAY = date(2026, 10, 18)


import http.client, threading, urllib.parse
_tl = threading.local()


def get(url, tries=6):
    """持久連線 (每執行緒每 host 一條)，404 回 None。"""
    u = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(u.path, safe="/%:") + ("?" + urllib.parse.quote(u.query, safe="=&/%:") if u.query else "")
    err = None
    for a in range(tries):
        conns = getattr(_tl, "c", None)
        if conns is None:
            conns = _tl.c = {}
        try:
            c = conns.get(u.netloc)
            if c is None:
                c = conns[u.netloc] = http.client.HTTPSConnection(u.netloc, timeout=60)
            c.request("GET", path)
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


def s3_list(prefix, delimiter=True):
    """回傳 (common_prefixes, keys)。處理分頁 (marker)。"""
    cps, keys, marker = [], [], ""
    while True:
        u = f"{S3}?prefix={prefix}&max-keys=1000" + ("&delimiter=/" if delimiter else "") + (f"&marker={marker}" if marker else "")
        x = get(u).decode()
        cps += re.findall(r"<CommonPrefixes><Prefix>([^<]+)</Prefix>", x)
        keys += re.findall(r"<Key>([^<]+)</Key>", x)
        if "<IsTruncated>true</IsTruncated>" not in x:
            break
        nm = re.search(r"<NextMarker>([^<]+)</NextMarker>", x)
        marker = nm.group(1) if nm else (keys[-1] if keys else cps[-1])
    return cps, keys


def fetch_verified(key):
    """下載 zip 與 .CHECKSUM，驗 sha256。回傳 (rows|None, status)。"""
    z = get(f"{CDN}/{key}")
    if z is None:
        return None, "404"
    ck = get(f"{CDN}/{key}.CHECKSUM")
    if ck is None:
        return None, "no_checksum"
    want = ck.decode().split()[0].lower()
    got = hashlib.sha256(z).hexdigest()
    if want != got:
        return None, "sha_mismatch"
    with zipfile.ZipFile(io.BytesIO(z)) as zf:
        raw = zf.read(zf.namelist()[0]).decode()
    rows = []
    for ln in raw.splitlines():
        p = ln.split(",")
        if not p or not p[0].strip().isdigit():
            continue
        rows.append((int(p[0]), p[1], p[2], p[3], p[4], p[5], p[7]))   # t,o,h,l,c,vol,quote_vol
    return rows, "ok"


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open_time", "open", "high", "low", "close", "volume", "quote_volume"])
        w.writerows(rows)


def do_symbol_1d(sym):
    mp = os.path.join(DATA, "1d_meta", f"{sym}.json")
    if os.path.exists(mp):
        return sym, json.load(open(mp))
    sym, info = _do_symbol_1d(sym)
    os.makedirs(os.path.dirname(mp), exist_ok=True)
    json.dump(info, open(mp, "w"))
    return sym, info


def _do_symbol_1d(sym):
    rows, st = {}, {"ok": 0, "bad": []}
    # 月檔列表
    _, mk = s3_list(f"{PFX}/monthly/klines/{sym}/1d/")
    _, dk = s3_list(f"{PFX}/daily/klines/{sym}/1d/")
    keys = [k for k in mk + dk if k.endswith(".zip")]
    # 若某月已有月檔，忽略該月日檔 (避免重複)
    months = {re.search(r"-(\d{4}-\d{2})\.zip$", k).group(1) for k in mk if re.search(r"-(\d{4}-\d{2})\.zip$", k)}
    use = []
    for k in keys:
        m = re.search(r"-(\d{4}-\d{2})-\d{2}\.zip$", k)
        if m and m.group(1) in months:
            continue
        use.append(k)
    for k in sorted(use):
        r, s = fetch_verified(k)
        if r is None:
            st["bad"].append((k, s)); continue
        st["ok"] += 1
        for x in r:
            rows[x[0]] = x
    out = [rows[t] for t in sorted(rows)]
    if out:
        write_csv(os.path.join(DATA, "1d", f"{sym}.csv"), out)
    return sym, {"files_ok": st["ok"], "files_bad": st["bad"], "rows": len(out),
                 "first": out[0][0] if out else None, "last": out[-1][0] if out else None}


def run_1d():
    cps, _ = s3_list(f"{PFX}/monthly/klines/")
    syms_all = sorted(c.rstrip("/").split("/")[-1] for c in cps)
    syms = [s for s in syms_all if s.endswith("USDT") and "_" not in s]
    print("symbols total", len(syms_all), "USDT non-delivery", len(syms), flush=True)
    man = {"symbols_listed_total": len(syms_all), "symbols_used": len(syms), "excluded": [s for s in syms_all if s not in syms], "per_symbol": {}}
    with ThreadPoolExecutor(64) as ex:
        for i, (sym, info) in enumerate(ex.map(do_symbol_1d, syms)):
            man["per_symbol"][sym] = info
            if i % 25 == 0:
                print(i, sym, info["rows"], len(info["files_bad"]), flush=True)
    bad = {s: v["files_bad"] for s, v in man["per_symbol"].items() if v["files_bad"]}
    man["n_files_bad"] = sum(len(v) for v in bad.values())
    man["files_bad_detail"] = bad
    json.dump(man, open(os.path.join(DATA, "MANIFEST_1d.json"), "w"), indent=1)
    print("DONE 1d symbols_with_rows", sum(1 for v in man["per_symbol"].values() if v["rows"]), "bad files", man["n_files_bad"], flush=True)


def run_1m():
    syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
    jobs = []
    for s in syms:
        y, m = 2023, 10
        while (y, m) <= (2026, 9):
            jobs.append((s, f"{PFX}/monthly/klines/{s}/1m/{s}-1m-{y}-{m:02d}.zip"))
            m += 1
            if m == 13: y, m = y + 1, 1
        d = date(2026, 10, 1)
        while d <= LAST_DAY:
            jobs.append((s, f"{PFX}/daily/klines/{s}/1m/{s}-1m-{d.isoformat()}.zip"))
            d += timedelta(days=1)

    def one(j):
        s, k = j
        r, st = fetch_verified(k)
        return s, k, r, st
    data = {s: {} for s in syms}
    bad, nok = [], 0
    with ThreadPoolExecutor(24) as ex:
        for s, k, r, st in ex.map(one, jobs):
            if r is None:
                bad.append((k, st)); continue
            nok += 1
            for x in r:
                data[s][x[0]] = x
    man = {"files_ok": nok, "files_bad": bad, "per_symbol": {}}
    for s in syms:
        out = [data[s][t] for t in sorted(data[s])]
        write_csv(os.path.join(DATA, "1m", f"{s}.csv"), out)
        man["per_symbol"][s] = {"rows": len(out), "first": out[0][0], "last": out[-1][0]}
    json.dump(man, open(os.path.join(DATA, "MANIFEST_1m.json"), "w"), indent=1)
    print("DONE 1m files_ok", nok, "bad", len(bad), man["per_symbol"], flush=True)


def run_funding_rest_patch():
    """補跑: 只為既有 data/funding/*.csv 合併 2026-10 REST 資金費率 (首版 run_funding 的 REST 段因 datetime 未匯入而失敗，月檔部分正常)。"""
    syms = json.load(open(os.path.join(ROOT, "results", "a_universe.json")))["symbols_ever_in_top20"]

    def one(sym):
        path = os.path.join(DATA, "funding", f"{sym}.csv")
        rows = {}
        with open(path) as f:
            rd = csv.reader(f); next(rd)
            for p in rd:
                rows[int(p[0])] = float(p[1])
        n0 = len(rows); err = None
        try:
            start = 1790812800000
            while True:
                arr = json.loads(get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}&startTime={start}&limit=1000") or b"[]")
                if not arr:
                    break
                for r in arr:
                    rows[int(r["fundingTime"])] = float(r["fundingRate"])
                start = int(arr[-1]["fundingTime"]) + 1
                if len(arr) < 1000:
                    break
        except Exception as e:
            err = str(e)
        with open(path, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["funding_time_ms", "rate"]); w.writerows(sorted(rows.items()))
        return sym, {"added": len(rows) - n0, "err": err}
    man = {}
    with ThreadPoolExecutor(16) as ex:
        for sym, info in ex.map(one, syms):
            man[sym] = info
    json.dump(man, open(os.path.join(DATA, "MANIFEST_funding_rest_patch.json"), "w"), indent=1)
    print("DONE rest patch symbols", len(man), "added rows", sum(v["added"] for v in man.values()), "errors", sum(1 for v in man.values() if v["err"]), flush=True)


def run_funding():
    """只為『曾進入 PIT 前 20』的符號下載 fundingRate 月檔 (+ 2026-10 REST)。符號集合由 results/a_universe.json 機械決定。"""
    syms = json.load(open(os.path.join(ROOT, "results", "a_universe.json")))["symbols_ever_in_top20"]

    def one(sym):
        _, keys = s3_list(f"{PFX}/monthly/fundingRate/{sym}/")
        rows, bad = {}, []
        for k in sorted(x for x in keys if x.endswith(".zip")):
            ck = get(f"{CDN}/{k}.CHECKSUM"); z = get(f"{CDN}/{k}")
            if ck is None or z is None:
                bad.append((k, "missing")); continue
            if ck.decode().split()[0].lower() != hashlib.sha256(z).hexdigest():
                bad.append((k, "sha_mismatch")); continue
            with zipfile.ZipFile(io.BytesIO(z)) as zf:
                for ln in zf.read(zf.namelist()[0]).decode().splitlines():
                    p = ln.split(",")
                    if p and p[0].strip().isdigit():
                        rows[int(p[0])] = float(p[2])
        # 2026-10 以 REST 補 (月檔尚未發布)
        try:
            start = 1790812800000      # 2026-10-01 00:00 UTC
            while True:
                arr = json.loads(get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}&startTime={start}&limit=1000") or b"[]")
                if not arr:
                    break
                for r in arr:
                    rows[int(r["fundingTime"])] = float(r["fundingRate"])
                start = int(arr[-1]["fundingTime"]) + 1
                if len(arr) < 1000:
                    break
        except Exception as e:
            bad.append(("rest_2026-10", str(e)))
        os.makedirs(os.path.join(DATA, "funding"), exist_ok=True)
        if rows:
            with open(os.path.join(DATA, "funding", f"{sym}.csv"), "w", newline="") as f:
                w = csv.writer(f); w.writerow(["funding_time_ms", "rate"]); w.writerows(sorted(rows.items()))
        return sym, {"rows": len(rows), "bad": bad}
    man = {}
    with ThreadPoolExecutor(32) as ex:
        for sym, info in ex.map(one, syms):
            man[sym] = info
    json.dump(man, open(os.path.join(DATA, "MANIFEST_funding.json"), "w"), indent=1)
    print("DONE funding symbols", len(man), "with rows", sum(1 for v in man.values() if v["rows"]), "bad", sum(len(v["bad"]) for v in man.values()), flush=True)


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    {"1d": run_1d, "1m": run_1m, "funding": run_funding, "funding_rest": run_funding_rest_patch}[sys.argv[1]]()
