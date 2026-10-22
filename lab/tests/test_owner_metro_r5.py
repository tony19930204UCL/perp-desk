"""Synthetic read-only owner metro decision, freshness and timeline contracts."""
import json
import tempfile
import unittest
from pathlib import Path
from owner_metro_model import read_metro
from owner_metro_sources import load


class MetroR5(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for name in ("commitments","owner_metro","cron","lab/journal","lab/shared"):
            (self.root/name).mkdir(parents=True)
        self.now=1792065600000
        self.ledger=self.root/"commitments/ledger.json"
        self.directive=self.root/"owner_metro/directive.json"
        (self.root/"owner_metro/bindings.json").write_text(json.dumps({
            "schema_version":1,"sources":{"directive":"owner_metro/directive.json"}}))
        self.directive.write_text(json.dumps({"updated_at":1792065600,
                                               "pending_decisions":9}))
        self.write([])

    def write(self,rows):
        self.ledger.write_text(json.dumps({"next":200,"items":rows}))

    def model(self):
        return read_metro(str(self.root),now_ms=self.now)

    def item(self,i,state="OPEN",blocked="user:Question",created=1792065500):
        return {"id":"C-"+str(i),"state":state,"text":"Needs a human",
                "blocked_by":blocked,"created":created,"updated":created}

    def test_decision_list(self):
        rows=[self.item(1),self.item(2,"DISPATCHED","user:Another question"),
              self.item(3,"DROPPED"),self.item(4,"OPEN","item:C-5")]
        self.write(rows)
        k=self.model()["kpis"]
        self.assertEqual((k["pending_decisions"],k["pending_source"]),(9,"directive"))
        self.assertEqual(len(k["pending_list"]),2)
        self.assertEqual(k["pending_list"][0]["question"],"Question")
        self.assertEqual(k["pending_list"][0]["reason"],"Needs a human")
        self.assertEqual(k["pending_list"][0]["created"],1792065500)
        self.write([])
        self.assertEqual(self.model()["kpis"]["pending_list"],[])
        self.ledger.write_text("{")
        self.assertIsNone(self.model()["kpis"]["pending_list"])

    def test_bounds(self):
        self.write([self.item(i,blocked="user:"+("Q"*2000))
                    for i in range(1,70)])
        k=self.model()["kpis"]
        self.assertEqual((k["pending_decisions"],k["pending_source"]),(9,"directive"))
        self.assertEqual(len(k["pending_list"]),64)
        self.assertEqual(len(k["pending_list"][0]["question"]),2000)

    def test_freshness(self):
        self.directive.write_text(json.dumps({"updated_at":1792065000,
                                               "pending_decisions":2}))
        self.assertEqual(load(str(self.root),self.now)[1]["directive"]["state"],
                         "available")
        self.directive.write_text(json.dumps({"updated_at":1792064600,
                                               "pending_decisions":2}))
        self.assertEqual(load(str(self.root),self.now)[1]["directive"]["state"],
                         "stale")
        roadmap=self.root/"lab/journal/roadmap.json"
        roadmap.write_text(json.dumps({"updated":"2026-10-15T10:00:00Z",
                                        "items":[]}))
        self.write([self.item(1,created=1792065000)])
        self.assertEqual(self.model()["sources"]["ledger"]["state"],"available")
        # Roadmap's age is checked against its own 24-hour budget.
        self.assertEqual(self.model()["sources"]["roadmap"]["state"],"available")
        roadmap.write_text(json.dumps({"updated":"2026-10-13T12:00:00Z",
                                        "items":[]}))
        self.assertEqual(self.model()["sources"]["roadmap"]["state"],"stale")

    def test_recent(self):
        rows=[self.item(i,created=1792065000+i) for i in range(1,18)]
        self.write(rows)
        data=self.model()["recent"]
        self.assertEqual(len(data),12)
        self.assertEqual(data[0]["id"],"C-17")
        self.assertEqual(data[-1]["id"],"C-6")
        self.write([])
        self.assertEqual(self.model()["recent"],[])

    def test_dead_holder(self):
        row=self.item(1,"DISPATCHED")
        row["handle"]={"kind":"pid","pid":999999,"start":5}
        self.write([row])
        recent=self.model()["recent"]
        self.assertEqual(recent[0]["alive"],"已死")
