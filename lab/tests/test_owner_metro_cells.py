"""Native Maker PR75 bounded artifact tests (sanitized fixture-only)."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from owner_metro_results import read, IDS
from owner_metro_model import read_metro

FIX = Path(__file__).parent / "fixtures"
EXPECT = {"input_manifest_sha256":"a"*64, "config_sha256":"b"*64,
          "source_sha256":{"backtest_entry.py":"c"*64}}


class CellProof(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root/"output"
        (self.out/"cells").mkdir(parents=True)
        self.full = json.loads((FIX/"owner_metro_cell_full.json").read_text())
        self.part = json.loads((FIX/"owner_metro_cell_part.json").read_text())
        self.bad = json.loads((FIX/"owner_metro_cell_bad.json").read_text())
        self.inventory = {}

    def write(self, rel, value):
        path = self.out/rel
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(value,allow_nan=False).encode()
        path.write_bytes(raw)
        self.inventory[rel] = hashlib.sha256(raw).hexdigest()

    def cell(self, index, cid, template=None):
        sym, frame, k, ttl, exit = cid.split(":")
        item = dict(template or self.full)
        item.update(symbol=sym,framework=int(frame[:-1]),k=k,ttl=int(ttl),exit=exit)
        self.write(f"cells/{index:02d}_strategy.json",
                   {x:v for x,v in item.items() if x!="metrics"})
        self.write(f"cells/{index:02d}_matched.json",None)
        return {"cell_id":cid,"metrics":dict(item.get("metrics",{}))}

    def finalize(self, rows):
        self.write("results.json", {"cell_count":len(rows),"cells":rows})
        self.write("manifest.json", {**EXPECT,"artifacts":dict(self.inventory),
                                    "research_status":"EXPLORATORY_UNACCEPTED"})

    def result(self,expected=None):
        return read(str(self.root),("output",),"signed",expected or EXPECT)

    def test_missing_partial(self):
        self.assertEqual(self.result()["state"],"missing")
        self.assertIsNone(self.result()["count"])
        rows = list(sorted(IDS))
        self.cell(0,rows[0],self.part)
        self.cell(1,rows[1])
        data = self.result()
        self.assertEqual((data["state"],data["count"]),("preliminary",2))
        self.assertIsNone(data["metrics"][0]["actual_time_share"])

    def test_full_rank(self):
        rows=[]
        for index,cid in enumerate(sorted(IDS)):
            row=self.cell(index,cid)
            row["metrics"]["net_bp"]=str(index)
            rows.append(row)
        self.finalize(rows)
        data=self.result()
        self.assertEqual((data["state"],data["count"]),("complete",48))
        self.assertEqual(data["metrics"][0]["net_bp"],"47")
        self.assertEqual(data["by_exit"],{"X1":16,"X2":16,"X3":16})
        self.assertFalse(data["accepted"])
        self.assertIsNone(data["metrics"][0]["actual_time_share"])
        self.assertEqual(sum(data["by_exit"].values()),48)

    def test_duplicate(self):
        cid=sorted(IDS)[0]
        rows=[self.cell(0,cid),self.cell(1,cid)]
        self.finalize(rows)
        self.assertEqual(self.result()["state"],"duplicate")

    def test_bad_and_digest(self):
        cid=sorted(IDS)[0]
        self.cell(0,cid,self.bad)
        self.assertEqual(self.result()["state"],"invalid")
        self.cell(0,cid)
        self.finalize([{"cell_id":cid,"metrics":self.full["metrics"]}])
        self.assertEqual(self.result()["count"],1)
        (self.out/"cells/00_strategy.json").write_text("{}")
        self.assertEqual(self.result()["state"],"invalid")

    def test_old_and_nan(self):
        cid=sorted(IDS)[0]
        row=self.cell(0,cid)
        row["metrics"]["net_bp"]="NaN"
        self.finalize([row])
        self.assertIsNone(self.result()["metrics"][0]["net_bp"])
        for key in ("input_manifest_sha256","config_sha256","source_sha256"):
            old=dict(EXPECT)
            old[key]={"other.py":"d"*64} if key=="source_sha256" else "d"*64
            self.assertEqual(self.result(old)["state"],"old_or_invalid")

    def test_cache_restore(self):
        cid=sorted(IDS)[0]
        self.assertIsNone(self.result()["count"])
        self.cell(0,cid,self.part)
        self.assertEqual(self.result()["count"],1)
        self.cell(1,sorted(IDS)[1])
        self.assertEqual(self.result()["count"],2)

    def test_metro_transition(self):
        (self.root/"owner_metro").mkdir()
        (self.root/"owner_metro/bindings.json").write_text(json.dumps({
            "schema_version":1,"sources":{"results":"owner_metro/results.json"}}))
        (self.root/"owner_metro/results.json").write_text(json.dumps({
            "updated_at":1792065600,"root":"output","attempt":"signed",
            "expected":EXPECT}))
        initial=read_metro(str(self.root),now_ms=1792065600000)
        self.assertIsNone(initial["kpis"]["maker_cells"])
        cid=sorted(IDS)[0]
        self.cell(0,cid)
        current=read_metro(str(self.root),now_ms=1792065600000)
        self.assertEqual(current["kpis"]["maker_cells"],1)
        self.assertEqual(len(current["maker"]["results"]["metrics"]),1)
