"""Negative tests for R4 graph fail-closed source and controller contract."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_build_r4_graph import GraphTests
from build_r4_graph import build, validate, GraphError


class Negative(unittest.TestCase):
    def setUp(self):
        self.fixture=GraphTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown if hasattr(
            self.fixture,"tearDown") else lambda: None)
        self.addCleanup(self.fixture.doCleanups)

    def base(self):
        return self.fixture.build_graph()

    def test_bad_head(self):
        # Kills acceptance of truncated/uppercase/unverified commit IDs.
        f=self.fixture
        for h in ("a"*39,"A"*40,"g"*40,"a"*41):
            with self.assertRaisesRegex(GraphError,"40"):
                build(f.ns,h,f.inp,f.config,f.output,
                      f.proof,sys.executable,f.out)

    def test_wrong_namespace(self):
        # Kills fallback to home, arbitrary directory or profile escape.
        f=self.fixture
        with self.assertRaisesRegex(GraphError,"stage-relay/C0041"):
            build(f.root,"c"*40,f.inp,f.config,f.output,
                  f.proof,sys.executable,f.out)

    def test_bad_cwd(self):
        # Kills accepting a relative stage working directory.
        g=self.base()
        g["stages"][0]["cwd"]="relative"
        with self.assertRaisesRegex(GraphError,"cwd"):
            validate(g)

    def test_missing_command(self):
        # Kills implicit command fallback and denied executors.
        g=self.base()
        del g["stages"][0]["command"]
        with self.assertRaisesRegex(GraphError,"command"):
            validate(g)
        g=self.base()
        g["stages"][0]["command"]=["python3","read_file"]
        with self.assertRaisesRegex(GraphError,"denied"):
            validate(g)

    def test_wrong_rows(self):
        # Kills reduced research matrix and forged stage row counts.
        g=self.base()
        for i,rows in ((0,143),(1,144),(2,0)):
            changed=copy.deepcopy(g)
            changed["stages"][i]["reports"][0]["rows"]=rows
            with self.assertRaisesRegex(GraphError,"rows"):
                validate(changed)

    def test_wrong_reflection(self):
        # Kills accidentally routing reflection through r4_entry.py.
        g=self.base()
        g["stages"][2]["command"]=[
            v.replace("r4_reflection.py","r4_entry.py")
            for v in g["stages"][2]["command"]]
        with self.assertRaisesRegex(GraphError,"reflection"):
            validate(g)

    def test_missing_proof(self):
        # Kills bootstrap substitution, nonexistent upstream evidence.
        f=self.fixture
        f.witness.unlink()
        with self.assertRaisesRegex(GraphError,"evidence"):
            f.build_graph()

    def test_outside_output(self):
        # Kills output artifact path escaping fixed supervisor evidence.
        f=self.fixture
        with self.assertRaisesRegex(GraphError,"outside"):
            build(f.ns,"c"*40,f.inp,f.config,f.root/"foreign",
                  f.proof,sys.executable,f.out)


if __name__=="__main__":
    unittest.main()
