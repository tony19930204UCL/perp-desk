"""Real offline account fractional X2 conservation and strict over-close guards."""
import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from decimal import Decimal as D, getcontext, localcontext
from unittest.mock import patch

# --base-dir switches ALL production imports to an independent immutable
# checkout, while this test/forensic harness remains in the PR75 branch.
_MODULE_ROOT = Path(__file__).resolve().parent.parent
if "--forensic" in sys.argv and "--base-dir" in sys.argv:
    _at = sys.argv.index("--base-dir")
    if _at + 1 >= len(sys.argv):
        raise SystemExit("--base-dir requires a directory")
    _MODULE_ROOT = Path(sys.argv[_at + 1]).resolve()
    if not (_MODULE_ROOT / "market_replay.py").is_file():
        raise SystemExit("--base-dir must be a maker_x2 source directory")
sys.path.insert(0, str(_MODULE_ROOT))
from account_lifecycle import OfflineAccount
from account_ledger import replay_account
from maker_exit_lab import X2Error

Q = "1.9999999999999999999999999999999999999999999999999"

def bar(ts, op, hi, lo, cl):
    return {"ts":ts, "open":op,"high":hi,"low":lo,
            "close":cl,"closed":True}

def account():
    return OfflineAccount("1000","50","5","5",2,"24",".0145",".0005")

class OvercloseControl(unittest.TestCase):
    def test_fractional_partial_tp_funding_exact_conservation(self):
        a=account()
        self.assertEqual(a.submit("FICTION","frac",0,"100","100","1",
                                  ".25",2,Q,"X2"),"PENDING")
        self.assertEqual(a.bar(bar(0,"100","100.27005","99.9","100"),
                               entry_qty=Q),"FILLED")
        initial=a.ledger()
        self.assertEqual(initial["qty"],D(Q))
        self.assertEqual(initial["reserved"],D("50"))
        self.assertEqual(a.bar(bar(60000,"100","100","99.5","99.8"),
                       maker_fill=True,settlements=[{
                           "id":"fund-1","settlement_id":"settlement-1",
                           "ts":120000,"mark":"100","rate":".001"}]),"OPEN")
        remaining=a.ledger()
        self.assertGreater(remaining["qty"],D("0"))
        self.assertLess(remaining["qty"],D(Q))
        with localcontext() as ctx:
            ctx.prec=50
            expected_funding = -D(Q)*D("100")*D(".001")
        self.assertEqual(remaining["funding_cost"], expected_funding)
        self.assertEqual(a.bar(bar(120000,"99.4","99.5","99","99.2"),
                               maker_fill=True),"CLOSED")
        finished=a.ledger()
        self.assertEqual(finished["qty"],D("0"))
        self.assertEqual(finished["reserved"],D("0"))
        sells=[e for e in a.events if e["type"]=="fill" and e["side"]=="sell"]
        buys=[e for e in a.events if e["type"]=="fill" and e["side"]=="buy"]
        self.assertEqual(len(sells),1)
        self.assertEqual(len(buys),2)
        with localcontext() as ctx:
            ctx.prec=50
            qty=D(Q)
            entry_price=D("100.25")
            half=qty/D(2)
            rest=qty-half
            tp1=entry_price*(1-D(".0045"))
            tp2=entry_price*(1-D(".0090"))
            self.assertEqual(D(sells[0]["qty"]),qty)
            self.assertEqual(D(sells[0]["price"]),entry_price)
            self.assertEqual([(D(e["qty"]),D(e["price"])) for e in buys],
                             [(half,tp1),(rest,tp2)])
            self.assertEqual(half+rest,qty)
            basis=qty*entry_price
            first=(basis/qty-tp1)*half
            basis-=basis/qty*half
            second=(basis/rest-tp2)*rest
            gross=first+second
            entry_fee=qty*entry_price*D(".0002")
            first_fee=half*tp1*D(".0002")
            second_fee=rest*tp2*D(".0002")
            fee=entry_fee+first_fee+second_fee
            funding_credit=qty*D("100")*D(".001")
            # Materialize the signed expected value before leaving Decimal50.
            expected_funding_cost=-funding_credit
            self.assertEqual(expected_funding_cost,expected_funding)
            cash=D("1000")-entry_fee
            cash+=funding_credit
            cash+=first-first_fee
            cash+=second-second_fee
            # All independent arithmetic (including unary negation,
            # quantity sums and sequential cash) stays inside Decimal50.
            self.assertEqual(finished["gross"],gross)
            self.assertEqual(finished["fees"],fee)
            self.assertEqual(finished["funding_cost"],expected_funding_cost)
            self.assertEqual(finished["cash"],cash)
        self.assertEqual(finished,replay_account("1000",a.events))

    def test_true_overclose_remains_rejected(self):
        a=account()
        a.submit("FICTION","negative",0,"100","100","1",".25",2,"2","X2")
        a.bar(bar(0,"100","100.27005","99.9","100"),entry_qty="2")
        original=list(a.events)
        forged=list(original)+[{"id":"bad-buy","seq":len(original),
            "ts":60000,"type":"fill","side":"buy",
            "qty":"2.000000000000000000000000000000000001",
            "price":"99","liquidity":"maker"}]
        with self.assertRaisesRegex(X2Error,"over-close"):
            replay_account("1000",forged)
        self.assertEqual(a.events,original)



