"""U4 (operator-local): live auto_allow behaviours merged into the source. Stubs subprocess/time only; clicks nothing."""
import importlib.util, io, json, os, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "engineering" / "relay" / "auto_allow.py"


class AutoAllowLive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.home = Path(self.tmp.name)
        (self.home / "cache").mkdir()
        (self.home / "lab" / "journal").mkdir(parents=True)
        self._old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)
        spec = importlib.util.spec_from_file_location("auto_allow_u4", SRC)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.m.ONCE = True
        self.m.active_job = lambda: Path("/job")
        self.calls = []

    def tearDown(self):
        if self._old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old
        self.tmp.cleanup()

    def feed(self, *results):
        queue = list(results)

        def run(script, *args):
            self.calls.append((script, args))
            return queue.pop(0)
        self.m.run = run

    def once(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.m.main()
        return out.getvalue()

    def state(self):
        return json.loads(self.m.STATE.read_text())

    def test_transient_quiet(self):
        self.feed(*[(1, "", "BlockingIOError: busy")] * 8)
        outs = [self.once() for _ in range(7)]
        self.assertEqual([""] * 7, outs)
        self.assertEqual(7, self.state()["transient"])
        self.assertIn("核准器連續 8 次", self.once())

    def test_transient_reset(self):
        self.feed((1, "", "TimeoutExpired"), (1, "NO_CARD", ""), (0, "NO_CARD", ""))
        self.once(); self.once()
        self.assertEqual(1, self.state()["transient"])
        self.once()
        self.assertEqual(0, self.state()["transient"])

    def test_click_message_zh(self):
        self.feed((0, "ALLOWED_ONCE path", ""))
        self.assertIn("已核准一次寫入卡", self.once())
        self.assertEqual(1, self.state()["clicked"])

    def test_diff_limit_600(self):
        self.feed((1, "", "token-like string present"), (0, "ALLOWED_ONCE x", ""))
        self.assertIn("差異檢查", self.once())
        self.assertEqual("600", self.calls[1][1][1])
        self.assertEqual("relay-allow-if-diff-clean.py", self.calls[1][0])

    def test_secret_refused(self):
        self.feed((1, "", "token-like string present"), (1, "", "secret-like content refused"))
        out = self.once()
        self.assertIn("兩個核准腳本都拒絕", out)
        self.assertNotIn("已核准", out)
        self.assertNotIn("clicked", self.state())


if __name__ == "__main__":
    unittest.main()
