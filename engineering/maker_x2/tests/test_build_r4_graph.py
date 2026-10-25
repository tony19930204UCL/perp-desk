"""Pure stdlib controls for the exact installed R4 stage graph."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_r4_graph import build, validate, GraphError, main


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ns = self.root / "stage-relay" / "C0041"
        self.ev = self.ns / "evidence"
        (self.ev / "upstream").mkdir(parents=True)
        self.output = self.ev / "results" / "new-r4"
        self.config = self.root / "account.json"
        self.config.write_text('{"fictional":true}')
        self.inp = self.root / "input.json"
        self.inp.write_text('{"fictional":"offline"}')
        self.witness = self.ev / "upstream" / "evidence.json"
        self.witness.write_text('{"synthetic":true}')
        self.proof = self.ev / "upstream" / "accepted.json"
        self.proof.write_text(json.dumps({
            "stage_id": "offline-source", "source_head": "a"*40,
            "input_sha256": "b"*64, "decision": "ACCEPTED",
            "reviewer": "fictional-test",
            "evidence_path": "upstream/evidence.json",
            "evidence_sha256": hashlib.sha256(
                self.witness.read_bytes()).hexdigest()}))
        self.out = self.ns / "graph.json"

    def build_graph(self):
        return build(self.ns, "c"*40, self.inp, self.config, self.output,
                     self.proof, sys.executable, self.out)

    def test_valid_graph(self):
        # Fails on missing controller-required keys or wrong stage count.
        graph = self.build_graph()
        self.assertIs(validate(graph), graph)
        self.assertEqual(graph["schema_version"], 1)
        self.assertEqual(graph["kind"], "stage_relay")
        self.assertEqual(graph["evidence_root"], str(self.ev))
        self.assertEqual([x["id"] for x in graph["stages"]],
                         ["r4-run","r4-verify","r4-reflection"])
        self.assertEqual([x["reports"][0]["rows"]
                          for x in graph["stages"]], [144,1,1])
        self.assertEqual(graph["stages"][0]["reports"][0]["symbol_cells"],
                         {"ETHUSDT":72,"BTCUSDT":72})
        self.assertEqual(graph["stages"][0]["command"][:5],
                         ["prlimit","--as=5368709120","--",
                          sys.executable,"-u"])
        self.assertIn("--run",graph["stages"][0]["command"])
        self.assertIn("--verify",graph["stages"][1]["command"])
        self.assertIn("r4_reflection.py",graph["stages"][2]["command"])
        self.assertNotIn("r4_entry.py",graph["stages"][2]["command"])
        self.assertEqual(graph["bootstrap"]["acceptance_path"],
                         "upstream/accepted.json")

    def test_deterministic(self):
        # Fails if timestamps, random IDs or object iteration leak to bytes.
        a=self.build_graph()
        b=self.build_graph()
        encoded=lambda x:(json.dumps(x,sort_keys=True,separators=(",",":"),
                            ensure_ascii=False)+"\n").encode()
        self.assertEqual(encoded(a), encoded(b))
        self.assertEqual(hashlib.sha256(encoded(a)).hexdigest(),
                         hashlib.sha256(encoded(b)).hexdigest())

    def test_cli_output(self):
        # Fails if graph cannot be written once or silently overwritten.
        args=["--evidence-root",str(self.ns),"--source-head","c"*40,
              "--input-json",str(self.inp),"--config",str(self.config),
              "--output-root",str(self.output),
              "--bootstrap-acceptance-json",str(self.proof),
              "--python",sys.executable,"--out",str(self.out)]
        expected=self.build_graph()
        self.assertEqual(main(args),0)
        self.assertEqual(json.loads(self.out.read_text()),expected)
        self.assertEqual(main(args),1)

    def test_no_runtime_side_effect(self):
        # Graph creation must not launch stages or create research output.
        self.build_graph()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