class ForensicIncomplete(RuntimeError):
    """A finite diagnostic prefix ended without a confirmed over-close."""


def _json_safe(value):
    if isinstance(value, D):
        return str(value)
    if isinstance(value, dict):
        return {k:_json_safe(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, set):
        return sorted((_json_safe(v) for v in value),key=str)
    return value


def _sha256(path):
    h=hashlib.sha256()
    with open(path,"rb") as handle:
        for block in iter(lambda:handle.read(1048576),b""):
            h.update(block)
    return h.hexdigest()


def forensic(args):
    """Single selected original account cell and bounded chronological prefix.

    This intercepts only after the original replay_account raises; it
    never modifies, suppresses or mocks its over-close validation.
    """
    import resource
    from backtest_entry import load_config
    from bounded_history import prepare_bars, prepare_decisions
    from data_adapter import read_funding
    from matched_signals import select_matched
    from market_replay import replay_cell
    import market_replay
    from scenario_rates import SENSITIVITY

    if args.max_bars <= 0 or args.max_events <= 0:
        raise ValueError("positive --max-bars and --max-events required")
    cap=5*1024**3
    soft,hard=resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS,
                       (min(soft,cap) if soft!=-1 else cap,hard))
    config=load_config(args.config)
    with open(args.input,encoding="utf-8") as handle:
        sources=json.load(handle)
    if set(sources)!={"ETHUSDT","BTCUSDT"}:
        raise X2Error("original two-symbol input manifest required")
    if args.expected_config_sha256 and _sha256(args.config)!=args.expected_config_sha256:
        raise X2Error("config SHA256 identity mismatch")
    if args.expected_input_sha256 and _sha256(args.input)!=args.expected_input_sha256:
        raise X2Error("input manifest SHA256 identity mismatch")
    identity={"symbol":args.symbol,"framework_minutes":args.framework,
              "k":args.k,"ttl":args.ttl,"exit":args.exit,
              "scenario":args.scenario,"matched":args.matched,
              "input_manifest_sha256":_sha256(args.input),
              "config_sha256":_sha256(args.config),
              "account_source_sha256":_sha256(_MODULE_ROOT/"account_lifecycle.py"),
              "ledger_source_sha256":_sha256(_MODULE_ROOT/"account_ledger.py"),
              "market_source_sha256":_sha256(_MODULE_ROOT/"market_replay.py")}
    start=int(config["evaluation_start"])
    end=int(config["evaluation_end"])
    rates=SENSITIVITY if args.scenario=="cost1_5x" else None
    delay=2 if args.scenario=="delay2" else 0
    last={"index":None,"ts":None}
    observed={"account":None}
    required=("capital","per_trade_margin","leverage","daily_loss_limit",
              "loss_streak_limit","halt_hours","liq_pct","liq_fee_pct")

    class ForensicAccount(OfflineAccount):
        def _emit(self, kind, eid, ts, **fields):
            if len(self.events)>=args.max_events:
                raise ForensicIncomplete("MAX_EVENTS reached")
            return super()._emit(kind,eid,ts,**fields)

        def _sync(self, source):
            try:
                return super()._sync(source)
            except X2Error as exc:
                if str(exc)!="over-close":
                    raise
                bad=self.events[-1]
                # Original guard ran after the exact offending buy was
                # appended; the preceding chronological ledger is valid.
                before=replay_account(self.capital,self.events[:-1],
                                      rates=self.rates)
                with localcontext() as ctx:
                    ctx.prec=50
                    before_qty=before["qty"]
                    bad_qty=D(bad["qty"])
                    after_qty=before_qty-bad_qty
                    fees=[{"id":ev["id"],
                           "fee":str(D(ev["qty"])*D(ev["price"])*
                             (self.rates.maker_fee if ev["liquidity"]=="maker"
                              else self.rates.taker_fee))}
                          for ev in self.events if ev["type"]=="fill"]
                source_events=list(source)
                entry=self.entry
                active=self.exit
                payload={"status":"FIRST_REAL_OVERCLOSE",
                         "provenance":"OPERATOR_LOCAL_ACTUAL_INPUT_NOT_SYNTHETIC",
                         "cell":identity,"bar_open_index":last["index"],
                         "bar_open_ts":last["ts"],
                         "decimal_context":{"prec":getcontext().prec,
                             "rounding":str(getcontext().rounding)},
                         "before_qty":str(before_qty),
                         "offending_buy_qty":str(bad_qty),
                         "uncommitted_qty_after_buy":str(after_qty),
                         "offending_event":bad,
                         "account_events_including_bad":list(self.events),
                         "source_events":source_events,
                         "before_ledger":before,"fill_fees":fees,
                         "entry_state":None if entry is None else {
                             "signal_id":entry.signal_id,
                             "status":entry.status,"filled":entry.filled,
                             "remaining":entry.remaining,
                             "target_qty":entry.qty_target,"basis":entry.basis,
                             "limit":entry.limit,"events":list(entry.events)},
                         "exit_state":None if active is None else {
                             "style":active.style,"status":active.status,
                             "core_qty":active.core.qty,
                             "core_entry_qty":active.core.entry_qty,
                             "core_orders":dict(active.core.orders),
                             "core_stop":active.core.stop,
                             "core_events":list(active.core.events)}}
                print(json.dumps(_json_safe(payload),sort_keys=True,
                                 separators=(",",":")),flush=True)
                raise

    def create(config, rates=None):
        account=ForensicAccount(**{key:config[key] for key in required},
                                rates=rates)
        observed["account"]=account
        return account

    with tempfile.TemporaryDirectory(prefix="maker-forensic-") as tmp:
        path=sources[args.symbol]
        if set(path)!={"bars","funding"}:
            raise X2Error("bar/funding paths required")
        prepared=prepare_bars(path["bars"],Path(tmp)/"bars.sqlite3")
        features=prepare_decisions(prepared["bars"],args.framework,
                                   Path(tmp)/"features.sqlite3")
        funding=read_funding(path["funding"])["events"]
        selected=None
        if args.matched:
            chosen=select_matched(features,args.framework,start,end,count=6000)
            if chosen["status"]!="READY":
                raise ForensicIncomplete("INSUFFICIENT_UNIVERSE (6000)")
            selected=chosen["selected_timestamps"]
        def limited():
            for index,row in enumerate(
                    itertools.islice(prepared["bars"],args.max_bars)):
                last["index"],last["ts"]=index,row["ts"]
                yield row
        # Patch only the local account factory to inject observation;
        # every genuine production method/ledger guard still executes.
        with patch.object(market_replay,"_account",side_effect=create):
            replay_cell(args.symbol,limited(),funding,args.framework,
                        args.k,args.ttl,args.exit,config,start,end,
                        signal_times=selected,delay_minutes=delay,
                        eligible=features,rates=rates)
    raise ForensicIncomplete(
        "MAX_BARS/data prefix ended without actual over-close")


