"""Routed Binance public streams: independently checked mark/book/depth health."""
import time
from decimal import Decimal

class Depth:
    def __init__(self):
        self.last = None
        self.ready = False
        self.pending = []
        self.gaps = 0
        self.bids, self.asks = {}, {}

    def delta(self, d):
        if not self.ready:
            self.pending.append(d)
            if len(self.pending) > 256:
                self.pending = self.pending[-256:]
            return False
        if int(d["u"]) <= self.last:
            return True
        if int(d["pu"]) != self.last:
            self.gaps += 1
            self.last = None
            self.ready = False
            self.pending = [d]
            self.bids, self.asks = {}, {}
            return False
        self._apply(d)
        return True

    def _apply(self, d):
        for key, target in (("b", self.bids), ("a", self.asks)):
            for p, qty in d.get(key, []):
                amount = Decimal(qty)
                if amount < 0:
                    raise ValueError("invalid depth")
                if amount:
                    target[p] = amount
                else:
                    target.pop(p, None)
        self.last = int(d["u"])

    def snapshot(self, snap):
        self.last = int(snap["lastUpdateId"])
        self.bids = {p: Decimal(q) for p, q in snap["bids"]}
        self.asks = {p: Decimal(q) for p, q in snap["asks"]}
        self.ready = True
        pending, self.pending = self.pending, []
        first = True
        for d in pending:
            if int(d["u"]) < self.last:
                continue
            if first:
                if not int(d["U"]) <= self.last + 1 <= int(d["u"]):
                    self.ready = False
                    break
                self._apply(d)
                first = False
            elif not self.delta(d):
                break
        self.ready = self.ready and not first
        return self.ready

class Feeds:
    ROUTE = {"bookTicker": "public", "depthUpdate": "public",
             "markPriceUpdate": "market", "aggTrade": "market"}
    def __init__(self):
        self.seen = {}
        self.book = Depth()
        self.errors = []
        self.prices = {}

    def observe(self, route, obj, wall=None, mono=None):
        wall = int(time.time() * 1000) if wall is None else wall
        mono = time.monotonic() if mono is None else mono
        d = obj.get("data", obj)
        kind = d.get("e")
        if self.ROUTE.get(kind) != route:
            self.errors.append("BAD_ROUTE:" + str(kind))
            return False
        ts = d.get("E")
        if type(ts) is not int or not 0 <= wall - ts <= 3000:
            self.errors.append("CLOCK_OR_STALE:" + str(kind))
            return False
        self.seen[kind] = {"event_ms": ts, "receive_ms": wall, "mono": mono,
                           "age_ms": wall - ts}
        if kind == "depthUpdate":
            self.book.delta(d)
        elif kind == "markPriceUpdate":
            self.prices["mark"] = d.get("p")
        elif kind == "bookTicker":
            self.prices["bid"], self.prices["ask"] = d.get("b"), d.get("a")
        return True

    def health(self, wall=None, mono=None):
        wall = int(time.time() * 1000) if wall is None else wall
        mono = time.monotonic() if mono is None else mono
        required = ("markPriceUpdate", "bookTicker", "depthUpdate")
        fresh = all(k in self.seen and 0 <= wall - self.seen[k]["receive_ms"] <= 2500
                    and 0 <= mono - self.seen[k]["mono"] <= 3 for k in required)
        return {"mark_ms": self.seen.get("markPriceUpdate", {}).get("event_ms", 0),
                "book_ready": fresh and "bid" in self.prices,
                "depth_ready": fresh and self.book.ready,
                "status": "HEALTHY" if fresh and self.book.ready else "UNKNOWN",
                "gaps": self.book.gaps}