def forensic_main(argv):
    parser=argparse.ArgumentParser(description="Read-only bounded over-close forensic")
    parser.add_argument("--forensic",action="store_true",required=True)
    parser.add_argument("--config",required=True)
    parser.add_argument("--input",required=True)
    parser.add_argument("--base-dir")
    parser.add_argument("--symbol",choices=("ETHUSDT","BTCUSDT"),default="ETHUSDT")
    parser.add_argument("--framework",type=int,choices=(1,15),default=1)
    parser.add_argument("--k",choices=(".25",".50"),default=".25")
    parser.add_argument("--ttl",type=int,choices=(2,5),default=2)
    parser.add_argument("--exit",choices=("X1","X2","X3"),default="X1")
    parser.add_argument("--scenario",choices=("baseline","delay2","cost1_5x"),
                        default="baseline")
    parser.add_argument("--matched",action="store_true")
    parser.add_argument("--max-bars",type=int,required=True)
    parser.add_argument("--max-events",type=int,required=True)
    parser.add_argument("--expected-config-sha256")
    parser.add_argument("--expected-input-sha256")
    args=parser.parse_args(argv)
    try:
        forensic(args)
    except ForensicIncomplete as exc:
        print(json.dumps({"status":"INCOMPLETE","reason":str(exc),
                          "limit_bars":args.max_bars,
                          "limit_events":args.max_events}),flush=True)
        return 3
    return 3

if __name__=="__main__":
    if "--forensic" in sys.argv:
        raise SystemExit(forensic_main(sys.argv[1:]))
    unittest.main(verbosity=2)
