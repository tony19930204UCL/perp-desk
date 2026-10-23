try:
    import fcntl
except ImportError:
    fcntl = None  # Windows CI runs only test43; POSIX tests still require fcntl.
import glob
import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta

WORK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVE_PY = os.environ.get("TEST_ARCHIVE_PY", os.path.join(WORK_DIR, "heatmap_archive.py"))
CAPTURE_PS1 = os.environ.get("TEST_CAPTURE_PS1", os.path.join(WORK_DIR, "heatmap_capture.ps1"))
FIXTURES_DIR = os.path.join(WORK_DIR, "fixtures")
SAMPLE_PNG = os.path.join(FIXTURES_DIR, "sample_btc_24h_m1.png")
SAMPLE_CHART = os.path.join(FIXTURES_DIR, "sample_btc_24h_m1.chart.json.gz")
TMP_BASE = os.path.join(WORK_DIR, ".tmp")


def get_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class TestHeatmapArchive(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TMP_BASE):
            try:
                if not os.listdir(TMP_BASE):
                    os.rmdir(TMP_BASE)
            except Exception:
                pass

    def setUp(self):
        os.makedirs(TMP_BASE, exist_ok=True)
        self.test_dir = tempfile.mkdtemp(prefix="test_hmap_", dir=TMP_BASE)
        self.data_root = os.path.join(self.test_dir, "data")
        os.makedirs(self.data_root, mode=0o700, exist_ok=True)
        self.png_sha = get_sha256(SAMPLE_PNG)
        self.chart_sha = get_sha256(SAMPLE_CHART)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            try:
                shutil.rmtree(self.test_dir)
            except Exception:
                pass
        if os.path.exists(TMP_BASE):
            try:
                if not os.listdir(TMP_BASE):
                    os.rmdir(TMP_BASE)
            except Exception:
                pass

    def make_fake_powershell(self, behavior="ok", run_id="20261019T173000Z"):
        script_path = os.path.join(self.test_dir, f"fake_ps_{behavior}.py")
        curr_xl = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M")
        code = f"""#!/usr/bin/env python3
import sys, json, os, shutil, time

behavior = "{behavior}"
run_id = "{run_id}"
out_dir = None
for i, arg in enumerate(sys.argv):
    if arg == "-OutDir" and i + 1 < len(sys.argv):
        out_dir = sys.argv[i + 1]

if behavior == "timeout":
    time.sleep(300)
    sys.exit(0)

if behavior == "bad_json":
    print("FATAL: INVALID JSON OUTPUT")
    sys.exit(0)

if behavior == "chrome_unreachable":
    out = {{
        "schema": 1,
        "status": "chrome_unreachable",
        "chrome": {{"version": "", "port": 19301, "owner_ok": False}},
        "page": {{"title": "", "url": "", "tabs": []}},
        "logged_in": "unknown",
        "visibility": "unknown",
        "warnings": [],
        "viewport": [1400, 1200],
        "results": []
    }}
    print(json.dumps(out))
    sys.exit(0)

if behavior == "not_logged_in":
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        shutil.copy2("{SAMPLE_PNG}", os.path.join(out_dir, f"{{run_id}}_BTC_pair_24h_m1.png"))
        shutil.copy2("{SAMPLE_CHART}", os.path.join(out_dir, f"{{run_id}}_BTC_pair_24h_m1.chart.json.gz"))
    out = {{
        "schema": 1,
        "status": "partial",
        "chrome": {{"version": "155", "port": 19301, "owner_ok": True}},
        "page": {{"title": "Coinglass", "url": "https://www.coinglass.com/pro/futures/LiquidationHeatMap", "tabs": []}},
        "logged_in": False,
        "login_evidence": "login_leaf",
        "login_wait_ms": 100,
        "visibility": "visible",
        "warnings": [],
        "viewport": [1400, 1200],
        "browser_tz_offset_min": 0,
        "results": [
            {{
                "coin": "BTC",
                "mode": "pair",
                "timeframe": "24 hour",
                "model": 1,
                "ok": True,
                "error": None,
                "login_required": False,
                "header": "Binance BTC/USDT",
                "price": 81903.0,
                "y_min": 77616.52,
                "y_max": 86359.12,
                "n_x": 288,
                "n_y": 136,
                "x_first": "19 Oct 2026, 17:00",
                "x_last": "{curr_xl}",
                "canvas": [1055, 730],
                "png_file": f"{{run_id}}_BTC_pair_24h_m1.png",
                "png_sha256": "{self.png_sha}",
                "chart_file": f"{{run_id}}_BTC_pair_24h_m1.chart.json.gz",
                "chart_sha256": "{self.chart_sha}",
                "chart_data_ok": True,
                "elapsed_ms": 1000
            }},
            {{
                "coin": "BTC",
                "mode": "pair",
                "timeframe": "1 week",
                "model": 1,
                "ok": False,
                "error": "login_required",
                "login_required": True,
                "header": "",
                "price": None,
                "y_min": None,
                "y_max": None,
                "n_x": 0,
                "n_y": 0,
                "x_first": "",
                "x_last": "",
                "canvas": [0, 0],
                "png_file": None,
                "png_sha256": None,
                "chart_file": None,
                "chart_sha256": None,
                "chart_data_ok": False,
                "elapsed_ms": 500
            }}
        ]
    }}
    print(json.dumps(out))
    sys.exit(0)

# Default ok / sha_mismatch / price_out_of_range / page_hidden
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    shutil.copy2("{SAMPLE_PNG}", os.path.join(out_dir, f"{{run_id}}_BTC_pair_24h_m1.png"))
    shutil.copy2("{SAMPLE_CHART}", os.path.join(out_dir, f"{{run_id}}_BTC_pair_24h_m1.chart.json.gz"))

png_hash = "{self.png_sha}"
if behavior == "sha_mismatch":
    png_hash = "deadbeef" * 8

price_val = 81903.0
if behavior == "price_out_of_range":
    price_val = 999999.0

vis_val = "visible"
warns_val = []
if behavior == "page_hidden":
    vis_val = "hidden"
    warns_val = ["page_hidden"]

out = {{
    "schema": 1,
    "status": "ok",
    "chrome": {{"version": "155", "port": 19301, "owner_ok": True}},
    "page": {{"title": "Coinglass", "url": "https://www.coinglass.com/pro/futures/LiquidationHeatMap", "tabs": []}},
    "logged_in": True,
    "login_evidence": "user_menu_button",
    "login_wait_ms": 506,
    "visibility": vis_val,
    "warnings": warns_val,
    "viewport": [1400, 1200],
    "browser_tz_offset_min": 0,
    "results": [
        {{
            "coin": "BTC",
            "mode": "pair",
            "timeframe": "24 hour",
            "model": 1,
            "ok": True,
            "error": None,
            "login_required": False,
            "header": "Binance BTC/USDT Liquidation Heatmap",
            "price": price_val,
            "y_min": 77616.52,
            "y_max": 86359.12,
            "n_x": 288,
            "n_y": 136,
            "x_first": "19 Oct 2026, 17:00",
            "x_last": "{curr_xl}",
            "canvas": [1055, 730],
            "png_file": f"{{run_id}}_BTC_pair_24h_m1.png",
            "png_sha256": png_hash,
            "chart_file": f"{{run_id}}_BTC_pair_24h_m1.chart.json.gz",
            "chart_sha256": "{self.chart_sha}",
            "chart_data_ok": True,
            "elapsed_ms": 1200
        }}
    ]
}}
print(json.dumps(out))
"""
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code)
        os.chmod(script_path, 0o755)
        return script_path

    def make_config(self, combos=None, retention_days=60, max_total_bytes=5368709120, png_every_n_runs=4):
        if combos is None:
            combos = [{"coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True}]
        cfg_path = os.path.join(self.test_dir, "config.json")
        cfg_data = {
            "port": 19301,
            "data_root": self.data_root,
            "retention_days": retention_days,
            "max_total_bytes": max_total_bytes,
            "viewport": [1400, 1200],
            "settle_seconds": 4,
            "png_every_n_runs": png_every_n_runs,
            "combos": combos,
        }
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg_data, f)
        return cfg_path

    def run_wrapper(self, config_path, fake_ps, extra_args=None):
        cmd = [
            sys.executable, ARCHIVE_PY,
            "--config", config_path,
            "--data-root", self.data_root,
            "--powershell", f"python3 {fake_ps}"
        ]
        if extra_args:
            cmd.extend(extra_args)
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_01_permissions_0700_0600(self):
        """Test directory permission is 0700, and files are 0600."""
        cfg = self.make_config()
        fake_ps = self.make_fake_powershell("ok")
        res = self.run_wrapper(cfg, fake_ps)
        self.assertEqual(res.returncode, 0)
        self.assertIn("OK heatmap-archive", res.stdout)

        # Check date directory permission
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        self.assertTrue(os.path.exists(today_dir))
        self.assertEqual(os.stat(today_dir).st_mode & 0o777, 0o700)

        # Check files inside date directory
        for f in os.listdir(today_dir):
            fp = os.path.join(today_dir, f)
            self.assertEqual(os.stat(fp).st_mode & 0o777, 0o600, f"File {f} is not 0600")

        # Check runs.jsonl permission
        runs_path = os.path.join(self.data_root, "runs.jsonl")
        self.assertTrue(os.path.exists(runs_path))
        self.assertEqual(os.stat(runs_path).st_mode & 0o777, 0o600)

        # Check .lock permission
        lock_path = os.path.join(self.data_root, ".lock")
        if os.path.exists(lock_path):
            self.assertEqual(os.stat(lock_path).st_mode & 0o777, 0o600)

        # Check atomic_write_bytes default mode
        sys_path_clean = list(sys.path)
        try:
            if WORK_DIR not in sys.path:
                sys.path.insert(0, WORK_DIR)
            import importlib
            import heatmap_archive
            importlib.reload(heatmap_archive)
            test_perm_file = os.path.join(self.test_dir, "test_perm.bin")
            heatmap_archive.atomic_write_bytes(test_perm_file, b"test")
            self.assertEqual(os.stat(test_perm_file).st_mode & 0o777, 0o600, "atomic_write_bytes default mode must be 0o600")
        finally:
            sys.path = sys_path_clean

    def test_02_filename_format(self):
        """Test archived filename format <run_id>_<COIN>_<mode>_<tf>_m<model>."""
        cfg = self.make_config()
        fake_ps = self.make_fake_powershell("ok")
        res = self.run_wrapper(cfg, fake_ps)
        self.assertEqual(res.returncode, 0)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        files = os.listdir(today_dir)
        pattern = r"^\d{8}T\d{6}Z_BTC_pair_24h_m1\.(png|json|chart\.json\.gz)$"
        self.assertGreaterEqual(len(files), 2)
        for f in files:
            self.assertTrue(re.match(pattern, f), f"Filename {f} does not match format")

    def test_03_metadata_fields_and_sha256(self):
        """Test metadata fields match SPEC §57 and sha256 matches actual files."""
        cfg = self.make_config()
        fake_ps = self.make_fake_powershell("ok")
        res = self.run_wrapper(cfg, fake_ps)
        self.assertEqual(res.returncode, 0)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        json_files = [f for f in os.listdir(today_dir) if f.endswith(".json")]
        self.assertEqual(len(json_files), 1)

        meta_path = os.path.join(today_dir, json_files[0])
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        required_fields = [
            "schema", "captured_utc", "captured_local", "run_id", "coin", "mode", "timeframe",
            "model", "header", "logged_in", "login_evidence", "login_wait_ms", "visibility", "focus_mode", "price", "y_min", "y_max", "n_x", "n_y", "x_first",
            "x_last", "browser_tz_offset_min", "canvas_w", "canvas_h", "png_sha256",
            "chart_sha256", "chart_data_ok", "login_required", "page_url", "chrome_version",
            "source", "warnings"
        ]
        for field in required_fields:
            self.assertIn(field, meta, f"Missing field in metadata: {field}")
        self.assertEqual(meta["visibility"], "visible")

        self.assertEqual(meta["source"], "coinglass-dedicated-chrome-19301")
        self.assertEqual(meta["schema"], 1)

        # Verify actual file sha256 against metadata
        base_name = json_files[0][:-5]
        png_path = os.path.join(today_dir, f"{base_name}.png")
        self.assertTrue(os.path.exists(png_path))
        self.assertEqual(get_sha256(png_path), meta["png_sha256"])

        chart_path = os.path.join(today_dir, f"{base_name}.chart.json.gz")
        self.assertTrue(os.path.exists(chart_path))
        self.assertEqual(get_sha256(chart_path), meta["chart_sha256"])

    def test_04_runs_jsonl_written_on_failure(self):
        """Test runs.jsonl is written even on failure and exit code is 0."""
        cfg = self.make_config()
        fake_ps = self.make_fake_powershell("bad_json")
        res = self.run_wrapper(cfg, fake_ps)
        self.assertEqual(res.returncode, 0)
        self.assertIn("ERROR heatmap-archive", res.stdout)

        runs_path = os.path.join(self.data_root, "runs.jsonl")
        self.assertTrue(os.path.exists(runs_path))
        with open(runs_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertGreaterEqual(len(lines), 1)
        self.assertEqual(lines[-1]["status"], "powershell_error")

    def test_05_chrome_unreachable_outputs_error_and_exit_0(self):
        """Test chrome_unreachable outputs ERROR, exit 0, runs.jsonl recorded."""
        cfg = self.make_config()
        fake_ps = self.make_fake_powershell("chrome_unreachable")
        res = self.run_wrapper(cfg, fake_ps)
        self.assertEqual(res.returncode, 0)
        self.assertIn("ERROR heatmap-archive status=chrome_unreachable", res.stdout)

        runs_path = os.path.join(self.data_root, "runs.jsonl")
        self.assertTrue(os.path.exists(runs_path))
        with open(runs_path, "r", encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(records[-1]["status"], "chrome_unreachable")

    def test_06_bad_json(self):
        """Test bad JSON from powershell produces powershell_error and exit 0."""
        cfg = self.make_config()
        fake_ps = self.make_fake_powershell("bad_json")
        res = self.run_wrapper(cfg, fake_ps)
        self.assertEqual(res.returncode, 0)
        self.assertIn("ERROR heatmap-archive status=powershell_error", res.stdout)

    def test_07_sha_mismatch(self):
        """Test sha mismatch prevents file archiving."""
        cfg = self.make_config()
        fake_ps = self.make_fake_powershell("sha_mismatch")
        res = self.run_wrapper(cfg, fake_ps)
        self.assertEqual(res.returncode, 0)
        self.assertIn("combos=0/1", res.stdout)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        if os.path.exists(today_dir):
            files = [f for f in os.listdir(today_dir) if f.endswith(".png")]
            self.assertEqual(len(files), 0, "Corrupted file must not be archived")

    def test_08_timeout(self):
        """Test powershell timeout kills child and outputs status=powershell_timeout."""
        cfg = self.make_config()
        # Test timeout handling by injecting short timeout runner or monkeypatched timeout
        # Create a mock powershell that sleeps
        fake_ps = self.make_fake_powershell("timeout")
        # Run with short timeout via python script invocation of run_archive
        test_script = os.path.join(self.test_dir, "test_timeout_runner.py")
        with open(test_script, "w") as f:
            f.write(f"""import subprocess, sys, re
# Replace timeout=280 with timeout=0.5 dynamically
with open("{ARCHIVE_PY}", "r") as f:
    code = f.read()
code = code.replace("timeout=280", "timeout=0.5")
with open("{os.path.join(self.test_dir, 'archive_short_to.py')}", "w") as f:
    f.write(code)

cmd = [sys.executable, "{os.path.join(self.test_dir, 'archive_short_to.py')}",
       "--config", "{cfg}", "--data-root", "{self.data_root}", "--ps-script", "{CAPTURE_PS1}", "--powershell", "python3 {fake_ps}"]
res = subprocess.run(cmd, capture_output=True, text=True)
print("RET:", res.returncode)
print(res.stdout)
""")
        res = subprocess.run([sys.executable, test_script], capture_output=True, text=True)
        self.assertIn("RET: 0", res.stdout)
        self.assertIn("status=powershell_timeout", res.stdout)

    def test_09_login_required_not_archived(self):
        """Test login_required combo is not archived while available combo is."""
        cfg = self.make_config(combos=[
            {"coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True},
            {"coin": "BTC", "mode": "pair", "timeframe": "1 week", "model": 1, "enabled": True}
        ])
        fake_ps = self.make_fake_powershell("not_logged_in")
        res = self.run_wrapper(cfg, fake_ps)
        self.assertEqual(res.returncode, 0)
        self.assertIn("combos=1/2", res.stdout)
        self.assertIn("logged_in=false", res.stdout)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        pngs = [f for f in os.listdir(today_dir) if f.endswith(".png")]
        self.assertEqual(len(pngs), 1)
        self.assertIn("24h", pngs[0])

    def test_10_price_out_of_range_refused(self):
        """Test price out of range is refused and not archived."""
        cfg = self.make_config()
        fake_ps = self.make_fake_powershell("price_out_of_range")
        res = self.run_wrapper(cfg, fake_ps)
        self.assertEqual(res.returncode, 0)
        self.assertIn("combos=0/1", res.stdout)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        if os.path.exists(today_dir):
            pngs = [f for f in os.listdir(today_dir) if f.endswith(".png")]
            self.assertEqual(len(pngs), 0)

    def test_11_same_name_no_overwrite(self):
        """Test same name adds _1, _2 without overwriting."""
        cfg = self.make_config(png_every_n_runs=1)
        fixed_run_id = "20261019T120000Z"
        fake_ps = self.make_fake_powershell("ok", run_id=fixed_run_id)

        # First run
        res1 = self.run_wrapper(cfg, fake_ps, extra_args=["--run-id", fixed_run_id])
        self.assertEqual(res1.returncode, 0)

        # Second run with same run_id / base name
        res2 = self.run_wrapper(cfg, fake_ps, extra_args=["--run-id", fixed_run_id])
        self.assertEqual(res2.returncode, 0)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        pngs = sorted([f for f in os.listdir(today_dir) if f.endswith(".png")])
        self.assertGreaterEqual(len(pngs), 2)
        self.assertTrue(any("_1.png" in f for f in pngs), f"Expected _1 suffix in pngs: {pngs}")

    def test_12_retention_days(self):
        """Test retention days prunes directories older than retention_days."""
        cfg = self.make_config(retention_days=60)
        # Create an 80 days old directory and a 10 days old directory
        old_day = (datetime.now(timezone.utc) - timedelta(days=80)).strftime("%Y-%m-%d")
        keep_day = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")

        old_dir = os.path.join(self.data_root, old_day)
        keep_dir = os.path.join(self.data_root, keep_day)
        os.makedirs(old_dir, mode=0o700, exist_ok=True)
        os.makedirs(keep_dir, mode=0o700, exist_ok=True)

        old_pfx = f"{old_day.replace('-', '')}T100000Z_BTC_pair_24h_m1"
        keep_pfx = f"{keep_day.replace('-', '')}T100000Z_BTC_pair_24h_m1"
        with open(os.path.join(old_dir, f"{old_pfx}.png"), "wb") as f:
            f.write(b"old")
        with open(os.path.join(keep_dir, f"{keep_pfx}.png"), "wb") as f:
            f.write(b"keep")

        fake_ps = self.make_fake_powershell("ok")
        res = self.run_wrapper(cfg, fake_ps, extra_args=["--prune-only"])
        self.assertEqual(res.returncode, 0)

        self.assertFalse(os.path.exists(old_dir), "Old directory > 60 days must be pruned")
        self.assertTrue(os.path.exists(keep_dir), "Recent directory < 60 days must be kept")

    def test_13_retention_bytes_oldest_first_protect_today_newest(self):
        """Test total bytes limit prunes oldest first and protects today's newest run."""
        # Max bytes 15000
        cfg = self.make_config(retention_days=60, max_total_bytes=15000)

        past_day_old = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
        past_day_mid = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        past_dir_old = os.path.join(self.data_root, past_day_old)
        past_dir_mid = os.path.join(self.data_root, past_day_mid)
        today_dir = os.path.join(self.data_root, today_str)
        os.makedirs(past_dir_old, mode=0o700, exist_ok=True)
        os.makedirs(past_dir_mid, mode=0o700, exist_ok=True)
        os.makedirs(today_dir, mode=0o700, exist_ok=True)

        # Past day old: 6,000 bytes
        with open(os.path.join(past_dir_old, "20261016T100000Z_BTC_pair_24h_m1.png"), "wb") as f:
            f.write(b"A" * 6000)

        # Past day mid: 6,000 bytes
        with open(os.path.join(past_dir_mid, "20261018T100000Z_BTC_pair_24h_m1.png"), "wb") as f:
            f.write(b"B" * 6000)

        # Today run (newest): 6,000 bytes
        with open(os.path.join(today_dir, "20261019T020000Z_BTC_pair_24h_m1.png"), "wb") as f:
            f.write(b"C" * 6000)

        fake_ps = self.make_fake_powershell("ok")
        res = self.run_wrapper(cfg, fake_ps, extra_args=["--prune-only"])
        self.assertEqual(res.returncode, 0)

        # Total was 18,000 bytes; limit is 15,000 bytes.
        # Pruning 1 group (6,000 bytes) brings total to 12,000 bytes.
        # Oldest past run must be pruned
        self.assertFalse(os.path.exists(os.path.join(past_dir_old, "20261016T100000Z_BTC_pair_24h_m1.png")),
                         "Oldest past run must be pruned first")
        # Newer past run must be kept
        self.assertTrue(os.path.exists(os.path.join(past_dir_mid, "20261018T100000Z_BTC_pair_24h_m1.png")),
                        "Mid past run must be kept when oldest-first satisfies byte limit")
        # Today newest run must NEVER be pruned
        self.assertTrue(os.path.exists(os.path.join(today_dir, "20261019T020000Z_BTC_pair_24h_m1.png")),
                        "Today's newest run must be preserved")

    def test_14_refuse_symlink_and_outside_paths(self):
        """Test prune ignores symlinks and outside paths."""
        outside_dir = os.path.join(self.test_dir, "outside_dir")
        os.makedirs(outside_dir, exist_ok=True)
        outside_file = os.path.join(outside_dir, "sensitive.txt")
        with open(outside_file, "w") as f:
            f.write("DO NOT DELETE")

        # Create symlink inside data_root to outside_dir
        symlink_dir = os.path.join(self.data_root, "2020-01-01")
        try:
            os.symlink(outside_dir, symlink_dir)
        except OSError:
            pass

        cfg = self.make_config(retention_days=1)
        fake_ps = self.make_fake_powershell("ok")
        res = self.run_wrapper(cfg, fake_ps, extra_args=["--prune-only"])
        self.assertEqual(res.returncode, 0)

        self.assertTrue(os.path.exists(outside_file), "Outside sensitive file was deleted!")

    def test_15_flock_skipped_locked(self):
        """Test concurrent run triggers skipped_locked and exit 0."""
        lock_file = os.path.join(self.data_root, ".lock")
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            cfg = self.make_config()
            fake_ps = self.make_fake_powershell("ok")
            res = self.run_wrapper(cfg, fake_ps)
            self.assertEqual(res.returncode, 0)
            self.assertTrue(res.stdout.startswith("ERROR heatmap-archive status=skipped_locked"))

            runs_path = os.path.join(self.data_root, "runs.jsonl")
            self.assertTrue(os.path.exists(runs_path))
            with open(runs_path, "r", encoding="utf-8") as f:
                records = [json.loads(line) for line in f if line.strip()]
            self.assertEqual(records[-1]["status"], "skipped_locked")
            self.assertEqual(records[-1]["combos_total"], 0)
            self.assertEqual(records[-1]["combos_ok"], 0)
            self.assertEqual(records[-1]["bytes_archived"], 0)

            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            today_dir = os.path.join(self.data_root, today_str)
            self.assertFalse(os.path.exists(today_dir), "No date directory should be created on skipped_locked")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_16_dry_run_no_archive_no_runs_jsonl(self):
        """Test --dry-run does not write archive files and does not write runs.jsonl."""
        cfg = self.make_config()
        fake_ps = self.make_fake_powershell("ok")
        res = self.run_wrapper(cfg, fake_ps, extra_args=["--dry-run"])
        self.assertEqual(res.returncode, 0)
        self.assertTrue(res.stdout.startswith("DRYRUN"))

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        self.assertFalse(os.path.exists(today_dir), "Dry run must not create archive directory")

        runs_path = os.path.join(self.data_root, "runs.jsonl")
        self.assertFalse(os.path.exists(runs_path), "Dry run must not create runs.jsonl")

    def test_17_selftest_passes(self):
        """Test --selftest runs and exits 0."""
        res = subprocess.run([sys.executable, ARCHIVE_PY, "--selftest"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Selftest failed: {res.stderr}")
        self.assertIn("SELFTEST OK", res.stdout)

    def test_18_ps1_static_analysis(self):
        """Static test on heatmap_capture.ps1 for required checks and banned terms."""
        with open(CAPTURE_PS1, "r", encoding="utf-8") as f:
            content = f.read()

        # Required keywords
        self.assertIn("19301", content, "ps1 must contain port whitelist 19301")
        self.assertIn("CoinglassDedicatedChrome", content, "ps1 must check CoinglassDedicatedChrome")
        self.assertIn("clearDeviceMetricsOverride", content, "ps1 must call clearDeviceMetricsOverride")
        finally_idx = content.rfind("finally")
        self.assertNotEqual(finally_idx, -1, "ps1 must have a finally block")
        clear_idx = content.find("clearDeviceMetricsOverride", finally_idx)
        self.assertNotEqual(clear_idx, -1, "clearDeviceMetricsOverride must appear after finally")

        # Banned keywords (case-insensitive)
        banned = ["getCookies", "document.cookie", "localStorage", "Storage.", "19242", "19300"]
        content_lower = content.lower()
        for b in banned:
            self.assertNotIn(b.lower(), content_lower, f"ps1 contains banned keyword: {b}")

        # Extra assertion: clearDeviceMetricsOverride in finally must be active code (not comment)
        finally_lines = content[finally_idx:].splitlines()
        active_finally = [line.strip() for line in finally_lines if line.strip() and not line.strip().startswith("#")]
        self.assertTrue(
            any("clearDeviceMetricsOverride" in line for line in active_finally),
            "clearDeviceMetricsOverride must appear as active code in finally block"
        )

    def test_19_ps1_bring_to_front_and_focus_emulation(self):
        """Test ps1 contains bringToFront and setFocusEmulationEnabled with true and false."""
        with open(CAPTURE_PS1, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertTrue(
            bool(re.search(r"^\s*Invoke-CdpCall.*Page\.bringToFront", content, re.MULTILINE)),
            "ps1 must contain active Invoke-CdpCall with Page.bringToFront"
        )
        # Check setFocusEmulationEnabled enabled true
        self.assertTrue(
            bool(re.search(r"Invoke-CdpCall.*setFocusEmulationEnabled.*enabled\s*=\s*\$true", content, re.DOTALL | re.IGNORECASE)),
            "ps1 must call setFocusEmulationEnabled with enabled:true"
        )
        # Check setFocusEmulationEnabled enabled false
        self.assertTrue(
            bool(re.search(r"setFocusEmulationEnabled.*enabled\s*=\s*\$false", content, re.DOTALL | re.IGNORECASE)),
            "ps1 must call setFocusEmulationEnabled with enabled:false in finally"
        )
        # Extra assertion: ensure the initial setFocusEmulationEnabled call block contains enabled = $true without crossing other statements
        self.assertTrue(
            bool(re.search(r"Method\s+['\"]Emulation\.setFocusEmulationEnabled['\"]\s+-Params\s+@\{\s*enabled\s*=\s*\$true\s*\}", content)),
            "ps1 must call setFocusEmulationEnabled with enabled = $true directly in params"
        )

    def test_20_ps1_passive_login_and_no_header_button(self):
        """Test ps1 uses passive login scan with Log out/Login and no header button clicks."""
        with open(CAPTURE_PS1, "r", encoding="utf-8") as f:
            content = f.read()
        # Must contain Log out and Login for passive leaf node checking
        self.assertIn("Log out", content, "ps1 must check for 'Log out'")
        self.assertIn("Login", content, "ps1 must check for 'Login'")
        # Must NOT contain header button
        self.assertNotIn("header button", content, "ps1 must not click header button")
        # Must invoke login poll with (10000, 500)
        self.assertIn("($loginPollJs)(10000, 500)", content, "ps1 must invoke login poll with (10000, 500)")

    def test_21_page_hidden_in_metadata(self):
        """Test page_hidden warning and hidden visibility are written to metadata JSON."""
        cfg = self.make_config()
        fake_ps = self.make_fake_powershell("page_hidden")
        res = self.run_wrapper(cfg, fake_ps)
        self.assertEqual(res.returncode, 0)
        self.assertIn("OK heatmap-archive", res.stdout)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        json_files = [f for f in os.listdir(today_dir) if f.endswith(".json")]
        self.assertEqual(len(json_files), 1)

        meta_path = os.path.join(today_dir, json_files[0])
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        self.assertEqual(meta.get("visibility"), "hidden")
        self.assertIn("page_hidden", meta.get("warnings", []), "page_hidden must be in metadata warnings")

    def test_22_runs_jsonl_contains_warnings(self):
        """Test runs.jsonl contains warnings field on both normal and page_hidden runs."""
        # 1. Normal run
        cfg = self.make_config()
        fake_ps = self.make_fake_powershell("ok")
        res = self.run_wrapper(cfg, fake_ps)
        self.assertEqual(res.returncode, 0)

        runs_path = os.path.join(self.data_root, "runs.jsonl")
        self.assertTrue(os.path.exists(runs_path))
        with open(runs_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertGreaterEqual(len(lines), 1)
        self.assertIn("warnings", lines[-1], "runs.jsonl must contain warnings field")
        self.assertIsInstance(lines[-1]["warnings"], list)
        self.assertEqual(lines[-1]["warnings"], [])

        # 2. Run with page_hidden
        fake_ps_hidden = self.make_fake_powershell("page_hidden")
        res2 = self.run_wrapper(cfg, fake_ps_hidden)
        self.assertEqual(res2.returncode, 0)

        with open(runs_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertIn("warnings", lines[-1])
        self.assertIn("page_hidden", lines[-1]["warnings"], "runs.jsonl must record page_hidden in warnings")

    def test_23_stale_data_warning_in_metadata_and_runs_jsonl(self):
        """Test stale_data warning is written to metadata JSON and runs.jsonl."""
        cfg = self.make_config()
        stale_ps = os.path.join(self.test_dir, "fake_ps_stale.py")
        old_time_str = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%d %b %Y, %H:%M")
        with open(stale_ps, "w") as f:
            f.write(f"""#!/usr/bin/env python3
import sys, json, os, shutil
out_dir = None
for i, arg in enumerate(sys.argv):
    if arg == "-OutDir" and i + 1 < len(sys.argv):
        out_dir = sys.argv[i + 1]
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    shutil.copy2("{SAMPLE_PNG}", os.path.join(out_dir, "20261019T173000Z_BTC_pair_24h_m1.png"))
    shutil.copy2("{SAMPLE_CHART}", os.path.join(out_dir, "20261019T173000Z_BTC_pair_24h_m1.chart.json.gz"))
out = {{
    "schema": 1, "status": "ok",
    "chrome": {{"version": "155", "port": 19301, "owner_ok": True}},
    "page": {{"title": "Coinglass", "url": "https://www.coinglass.com/pro/futures/LiquidationHeatMap", "tabs": []}},
    "logged_in": True, "visibility": "visible", "warnings": [], "viewport": [1400, 1200],
    "browser_tz_offset_min": 0,
    "results": [{{
        "coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1,
        "ok": True, "error": None, "login_required": False,
        "header": "Binance BTC/USDT Liquidation Heatmap",
        "price": 81903.0, "y_min": 77616.52, "y_max": 86359.12,
        "n_x": 288, "n_y": 136, "x_first": "19 Oct 2026, 17:00", "x_last": "{old_time_str}",
        "canvas": [1055, 730], "png_file": "20261019T173000Z_BTC_pair_24h_m1.png",
        "png_sha256": "{self.png_sha}", "chart_file": "20261019T173000Z_BTC_pair_24h_m1.chart.json.gz",
        "chart_sha256": "{self.chart_sha}", "chart_data_ok": True, "elapsed_ms": 1000
    }}]
}}
print(json.dumps(out))
""")
        os.chmod(stale_ps, 0o755)
        res = self.run_wrapper(cfg, stale_ps)
        self.assertEqual(res.returncode, 0)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        json_files = [f for f in os.listdir(today_dir) if f.endswith(".json")]
        meta_path = os.path.join(today_dir, json_files[0])
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertIn("stale_data", meta.get("warnings", []), "stale_data must be in metadata warnings")

        runs_path = os.path.join(self.data_root, "runs.jsonl")
        with open(runs_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertIn("stale_data", lines[-1]["warnings"], "stale_data must be in runs.jsonl warnings")

    def test_24_login_required_branch_and_counting(self):
        """Test login_required branch, hostile variant with ok=true and files, and exact counting."""
        cfg = self.make_config(combos=[
            {"coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True},
            {"coin": "ETH", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True}
        ], png_every_n_runs=1)

        # Hostile fake powershell: combo 2 says ok=True, login_required=True, and stages png and chart files
        hostile_ps = os.path.join(self.test_dir, "fake_ps_hostile_login.py")
        curr_xl = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M")
        with open(hostile_ps, "w") as f:
            f.write(f"""#!/usr/bin/env python3
import sys, json, os, shutil
out_dir = None
for i, arg in enumerate(sys.argv):
    if arg == "-OutDir" and i + 1 < len(sys.argv):
        out_dir = sys.argv[i + 1]
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    shutil.copy2("{SAMPLE_PNG}", os.path.join(out_dir, "run1_BTC_pair_24h_m1.png"))
    shutil.copy2("{SAMPLE_CHART}", os.path.join(out_dir, "run1_BTC_pair_24h_m1.chart.json.gz"))
    shutil.copy2("{SAMPLE_PNG}", os.path.join(out_dir, "run1_ETH_pair_24h_m1.png"))
    shutil.copy2("{SAMPLE_CHART}", os.path.join(out_dir, "run1_ETH_pair_24h_m1.chart.json.gz"))

out = {{
    "schema": 1, "status": "partial",
    "chrome": {{"version": "155", "port": 19301, "owner_ok": True}},
    "page": {{"title": "Coinglass", "url": "https://www.coinglass.com/pro/futures/LiquidationHeatMap", "tabs": []}},
    "logged_in": False, "login_evidence": "login_leaf", "login_wait_ms": 100,
    "visibility": "visible", "focus_mode": "emulation_only", "warnings": [], "viewport": [1400, 1200],
    "browser_tz_offset_min": 0,
    "results": [
        {{
            "coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1,
            "ok": True, "error": None, "login_required": False,
            "header": "Binance BTC/USDT Liquidation Heatmap",
            "price": 81903.0, "y_min": 77616.52, "y_max": 86359.12,
            "n_x": 288, "n_y": 136, "x_first": "19 Oct 2026, 17:00", "x_last": "{curr_xl}",
            "canvas": [1055, 730], "png_file": "run1_BTC_pair_24h_m1.png", "png_sha256": "{self.png_sha}",
            "chart_file": "run1_BTC_pair_24h_m1.chart.json.gz", "chart_sha256": "{self.chart_sha}",
            "chart_data_ok": True, "elapsed_ms": 1000
        }},
        {{
            "coin": "ETH", "mode": "pair", "timeframe": "24 hour", "model": 1,
            "ok": True, "error": None, "login_required": True,
            "header": "Binance ETH/USDT Liquidation Heatmap",
            "price": 2500.0, "y_min": 2000.0, "y_max": 3000.0,
            "n_x": 288, "n_y": 136, "x_first": "19 Oct 2026, 17:00", "x_last": "{curr_xl}",
            "canvas": [1055, 730], "png_file": "run1_ETH_pair_24h_m1.png", "png_sha256": "{self.png_sha}",
            "chart_file": "run1_ETH_pair_24h_m1.chart.json.gz", "chart_sha256": "{self.chart_sha}",
            "chart_data_ok": True, "elapsed_ms": 1000
        }}
    ]
}}
print(json.dumps(out))
""")
        os.chmod(hostile_ps, 0o755)
        res = self.run_wrapper(cfg, hostile_ps)
        self.assertEqual(res.returncode, 0)
        self.assertIn("combos=1/2", res.stdout)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        # That combo (ETH) must write NO files
        eth_files = [f for f in os.listdir(today_dir) if "ETH" in f]
        self.assertEqual(len(eth_files), 0, "login_required combo must write NO files even if hostile ps supplied them")

        runs_path = os.path.join(self.data_root, "runs.jsonl")
        with open(runs_path, "r", encoding="utf-8") as f:
            rec = [json.loads(line) for line in f if line.strip()][-1]
        self.assertEqual(rec["status"], "partial")
        self.assertEqual(rec["combos_login_required"], 1)
        self.assertEqual(rec["combos_ok"], 1)
        self.assertEqual(rec["combos_total"], 2)

        # Per-combo result entry for ETH has ok: False and error: "login_required"
        eth_res = [r for r in rec["results"] if r.get("coin") == "ETH"][0]
        self.assertFalse(eth_res["ok"])
        self.assertEqual(eth_res["error"], "login_required")

        # Test count with 2 login_required combos
        hostile_ps2 = os.path.join(self.test_dir, "fake_ps_hostile_login2.py")
        with open(hostile_ps2, "w") as f:
            f.write(f"""#!/usr/bin/env python3
import sys, json
out = {{
    "schema": 1, "status": "partial",
    "chrome": {{"version": "155", "port": 19301, "owner_ok": True}},
    "page": {{"title": "Coinglass", "url": "https://www.coinglass.com/pro/futures/LiquidationHeatMap", "tabs": []}},
    "logged_in": False, "login_evidence": "login_leaf", "login_wait_ms": 100,
    "visibility": "visible", "focus_mode": "emulation_only", "warnings": [], "viewport": [1400, 1200],
    "browser_tz_offset_min": 0,
    "results": [
        {{ "coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "ok": False, "login_required": True, "error": "login_required" }},
        {{ "coin": "ETH", "mode": "pair", "timeframe": "24 hour", "model": 1, "ok": False, "login_required": True, "error": "login_required" }}
    ]
}}
print(json.dumps(out))
""")
        os.chmod(hostile_ps2, 0o755)
        res2 = self.run_wrapper(cfg, hostile_ps2)
        self.assertEqual(res2.returncode, 0)
        with open(runs_path, "r", encoding="utf-8") as f:
            rec2 = [json.loads(line) for line in f if line.strip()][-1]
        self.assertEqual(rec2["combos_login_required"], 2)

    def test_25_skipped_locked_exact_prefix_and_record(self):
        """Assert stdout starts with ERROR heatmap-archive status=skipped_locked, combos 0, bytes 0, no date dir."""
        lock_file = os.path.join(self.data_root, ".lock")
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            cfg = self.make_config()
            fake_ps = self.make_fake_powershell("ok")
            res = self.run_wrapper(cfg, fake_ps)
            self.assertEqual(res.returncode, 0)
            first_line = res.stdout.strip().splitlines()[0]
            self.assertTrue(first_line.startswith("ERROR heatmap-archive status=skipped_locked"),
                            f"First line does not start with expected prefix: {first_line}")

            runs_path = os.path.join(self.data_root, "runs.jsonl")
            self.assertTrue(os.path.exists(runs_path))
            with open(runs_path, "r", encoding="utf-8") as f:
                records = [json.loads(line) for line in f if line.strip()]
            rec = records[-1]
            self.assertEqual(rec["status"], "skipped_locked")
            self.assertEqual(rec["combos_total"], 0)
            self.assertEqual(rec["combos_ok"], 0)
            self.assertEqual(rec["bytes_archived"], 0)

            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            today_dir = os.path.join(self.data_root, today_str)
            self.assertFalse(os.path.exists(today_dir), "No date directory should be created on skipped_locked")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_26_ps1_security_zero_cookie_storage_network_get(self):
        """Static test: ps1 contains zero occurrences of cookie, localStorage, sessionStorage, indexedDB, Storage., Network.get."""
        with open(CAPTURE_PS1, "r", encoding="utf-8") as f:
            content = f.read()
        content_lower = content.lower()

        banned_terms = ["cookie", "localstorage", "sessionstorage", "indexeddb", "storage.", "network.get"]
        for term in banned_terms:
            self.assertNotIn(term, content_lower, f"heatmap_capture.ps1 contains banned term: {term}")

    def test_27_archive_zero_cookie_localstorage(self):
        """Static test: heatmap_archive.py (excluding BANNED_PS1_TERMS list definition) contains zero cookie and zero localStorage."""
        with open(ARCHIVE_PY, "r", encoding="utf-8") as f:
            lines = f.readlines()

        filtered_lines = []
        in_banned_list = False
        for line in lines:
            if "BANNED_PS1_TERMS = [" in line:
                in_banned_list = True
                continue
            if in_banned_list and "]" in line:
                in_banned_list = False
                continue
            if not in_banned_list:
                filtered_lines.append(line)

        code_without_banned_list = "".join(filtered_lines).lower()
        self.assertNotIn("cookie", code_without_banned_list, "heatmap_archive.py contains 'cookie' outside banned terms list")
        self.assertNotIn("localstorage", code_without_banned_list, "heatmap_archive.py contains 'localstorage' outside banned terms list")

    def test_28_runtime_banned_term_guard(self):
        """Runtime guard test: if ps1 contains document.cookie, wrapper outputs ERROR, exits 0, no powershell execution."""
        temp_ps1 = os.path.join(self.test_dir, "banned_capture.ps1")
        with open(CAPTURE_PS1, "r", encoding="utf-8") as f:
            ps_code = f.read()
        with open(temp_ps1, "w", encoding="utf-8") as f:
            f.write(ps_code + "\n# document.cookie\n")

        marker_file = os.path.join(self.test_dir, "powershell_was_executed.marker")
        fake_ps = os.path.join(self.test_dir, "fake_ps_marker.py")
        with open(fake_ps, "w") as f:
            f.write(f"""#!/usr/bin/env python3
import sys
open("{marker_file}", "w").write("EXECUTED")
print('{{"schema":1,"status":"ok","results":[]}}')
""")
        os.chmod(fake_ps, 0o755)

        cfg = self.make_config()
        cmd = [
            sys.executable, ARCHIVE_PY,
            "--config", cfg,
            "--data-root", self.data_root,
            "--ps-script", temp_ps1,
            "--powershell", f"python3 {fake_ps}"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("ERROR heatmap-archive status=powershell_error detail=banned_term:", res.stdout)
        self.assertFalse(os.path.exists(marker_file), "Powershell must NOT be executed when banned term is present")

        runs_path = os.path.join(self.data_root, "runs.jsonl")
        self.assertTrue(os.path.exists(runs_path))
        with open(runs_path, "r", encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(records[-1]["status"], "powershell_error")
        self.assertIn("banned_term:", records[-1]["error"])

    def test_29_ps1_no_click_or_events_on_login_elements(self):
        """Assert ps1 has no 'header button', 'querySelector(\\'header button\\')', 'Log out' click, and login poll has no click/dispatchEvent/cookie/mouse."""
        with open(CAPTURE_PS1, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertNotIn("header button", content)
        self.assertNotIn("querySelector('header button')", content)
        self.assertNotIn("querySelector(\"header button\")", content)
        self.assertNotIn("Log out.click", content)
        self.assertNotIn("'Log out').click", content)

        # Extract loginPollJs
        m = re.search(r"\$loginPollJs\s*=\s*@'\r?\n(.*?)\r?\n'@", content, re.DOTALL)
        self.assertIsNotNone(m, "$loginPollJs here-string must exist")
        login_poll_code = m.group(1).lower()

        for term in ["click", "dispatchevent", "cookie", "mouse"]:
            self.assertNotIn(term, login_poll_code, f"$loginPollJs contains forbidden word: {term}")

    def test_30_node_login_poll_behavior(self):
        """Test login poll JS in node.js with fake DOM covering all 6 cases."""
        node_bin = shutil.which("node")
        if not node_bin:
            self.fail("node is required for embedded JavaScript tests but was not found in PATH")

        with open(CAPTURE_PS1, "r", encoding="utf-8") as f:
            ps_content = f.read()

        m = re.search(r"\$loginPollJs\s*=\s*@'\r?\n(.*?)\r?\n'@", ps_content, re.DOTALL)
        self.assertIsNotNone(m, "$loginPollJs here-string must exist")
        js_fn = m.group(1).strip()

        node_script = f"""
const loginPollJs = ({js_fn});

class FakeElement {{
  constructor(tag, text = "", children = [], rect = {{}}) {{
    this.tagName = tag.toUpperCase();
    this.innerText = text;
    this.textContent = text;
    this.children = children;
    this._rect = rect;
  }}
  querySelectorAll(sel) {{
    const res = [];
    const traverse = el => {{
      for (const ch of el.children) {{
        if (sel === "*" || ch.tagName.toLowerCase() === sel.toLowerCase()) {{
          res.push(ch);
        }}
        traverse(ch);
      }}
    }};
    traverse(this);
    return res;
  }}
  querySelector(sel) {{
    const traverse = el => {{
      for (const ch of el.children) {{
        if (ch.tagName.toLowerCase() === sel.toLowerCase()) return ch;
        const found = traverse(ch);
        if (found) return found;
      }}
      return null;
    }};
    return traverse(this);
  }}
  getBoundingClientRect() {{
    return {{
      top: this._rect.top !== undefined ? this._rect.top : 0,
      bottom: this._rect.bottom !== undefined ? this._rect.bottom : 50,
      left: this._rect.left !== undefined ? this._rect.left : 0,
      right: this._rect.right !== undefined ? this._rect.right : 100,
      width: this._rect.width !== undefined ? this._rect.width : 100,
      height: this._rect.height !== undefined ? this._rect.height : 50,
    }};
  }}
}}

function createEnv(width = 1400) {{
  const root = new FakeElement("body", "", []);
  const doc = {{
    body: root,
    querySelectorAll(sel) {{
      if (sel === "*") {{
        const all = [root];
        const traverse = el => {{
          for (const ch of el.children) {{
            all.push(ch);
            traverse(ch);
          }}
        }};
        traverse(root);
        return all;
      }}
      return root.querySelectorAll(sel);
    }},
    querySelector(sel) {{
      return root.querySelector(sel);
    }}
  }};
  return {{ doc, win: {{ innerWidth: width, document: doc }} }};
}}

async function runTests() {{
  const results = {{}};

  // (i) Login BUTTON appears after 400ms -> false/login_leaf, waited_ms between 2300 and 3200
  {{
    const {{ doc, win }} = createEnv(1400);
    global.document = doc; global.window = win;
    setTimeout(() => {{
      doc.body.children.push(new FakeElement("button", "Login", []));
    }}, 400);
    const res = JSON.parse(await loginPollJs(4000, 50));
    results.case1 = res;
  }}

  // (ii) Log out leaf appears after 300ms -> true/logout_leaf
  {{
    const {{ doc, win }} = createEnv(1400);
    global.document = doc; global.window = win;
    setTimeout(() => {{
      doc.body.children.push(new FakeElement("div", "Log out", []));
    }}, 300);
    const res = JSON.parse(await loginPollJs(1500, 50));
    results.case2 = res;
  }}

  // (iii) header svg-only button appearing after 300ms and staying -> true/user_menu_button
  {{
    const {{ doc, win }} = createEnv(1400);
    global.document = doc; global.window = win;
    const header = new FakeElement("header", "", []);
    doc.body.children.push(header);
    setTimeout(() => {{
      const svg = new FakeElement("svg", "", []);
      const btn = new FakeElement("button", "", [svg], {{ right: 1380, top: 47, bottom: 90 }});
      header.children.push(btn);
    }}, 300);
    const res = JSON.parse(await loginPollJs(1500, 50));
    results.case3_stay = res;
  }}

  // (iii-b) header svg button only exists for 1 poll (vanishes after 40ms) -> does not decide
  {{
    const {{ doc, win }} = createEnv(1400);
    global.document = doc; global.window = win;
    const header = new FakeElement("header", "", []);
    doc.body.children.push(header);
    setTimeout(() => {{
      const svg = new FakeElement("svg", "", []);
      const btn = new FakeElement("button", "", [svg], {{ right: 1380, top: 47, bottom: 90 }});
      header.children.push(btn);
      setTimeout(() => {{
        header.children = [];
      }}, 40);
    }}, 300);
    const res = JSON.parse(await loginPollJs(500, 50));
    results.case3_vanish = res;
  }}

  // (iii-c) header svg button on left side (right: 200 < innerWidth - 40) -> unknown/timeout
  {{
    const {{ doc, win }} = createEnv(1400);
    global.document = doc; global.window = win;
    const header = new FakeElement("header", "", []);
    doc.body.children.push(header);
    const svg = new FakeElement("svg", "", []);
    const btn = new FakeElement("button", "", [svg], {{ right: 200, top: 47, bottom: 90 }});
    header.children.push(btn);
    const res = JSON.parse(await loginPollJs(300, 50));
    results.case3_left = res;
  }}

  // (iv) nothing ever appears -> unknown/timeout with waited_ms >= maxMs - 100
  {{
    const {{ doc, win }} = createEnv(1400);
    global.document = doc; global.window = win;
    const res = JSON.parse(await loginPollJs(500, 50));
    results.case4 = res;
  }}

  // (v) mobile width 958 with svg button -> unknown/timeout
  {{
    const {{ doc, win }} = createEnv(958);
    global.document = doc; global.window = win;
    const header = new FakeElement("header", "", []);
    doc.body.children.push(header);
    const svg = new FakeElement("svg", "", []);
    const btn = new FakeElement("button", "", [svg], {{ right: 940, top: 47, bottom: 90 }});
    header.children.push(btn);
    const res = JSON.parse(await loginPollJs(400, 50));
    results.case5 = res;
  }}

  // (vi) Login present together with svg button -> false/login_leaf
  {{
    const {{ doc, win }} = createEnv(1400);
    global.document = doc; global.window = win;
    const header = new FakeElement("header", "", []);
    doc.body.children.push(header);
    const svg = new FakeElement("svg", "", []);
    const btn = new FakeElement("button", "", [svg], {{ right: 1380, top: 47, bottom: 90 }});
    header.children.push(btn);
    doc.body.children.push(new FakeElement("button", "Login", []));
    const res = JSON.parse(await loginPollJs(4000, 50));
    results.case6 = res;
  }}

  console.log(JSON.stringify(results));
}}

runTests();
"""
        res = subprocess.run([node_bin, "-e", node_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Node script failed: {res.stderr}")
        data = json.loads(res.stdout.strip())

        # (i)
        self.assertEqual(data["case1"]["state"], "false")
        self.assertEqual(data["case1"]["evidence"], "login_leaf")
        self.assertGreaterEqual(data["case1"]["waited_ms"], 2300)
        self.assertLessEqual(data["case1"]["waited_ms"], 3200)

        # (ii)
        self.assertEqual(data["case2"]["state"], "true")
        self.assertEqual(data["case2"]["evidence"], "logout_leaf")

        # (iii)
        self.assertEqual(data["case3_stay"]["state"], "true")
        self.assertEqual(data["case3_stay"]["evidence"], "user_menu_button")
        self.assertEqual(data["case3_vanish"]["state"], "unknown")
        self.assertEqual(data["case3_vanish"]["evidence"], "timeout")
        self.assertEqual(data["case3_left"]["state"], "unknown")
        self.assertEqual(data["case3_left"]["evidence"], "timeout")

        # (iv)
        self.assertEqual(data["case4"]["state"], "unknown")
        self.assertEqual(data["case4"]["evidence"], "timeout")
        self.assertGreaterEqual(data["case4"]["waited_ms"], 400)  # maxMs 500 - 100

        # (v)
        self.assertEqual(data["case5"]["state"], "unknown")
        self.assertEqual(data["case5"]["evidence"], "timeout")

        # (vi)
        self.assertEqual(data["case6"]["state"], "false")
        self.assertEqual(data["case6"]["evidence"], "login_leaf")

    def test_31_ps1_focus_emulation_static(self):
        """Static test on ps1: focus emulation order, bringToFront inside hidden check, focusMode strings, SavePng param, output fields."""
        with open(CAPTURE_PS1, "r", encoding="utf-8") as f:
            content = f.read()

        idx_focus = content.find("Emulation.setFocusEmulationEnabled")
        idx_bring = content.find("Page.bringToFront")
        self.assertNotEqual(idx_focus, -1, "ps1 must contain Emulation.setFocusEmulationEnabled")
        self.assertNotEqual(idx_bring, -1, "ps1 must contain Page.bringToFront")
        self.assertLess(idx_focus, idx_bring, "setFocusEmulationEnabled true must appear BEFORE Page.bringToFront")

        # Page.bringToFront must be textually inside if ($pageVisibility -eq 'hidden')
        hidden_block = re.search(r"if\s*\(\$pageVisibility\s*-eq\s*['\"]hidden['\"]\)\s*\{([^}]+)\}", content)
        self.assertIsNotNone(hidden_block, "ps1 must have if ($pageVisibility -eq 'hidden') block")
        self.assertIn("Page.bringToFront", hidden_block.group(1), "Page.bringToFront must be inside hidden check block")

        self.assertIn("emulation_only", content)
        self.assertIn("bringToFront_fallback", content)
        self.assertTrue(bool(re.search(r"\[int\]\$SavePng\s*=\s*1", content)))
        self.assertIn("focus_mode", content)
        self.assertIn("login_evidence", content)

    def test_32_png_cadence_5_runs(self):
        """Test PNG cadence over 5 consecutive runs: PNG only on run 1 and 5, state.json mode 0600."""
        cfg = self.make_config(png_every_n_runs=4)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)

        saved_flags = []
        for run_idx in range(5):
            existing_jsons = set(os.listdir(today_dir)) if os.path.exists(today_dir) else set()
            fake_ps = self.make_fake_powershell("ok", run_id=f"run_{run_idx}")
            res = self.run_wrapper(cfg, fake_ps)
            self.assertEqual(res.returncode, 0)
            if run_idx in (0, 4):
                self.assertIn("png=1", res.stdout)
            else:
                self.assertIn("png=0", res.stdout)

            # Check metadata for this run
            new_jsons = [f for f in os.listdir(today_dir) if f.endswith(".json") and f not in existing_jsons]
            self.assertEqual(len(new_jsons), 1)
            new_json = new_jsons[0]
            base_name = new_json[:-5]

            meta_file = os.path.join(today_dir, new_json)
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
            saved_flags.append(meta["png_saved"])

            png_file = os.path.join(today_dir, f"{base_name}.png")
            if run_idx in (0, 4):
                self.assertTrue(os.path.exists(png_file), f"Run {run_idx+1} must save PNG")
                self.assertIsNotNone(meta["png_sha256"])
            else:
                self.assertFalse(os.path.exists(png_file), f"Run {run_idx+1} must NOT save PNG")
                self.assertIsNone(meta["png_sha256"])

            # Chart and json must be present for all runs
            chart_file = os.path.join(today_dir, f"{base_name}.chart.json.gz")
            self.assertTrue(os.path.exists(chart_file))

        self.assertEqual(saved_flags, [True, False, False, False, True])

        # Check state.json
        state_path = os.path.join(self.data_root, "state.json")
        self.assertTrue(os.path.exists(state_path))
        self.assertEqual(os.stat(state_path).st_mode & 0o777, 0o600)
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["png_run_counter"], 5)

    def test_33_png_cadence_edge_cases(self):
        """Test PNG cadence edge cases: N=1 saves every run, no chart forces PNG, errors/dry-run leave counter unchanged, corrupt state."""
        # 1. png_every_n_runs = 1 saves every run
        cfg_every1 = self.make_config(png_every_n_runs=1)
        fake_ps = self.make_fake_powershell("ok", run_id="20261019T110100Z")
        res = self.run_wrapper(cfg_every1, fake_ps)
        self.assertEqual(res.returncode, 0)
        self.assertIn("png=1", res.stdout)

        # 2. Dry run does not increment counter
        state_before = os.path.join(self.data_root, "state.json")
        with open(state_before, "r", encoding="utf-8") as f:
            cnt_before = json.load(f)["png_run_counter"]
        res_dry = self.run_wrapper(cfg_every1, fake_ps, extra_args=["--dry-run"])
        self.assertEqual(res_dry.returncode, 0)
        with open(state_before, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["png_run_counter"], cnt_before, "Dry run must NOT change counter")

        # 3. Failed run (bad_json) does not increment counter
        fake_bad = self.make_fake_powershell("bad_json")
        self.run_wrapper(cfg_every1, fake_bad)
        with open(state_before, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["png_run_counter"], cnt_before, "Failed run must NOT change counter")

        # 4. Off-cadence run with chart_data_ok=False still saves PNG
        # Set state counter to 1 with N=4
        with open(state_before, "w", encoding="utf-8") as f:
            json.dump({"png_run_counter": 1, "updated_utc": "2026-10-19T00:00:00Z"}, f)
        cfg_every4 = self.make_config(png_every_n_runs=4)
        no_chart_ps = os.path.join(self.test_dir, "fake_ps_no_chart.py")
        with open(no_chart_ps, "w") as f:
            f.write(f"""#!/usr/bin/env python3
import sys, json, os, shutil
out_dir = None
for i, arg in enumerate(sys.argv):
    if arg == "-OutDir" and i + 1 < len(sys.argv):
        out_dir = sys.argv[i + 1]
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    shutil.copy2("{SAMPLE_PNG}", os.path.join(out_dir, "run_nochart_BTC_pair_24h_m3.png"))
out = {{
    "schema": 1, "status": "ok",
    "chrome": {{"version": "155", "port": 19301, "owner_ok": True}},
    "page": {{"title": "Coinglass", "url": "https://www.coinglass.com/pro/futures/LiquidationHeatMap", "tabs": []}},
    "logged_in": True, "login_evidence": "user_menu_button", "login_wait_ms": 100,
    "visibility": "visible", "focus_mode": "bringToFront_fallback", "warnings": [], "viewport": [1400, 1200],
    "browser_tz_offset_min": 0,
    "results": [
        {{
            "coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 3,
            "ok": True, "error": None, "login_required": False,
            "header": "Binance BTC/USDT",
            "price": None, "y_min": None, "y_max": None, "n_x": 0, "n_y": 0,
            "x_first": "", "x_last": "",
            "canvas": [923, 800], "png_file": "run_nochart_BTC_pair_24h_m3.png", "png_sha256": "{self.png_sha}",
            "chart_file": None, "chart_sha256": None, "chart_data_ok": False, "elapsed_ms": 1000
        }}
    ]
}}
print(json.dumps(out))
""")
        os.chmod(no_chart_ps, 0o755)
        res_no_chart = self.run_wrapper(cfg_every4, no_chart_ps)
        self.assertEqual(res_no_chart.returncode, 0)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        m3_pngs = [f for f in os.listdir(today_dir) if f.endswith("_BTC_pair_24h_m3.png")]
        self.assertEqual(len(m3_pngs), 1, "Off-cadence run with chart_data_ok=False must still save PNG")
        png_path = os.path.join(today_dir, m3_pngs[0])
        self.assertTrue(os.path.exists(png_path))

        # Check metadata has focus_mode and login_evidence
        m3_metas = [f for f in os.listdir(today_dir) if f.endswith("_BTC_pair_24h_m3.json")]
        self.assertEqual(len(m3_metas), 1)
        meta_path = os.path.join(today_dir, m3_metas[0])
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["focus_mode"], "bringToFront_fallback")
        self.assertEqual(meta["login_evidence"], "user_menu_button")

        # Check runs.jsonl has focus_mode and login_evidence
        runs_path = os.path.join(self.data_root, "runs.jsonl")
        with open(runs_path, "r", encoding="utf-8") as f:
            rec = [json.loads(line) for line in f if line.strip()][-1]
        self.assertEqual(rec["focus_mode"], "bringToFront_fallback")
        self.assertEqual(rec["login_evidence"], "user_menu_button")

        # 5. Corrupt state.json treated as 0
        with open(state_before, "w", encoding="utf-8") as f:
            f.write("CORRUPT NOT JSON")
        # Next run should treat counter as 0 -> save PNG
        fake_ps_corrupt = self.make_fake_powershell("ok", run_id="20261019T110500Z")
        res_corrupt = self.run_wrapper(cfg_every4, fake_ps_corrupt)
        self.assertEqual(res_corrupt.returncode, 0)
        self.assertIn("png=1", res_corrupt.stdout)

    def test_34_retention_pngless_group(self):
        """Test retention days and byte-pruning work properly when a group has no .png."""
        cfg = self.make_config(retention_days=60, max_total_bytes=10000)

        past_day = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        past_dir = os.path.join(self.data_root, past_day)
        os.makedirs(past_dir, mode=0o700, exist_ok=True)

        # Create a png-less group in past_dir: only chart.json.gz (6000 bytes) and json (2000 bytes)
        pfx = "20261014T100000Z_BTC_pair_24h_m1"
        chart_p = os.path.join(past_dir, f"{pfx}.chart.json.gz")
        json_p = os.path.join(past_dir, f"{pfx}.json")
        with open(chart_p, "wb") as f:
            f.write(b"Z" * 6000)
        with open(json_p, "wb") as f:
            f.write(b"J" * 2000)

        # Create a group in today's dir (5000 bytes)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        os.makedirs(today_dir, mode=0o700, exist_ok=True)
        today_pfx = "20261019T100000Z_BTC_pair_24h_m1"
        with open(os.path.join(today_dir, f"{today_pfx}.png"), "wb") as f:
            f.write(b"P" * 5000)

        fake_ps = self.make_fake_powershell("ok")
        res = self.run_wrapper(cfg, fake_ps, extra_args=["--prune-only"])
        self.assertEqual(res.returncode, 0)

        # The png-less group in past_dir should be pruned to bring total bytes <= 10000
        self.assertFalse(os.path.exists(chart_p), "PNG-less group chart must be pruned")
        self.assertFalse(os.path.exists(json_p), "PNG-less group metadata must be pruned")
        self.assertTrue(os.path.exists(os.path.join(today_dir, f"{today_pfx}.png")), "Today group must be kept")

    def test_35_ps1_focus_emulation_order_and_params(self):
        """Test ps1 calls setFocusEmulationEnabled with enabled=$true before bringToFront and setDeviceMetricsOverride."""
        with open(CAPTURE_PS1, "r", encoding="utf-8") as f:
            content = f.read()

        pattern = r"Invoke-CdpCall\s+-Sess\s+\$sess\s+-Method\s+['\"]Emulation\.setFocusEmulationEnabled['\"]\s+-Params\s+@\{\s*enabled\s*=\s*\$true\s*\}"
        matches = list(re.finditer(pattern, content))
        self.assertEqual(len(matches), 1, "Invoke-CdpCall for Emulation.setFocusEmulationEnabled enabled=$true must appear exactly once")

        pos_focus = matches[0].start()
        pos_bring = content.find("Page.bringToFront")
        pos_metrics = content.find("setDeviceMetricsOverride")

        self.assertNotEqual(pos_bring, -1, "ps1 must contain Page.bringToFront")
        self.assertNotEqual(pos_metrics, -1, "ps1 must contain setDeviceMetricsOverride")
        self.assertLess(pos_focus, pos_bring, "setFocusEmulationEnabled true must appear before Page.bringToFront")
        self.assertLess(pos_focus, pos_metrics, "setFocusEmulationEnabled true must appear before setDeviceMetricsOverride")

    def test_36_ps1_finally_cleanup_active_calls(self):
        """Test ps1 finally block contains active setFocusEmulationEnabled false and clearDeviceMetricsOverride before Dispose()."""
        with open(CAPTURE_PS1, "r", encoding="utf-8") as f:
            content = f.read()

        finally_marker = "} finally {"
        idx = content.rfind(finally_marker)
        self.assertNotEqual(idx, -1, "ps1 must contain '} finally {' block for main try")
        finally_idx = content.find("finally {", idx)
        if finally_idx == -1:
            finally_idx = content.find("finally", idx)
        finally_block = content[finally_idx:]

        # Strip lines whose first non-space char is # before searching
        active_lines = [
            line for line in finally_block.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        active_text = "\n".join(active_lines)

        # Assert that WITHIN that block text (from finally { to end of file)
        # both 'Emulation.setFocusEmulationEnabled' with enabled = $false and 'Emulation.clearDeviceMetricsOverride' appear as active code
        focus_false_match = re.search(
            r"['\"]Emulation\.setFocusEmulationEnabled['\"][\s\S]*?enabled\s*=\s*\$false",
            active_text
        )
        self.assertIsNotNone(focus_false_match, "finally block must contain active Emulation.setFocusEmulationEnabled with enabled = $false")

        self.assertIn("Emulation.clearDeviceMetricsOverride", active_text,
                      "finally block must contain active Emulation.clearDeviceMetricsOverride")

        # and that $sess.Dispose() comes after them
        pos_focus = focus_false_match.start()
        pos_clear = active_text.find("Emulation.clearDeviceMetricsOverride")
        pos_dispose = active_text.find("$sess.Dispose()")

        self.assertNotEqual(pos_dispose, -1, "finally block must contain $sess.Dispose()")
        self.assertLess(pos_focus, pos_dispose, "$sess.Dispose() must come after Emulation.setFocusEmulationEnabled")
        self.assertLess(pos_clear, pos_dispose, "$sess.Dispose() must come after Emulation.clearDeviceMetricsOverride")

    def test_37_retention_unrelated_files_preserved_day_and_byte(self):
        """HA-ACCEPT-001: Test unrelated files preserved during day and byte pruning, directories removed only if empty."""
        cfg = self.make_config(retention_days=60, max_total_bytes=10000)

        # 1. Day pruning (> 60 days): coexisting genuine group + unrelated notes
        old_day = (datetime.now(timezone.utc) - timedelta(days=70)).strftime("%Y-%m-%d")
        old_dir = os.path.join(self.data_root, old_day)
        os.makedirs(old_dir, mode=0o700, exist_ok=True)
        old_pfx = f"{old_day.replace('-', '')}T100000Z_BTC_pair_24h_m1"
        with open(os.path.join(old_dir, f"{old_pfx}.png"), "wb") as f:
            f.write(b"PNG" * 100)
        with open(os.path.join(old_dir, f"{old_pfx}.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        sentinel_day = os.path.join(old_dir, "unrelated-owner-notes.txt")
        with open(sentinel_day, "w", encoding="utf-8") as f:
            f.write("DO NOT DELETE day sentinel")

        # 2. Byte pruning: 1-day-old dir with genuine group + unrelated file exceeding max_total_bytes
        mid_day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        mid_dir = os.path.join(self.data_root, mid_day)
        os.makedirs(mid_dir, mode=0o700, exist_ok=True)
        mid_pfx = f"{mid_day.replace('-', '')}T100000Z_BTC_pair_24h_m1"
        with open(os.path.join(mid_dir, f"{mid_pfx}.png"), "wb") as f:
            f.write(b"B" * 6000)
        sentinel_byte = os.path.join(mid_dir, "unrelated-owner-notes.txt")
        with open(sentinel_byte, "w", encoding="utf-8") as f:
            f.write("DO NOT DELETE byte sentinel")

        # Today dir with newest run (6000 bytes)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        os.makedirs(today_dir, mode=0o700, exist_ok=True)
        today_pfx = f"{today_str.replace('-', '')}T100000Z_BTC_pair_24h_m1"
        with open(os.path.join(today_dir, f"{today_pfx}.png"), "wb") as f:
            f.write(b"T" * 6000)

        fake_ps = self.make_fake_powershell("ok")
        res = self.run_wrapper(cfg, fake_ps, extra_args=["--prune-only"])
        self.assertEqual(res.returncode, 0)

        # Day pruning assertions:
        # Genuine expired files deleted
        self.assertFalse(os.path.exists(os.path.join(old_dir, f"{old_pfx}.png")))
        self.assertFalse(os.path.exists(os.path.join(old_dir, f"{old_pfx}.json")))
        # Unrelated file preserved
        self.assertTrue(os.path.exists(sentinel_day), "Unrelated file in expired date dir was deleted!")
        # Expired dir NOT removed because sentinel remains
        self.assertTrue(os.path.exists(old_dir), "Directory with unrelated file was removed!")

        # Byte pruning assertions:
        # Mid genuine group pruned to satisfy 10000 byte limit
        self.assertFalse(os.path.exists(os.path.join(mid_dir, f"{mid_pfx}.png")))
        # Unrelated file in mid dir preserved
        self.assertTrue(os.path.exists(sentinel_byte), "Unrelated file in byte-pruned dir was deleted!")
        # Mid dir NOT removed because sentinel remains
        self.assertTrue(os.path.exists(mid_dir))
        # Today run preserved
        self.assertTrue(os.path.exists(os.path.join(today_dir, f"{today_pfx}.png")))

        # 3. Clean directory removal when ONLY genuine files exist
        clean_day = (datetime.now(timezone.utc) - timedelta(days=75)).strftime("%Y-%m-%d")
        clean_dir = os.path.join(self.data_root, clean_day)
        os.makedirs(clean_dir, mode=0o700, exist_ok=True)
        clean_pfx = f"{clean_day.replace('-', '')}T100000Z_BTC_pair_24h_m1"
        with open(os.path.join(clean_dir, f"{clean_pfx}.png"), "wb") as f:
            f.write(b"clean")
        res2 = self.run_wrapper(cfg, fake_ps, extra_args=["--prune-only"])
        self.assertEqual(res2.returncode, 0)
        self.assertFalse(os.path.exists(clean_dir), "Clean directory with only genuine files was not removed!")

    def test_38_retention_unknown_only_and_symlinks_and_malformed_names(self):
        """HA-ACCEPT-001: Test unknown-only directories, symlinks, and malformed tool-like names are untouched."""
        cfg = self.make_config(retention_days=60, max_total_bytes=1000)

        # 1. Unknown-only directories: 70-day-old and 2-day-old
        old_day = (datetime.now(timezone.utc) - timedelta(days=70)).strftime("%Y-%m-%d")
        mid_day = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        old_dir = os.path.join(self.data_root, old_day)
        mid_dir = os.path.join(self.data_root, mid_day)
        os.makedirs(old_dir, mode=0o700, exist_ok=True)
        os.makedirs(mid_dir, mode=0o700, exist_ok=True)

        unknown_1 = os.path.join(old_dir, "unknown.txt")
        unknown_2 = os.path.join(mid_dir, "custom_script.py")
        with open(unknown_1, "w") as f:
            f.write("unknown")
        with open(unknown_2, "w") as f:
            f.write("print('hello')")

        # 2. Symlinks
        outside_target = os.path.join(self.test_dir, "secret_outside.txt")
        with open(outside_target, "w") as f:
            f.write("SECRET")
        symlink_in_old = os.path.join(old_dir, "symlink_outside.png")
        try:
            os.symlink(outside_target, symlink_in_old)
        except OSError:
            pass

        # 3. Malformed tool-like names
        malformed_files = [
            os.path.join(old_dir, "20260810T100000Z_BTC_pair_24h_m1.txt"),
            os.path.join(old_dir, "20260810T100000Z_BTC_pair_24h_m1.json.bak"),
            os.path.join(mid_dir, "20261017T100000Z_BTC_pair_24h.png"),
            os.path.join(mid_dir, "20261017T100000Z_BTC_invalidmode_24h_m1.png"),
            os.path.join(mid_dir, "20261017T100000Z_BTC_pair_24h_m1.png.tmp"),
        ]
        for mf in malformed_files:
            with open(mf, "wb") as f:
                f.write(b"M" * 2000)

        fake_ps = self.make_fake_powershell("ok")
        res = self.run_wrapper(cfg, fake_ps, extra_args=["--prune-only"])
        self.assertEqual(res.returncode, 0)

        # All unknown files, symlinks, and malformed files must be preserved
        self.assertTrue(os.path.exists(unknown_1))
        self.assertTrue(os.path.exists(unknown_2))
        self.assertTrue(os.path.exists(outside_target), "Outside target was modified/deleted!")
        if os.path.islink(symlink_in_old):
            self.assertTrue(os.path.exists(symlink_in_old))
        for mf in malformed_files:
            self.assertTrue(os.path.exists(mf), f"Malformed file {mf} was incorrectly pruned!")
        self.assertTrue(os.path.exists(old_dir))
        self.assertTrue(os.path.exists(mid_dir))

    def test_39_login_wait_ms_positive_and_negative(self):
        """HA-ACCEPT-002: Test login_wait_ms positive and negative shape persistence in metadata and runs.jsonl."""
        cfg = self.make_config()

        # 1. Positive case: login_wait_ms = 506
        fake_pos = os.path.join(self.test_dir, "fake_ps_login_wait_pos.py")
        code_pos = f"""#!/usr/bin/env python3
import json, sys, os, shutil
args = sys.argv[1:]
out_dir = None
run_id = "20261019T120000Z"
for i in range(len(args)):
    if args[i] == "-OutDir" and i + 1 < len(args): out_dir = args[i + 1]
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    shutil.copy2("{SAMPLE_PNG}", os.path.join(out_dir, f"{{run_id}}_BTC_pair_24h_m1.png"))
    shutil.copy2("{SAMPLE_CHART}", os.path.join(out_dir, f"{{run_id}}_BTC_pair_24h_m1.chart.json.gz"))
out = {{
    "schema": 1, "status": "ok",
    "chrome": {{"version": "155", "port": 19301, "owner_ok": True}},
    "page": {{"title": "Coinglass", "url": "https://www.coinglass.com/pro/futures/LiquidationHeatMap", "tabs": []}},
    "logged_in": True, "login_evidence": "user_menu_button", "login_wait_ms": 506,
    "visibility": "visible", "focus_mode": "emulation_only", "warnings": [],
    "viewport": [1400, 1200], "browser_tz_offset_min": 0,
    "results": [{{
        "coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1,
        "ok": True, "error": None, "login_required": False, "header": "Binance BTC/USDT",
        "price": 81903.0, "y_min": 77616.52, "y_max": 86359.12, "n_x": 288, "n_y": 136,
        "x_first": "19 Oct 2026, 17:00", "x_last": "19 Oct 2026, 19:10", "canvas": [1055, 730],
        "png_file": f"{{run_id}}_BTC_pair_24h_m1.png", "png_sha256": "{self.png_sha}",
        "chart_file": f"{{run_id}}_BTC_pair_24h_m1.chart.json.gz", "chart_sha256": "{self.chart_sha}",
        "chart_data_ok": True, "elapsed_ms": 1200
    }}]
}}
print(json.dumps(out))
"""
        with open(fake_pos, "w", encoding="utf-8") as f:
            f.write(code_pos)
        os.chmod(fake_pos, 0o755)

        res_pos = self.run_wrapper(cfg, fake_pos, extra_args=["--run-id", "20261019T120000Z"])
        self.assertEqual(res_pos.returncode, 0)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        meta_pos_path = os.path.join(self.data_root, today_str, "20261019T120000Z_BTC_pair_24h_m1.json")
        with open(meta_pos_path, "r", encoding="utf-8") as f:
            meta_pos = json.load(f)
        self.assertIn("login_wait_ms", meta_pos)
        self.assertEqual(meta_pos["login_wait_ms"], 506)
        self.assertIsInstance(meta_pos["login_wait_ms"], int)

        runs_path = os.path.join(self.data_root, "runs.jsonl")
        with open(runs_path, "r", encoding="utf-8") as f:
            rec_pos = [json.loads(line) for line in f if line.strip()][-1]
        self.assertIn("login_wait_ms", rec_pos)
        self.assertEqual(rec_pos["login_wait_ms"], 506)
        self.assertIsInstance(rec_pos["login_wait_ms"], int)

        # 2. Negative case: login_wait_ms omitted
        fake_neg = os.path.join(self.test_dir, "fake_ps_login_wait_neg.py")
        code_neg = f"""#!/usr/bin/env python3
import json, sys, os, shutil
args = sys.argv[1:]
out_dir = None
run_id = "20261019T120100Z"
for i in range(len(args)):
    if args[i] == "-OutDir" and i + 1 < len(args): out_dir = args[i + 1]
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    shutil.copy2("{SAMPLE_PNG}", os.path.join(out_dir, f"{{run_id}}_BTC_pair_24h_m1.png"))
    shutil.copy2("{SAMPLE_CHART}", os.path.join(out_dir, f"{{run_id}}_BTC_pair_24h_m1.chart.json.gz"))
out = {{
    "schema": 1, "status": "ok",
    "chrome": {{"version": "155", "port": 19301, "owner_ok": True}},
    "page": {{"title": "Coinglass", "url": "https://www.coinglass.com/pro/futures/LiquidationHeatMap", "tabs": []}},
    "logged_in": True, "login_evidence": "none",
    "visibility": "visible", "focus_mode": "emulation_only", "warnings": [],
    "viewport": [1400, 1200], "browser_tz_offset_min": 0,
    "results": [{{
        "coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1,
        "ok": True, "error": None, "login_required": False, "header": "Binance BTC/USDT",
        "price": 81903.0, "y_min": 77616.52, "y_max": 86359.12, "n_x": 288, "n_y": 136,
        "x_first": "19 Oct 2026, 17:00", "x_last": "19 Oct 2026, 19:10", "canvas": [1055, 730],
        "png_file": f"{{run_id}}_BTC_pair_24h_m1.png", "png_sha256": "{self.png_sha}",
        "chart_file": f"{{run_id}}_BTC_pair_24h_m1.chart.json.gz", "chart_sha256": "{self.chart_sha}",
        "chart_data_ok": True, "elapsed_ms": 1200
    }}]
}}
print(json.dumps(out))
"""
        with open(fake_neg, "w", encoding="utf-8") as f:
            f.write(code_neg)
        os.chmod(fake_neg, 0o755)

        res_neg = self.run_wrapper(cfg, fake_neg, extra_args=["--run-id", "20261019T120100Z"])
        self.assertEqual(res_neg.returncode, 0)

        meta_neg_path = os.path.join(self.data_root, today_str, "20261019T120100Z_BTC_pair_24h_m1.json")
        with open(meta_neg_path, "r", encoding="utf-8") as f:
            meta_neg = json.load(f)
        self.assertIn("login_wait_ms", meta_neg)
        self.assertIsNone(meta_neg["login_wait_ms"])

        with open(runs_path, "r", encoding="utf-8") as f:
            rec_neg = [json.loads(line) for line in f if line.strip()][-1]
        self.assertIn("login_wait_ms", rec_neg)
        self.assertIsNone(rec_neg["login_wait_ms"])

    def test_40_per_combo_timeout_and_next_combo_accounting(self):
        """HA-ACCEPT-003: Test per-combo timeout records combo_timeout and continues to next combo."""
        combos = [
            {"coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True},
            {"coin": "ETH", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True},
            {"coin": "SOL", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True},
        ]
        cfg = self.make_config(combos=combos)

        fake_timeout_script = os.path.join(self.test_dir, "fake_ps_combo_timeout.py")
        code = f"""#!/usr/bin/env python3
import json, sys, os, shutil
args = sys.argv[1:]
out_dir = None
run_id = "20261019T140000Z"
for i in range(len(args)):
    if args[i] == "-OutDir" and i + 1 < len(args): out_dir = args[i + 1]
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    # Staging only for BTC and SOL (ETH timed out, no staging)
    shutil.copy2("{SAMPLE_PNG}", os.path.join(out_dir, f"{{run_id}}_BTC_pair_24h_m1.png"))
    shutil.copy2("{SAMPLE_CHART}", os.path.join(out_dir, f"{{run_id}}_BTC_pair_24h_m1.chart.json.gz"))
    shutil.copy2("{SAMPLE_PNG}", os.path.join(out_dir, f"{{run_id}}_SOL_pair_24h_m1.png"))
    shutil.copy2("{SAMPLE_CHART}", os.path.join(out_dir, f"{{run_id}}_SOL_pair_24h_m1.chart.json.gz"))
out = {{
    "schema": 1, "status": "partial",
    "chrome": {{"version": "155", "port": 19301, "owner_ok": True}},
    "page": {{"title": "Coinglass", "url": "https://www.coinglass.com/pro/futures/LiquidationHeatMap", "tabs": []}},
    "logged_in": True, "login_evidence": "user_menu_button", "login_wait_ms": 500,
    "visibility": "visible", "focus_mode": "emulation_only", "warnings": [],
    "viewport": [1400, 1200], "browser_tz_offset_min": 0,
    "results": [
        {{
            "coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1,
            "ok": True, "error": None, "login_required": False, "header": "Binance BTC/USDT",
            "price": 81903.0, "y_min": 77616.52, "y_max": 86359.12, "n_x": 288, "n_y": 136,
            "x_first": "19 Oct 2026, 17:00", "x_last": "19 Oct 2026, 19:10", "canvas": [1055, 730],
            "png_file": f"{{run_id}}_BTC_pair_24h_m1.png", "png_sha256": "{self.png_sha}",
            "chart_file": f"{{run_id}}_BTC_pair_24h_m1.chart.json.gz", "chart_sha256": "{self.chart_sha}",
            "chart_data_ok": True, "elapsed_ms": 1200
        }},
        {{
            "coin": "ETH", "mode": "pair", "timeframe": "24 hour", "model": 1,
            "ok": False, "error": "combo_timeout", "login_required": False, "header": "",
            "price": None, "y_min": None, "y_max": None, "n_x": 0, "n_y": 0,
            "x_first": "", "x_last": "", "canvas": [0, 0],
            "png_file": None, "png_sha256": None, "chart_file": None, "chart_sha256": None,
            "chart_data_ok": False, "elapsed_ms": 60000
        }},
        {{
            "coin": "SOL", "mode": "pair", "timeframe": "24 hour", "model": 1,
            "ok": True, "error": None, "login_required": False, "header": "Binance SOL/USDT",
            "price": 105.0, "y_min": 90.0, "y_max": 120.0, "n_x": 288, "n_y": 136,
            "x_first": "19 Oct 2026, 17:00", "x_last": "19 Oct 2026, 19:10", "canvas": [1055, 730],
            "png_file": f"{{run_id}}_SOL_pair_24h_m1.png", "png_sha256": "{self.png_sha}",
            "chart_file": f"{{run_id}}_SOL_pair_24h_m1.chart.json.gz", "chart_sha256": "{self.chart_sha}",
            "chart_data_ok": True, "elapsed_ms": 1500
        }}
    ]
}}
print(json.dumps(out))
"""
        with open(fake_timeout_script, "w", encoding="utf-8") as f:
            f.write(code)
        os.chmod(fake_timeout_script, 0o755)

        res = self.run_wrapper(cfg, fake_timeout_script, extra_args=["--run-id", "20261019T140000Z"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("status=partial", res.stdout)
        self.assertIn("combos=2/3", res.stdout)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)

        # BTC and SOL archived
        self.assertTrue(os.path.exists(os.path.join(today_dir, "20261019T140000Z_BTC_pair_24h_m1.png")))
        self.assertTrue(os.path.exists(os.path.join(today_dir, "20261019T140000Z_SOL_pair_24h_m1.png")))
        # ETH was NOT archived
        self.assertFalse(os.path.exists(os.path.join(today_dir, "20261019T140000Z_ETH_pair_24h_m1.png")))

        # Check runs.jsonl
        runs_path = os.path.join(self.data_root, "runs.jsonl")
        with open(runs_path, "r", encoding="utf-8") as f:
            rec = [json.loads(line) for line in f if line.strip()][-1]
        self.assertEqual(rec["status"], "partial")
        self.assertEqual(rec["combos_total"], 3)
        self.assertEqual(rec["combos_ok"], 2)
        eth_summary = [s for s in rec["results"] if s["coin"] == "ETH"][0]
        self.assertFalse(eth_summary["ok"])
        self.assertEqual(eth_summary["error"], "combo_timeout")

    def test_41_per_combo_elapsed_exceeding_60s_rejected(self):
        """HA-ACCEPT-003: Test combo reporting elapsed > 60s is rejected by contract."""
        cfg = self.make_config()
        fake_overtime = os.path.join(self.test_dir, "fake_ps_overtime.py")
        code = f"""#!/usr/bin/env python3
import json, sys, os, shutil
args = sys.argv[1:]
out_dir = None
run_id = "20261019T150000Z"
for i in range(len(args)):
    if args[i] == "-OutDir" and i + 1 < len(args): out_dir = args[i + 1]
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    shutil.copy2("{SAMPLE_PNG}", os.path.join(out_dir, f"{{run_id}}_BTC_pair_24h_m1.png"))
    shutil.copy2("{SAMPLE_CHART}", os.path.join(out_dir, f"{{run_id}}_BTC_pair_24h_m1.chart.json.gz"))
out = {{
    "schema": 1, "status": "ok",
    "chrome": {{"version": "155", "port": 19301, "owner_ok": True}},
    "page": {{"title": "Coinglass", "url": "https://www.coinglass.com/pro/futures/LiquidationHeatMap", "tabs": []}},
    "logged_in": True, "login_evidence": "user_menu_button", "login_wait_ms": 500,
    "visibility": "visible", "focus_mode": "emulation_only", "warnings": [],
    "viewport": [1400, 1200], "browser_tz_offset_min": 0,
    "results": [{{
        "coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1,
        "ok": True, "error": None, "login_required": False, "header": "Binance BTC/USDT",
        "price": 81903.0, "y_min": 77616.52, "y_max": 86359.12, "n_x": 288, "n_y": 136,
        "x_first": "19 Oct 2026, 17:00", "x_last": "19 Oct 2026, 19:10", "canvas": [1055, 730],
        "png_file": f"{{run_id}}_BTC_pair_24h_m1.png", "png_sha256": "{self.png_sha}",
        "chart_file": f"{{run_id}}_BTC_pair_24h_m1.chart.json.gz", "chart_sha256": "{self.chart_sha}",
        "chart_data_ok": True, "elapsed_ms": 61200
    }}]
}}
print(json.dumps(out))
"""
        with open(fake_overtime, "w", encoding="utf-8") as f:
            f.write(code)
        os.chmod(fake_overtime, 0o755)

        res = self.run_wrapper(cfg, fake_overtime, extra_args=["--run-id", "20261019T150000Z"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("combos=0/1", res.stdout)

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir = os.path.join(self.data_root, today_str)
        self.assertFalse(os.path.exists(os.path.join(today_dir, "20261019T150000Z_BTC_pair_24h_m1.png")),
                         "Overtime combo must NOT be archived")

        runs_path = os.path.join(self.data_root, "runs.jsonl")
        with open(runs_path, "r", encoding="utf-8") as f:
            rec = [json.loads(line) for line in f if line.strip()][-1]
        self.assertEqual(rec["combos_ok"], 0)
        self.assertEqual(rec["results"][0]["error"], "combo_timeout")

    def test_43_ps_deadline_probe(self):
        """Execute shipped PowerShell's real monotonic deadline branches offline."""
        exe = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not exe:
            self.skipTest("PowerShell not installed: Windows operator must execute")
        ps_path = CAPTURE_PS1
        if os.name != "nt" and os.path.isfile("/proc/sys/fs/binfmt_misc/WSLInterop"):
            p = subprocess.run(["wslpath", "-w", CAPTURE_PS1], capture_output=True, text=True, check=True)
            ps_path = p.stdout.strip()
        elif os.name != "nt" and "microsoft" in os.uname().release.lower():
            p = subprocess.run(["wslpath", "-w", CAPTURE_PS1], capture_output=True, text=True, check=True)
            ps_path = p.stdout.strip()
        for case in ("connect", "login", "reload", "output", "hash", "cleanup", "next"):
            with self.subTest(case=case):
                started = time.monotonic()
                proc = subprocess.run(
                    [exe, "-NoProfile", "-File", ps_path,
                     "-DeadlineProbe", "-ProbeCase", case, "-ProbeMs", "4000"],
                    capture_output=True, timeout=20,
                )
                wall_ms = (time.monotonic() - started) * 1000
                stdout = proc.stdout.decode("utf-8-sig", errors="replace")
                stderr = proc.stderr.decode("utf-8-sig", errors="replace")
                self.assertEqual(proc.returncode, 0, stderr)
                try:
                    result = json.loads(stdout.strip().splitlines()[-1])
                except (ValueError, IndexError) as exc:
                    self.fail(f"PowerShell returned invalid JSON: {exc}; stdout={stdout!r}; stderr={stderr!r}")
                self.assertEqual(result["status"], "deadline_probe")
                self.assertLess(wall_ms, 20000)
                rows = result["results"]
                self.assertEqual(len(rows), 3 if case == "next" else 1)
                for row in rows:
                    self.assertEqual(row["error"], None if row["coin"] == "ok" else "combo_timeout")
                    self.assertEqual(row["ok"], row["coin"] == "ok")
                    self.assertGreaterEqual(row["elapsed_ms"], 0)
                    self.assertLess(row["elapsed_ms"], 6000)
                if case == "next":
                    self.assertEqual([r["coin"] for r in rows], ["connect", "ok", "cleanup"])

    def test_45_ipc_protocol(self):
        """Real PowerShell 5.1 worker IPC, without Chrome or authorization bypass."""
        exe = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        if not exe:
            self.skipTest("PowerShell unavailable")
        ps_path = CAPTURE_PS1
        if os.name != "nt" and "microsoft" in os.uname().release.lower():
            ps_path = subprocess.run(["wslpath", "-w", CAPTURE_PS1],
                                     capture_output=True, text=True, check=True).stdout.strip()
        cmd = [exe, "-NoProfile", "-File", ps_path]
        good = subprocess.run(cmd + ["-ProbeWorker", "-ProbeCase", "ipcgood"],
                              capture_output=True, timeout=15)
        self.assertEqual(good.returncode, 0, good.stderr.decode("utf-8", errors="replace"))
        raw = good.stdout.decode("utf-8-sig")
        self.assertEqual(len(raw.strip().splitlines()), 1, repr(raw))
        obj = json.loads(raw)
        self.assertIsNone(obj["results"][0]["error"])
        self.assertEqual(obj["status"], "ok")

        error = subprocess.run(cmd + ["-ProbeWorker", "-ProbeCase", "ipcerror"],
                               capture_output=True, timeout=15)
        self.assertEqual(error.returncode, 0, error.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(len(error.stdout.decode("utf-8-sig").strip().splitlines()), 1)
        self.assertIn(b"ipc_expected_error", error.stderr)
        self.assertEqual(json.loads(error.stdout)["results"][0]["error"], "chart_missing")

        parent = subprocess.run(cmd + ["-IpcProbe", "-IpcCase", "next"],
                                capture_output=True, timeout=45)
        self.assertEqual(parent.returncode, 0, parent.stderr.decode("utf-8", errors="replace"))
        rows = json.loads(parent.stdout.decode("utf-8-sig"))["results"]
        self.assertEqual([r["case"] for r in rows],
                         ["noise", "good", "error", "null", "bad", "timeout", "good"])
        self.assertEqual([r["accepted"] for r in rows],
                         [False, True, True, True, False, False, True])
        self.assertEqual(rows[2]["error"], "chart_missing")
        self.assertTrue(rows[2]["stderr_ok"])
        self.assertIsNone(rows[3]["error"])
        self.assertTrue(rows[5]["timeout"])
        self.assertTrue(rows[6]["accepted"])

    def test_44_deadline_static_guards(self):
        """Require no old widened call budget or post-failure reconnection."""
        with open(CAPTURE_PS1, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("Get-ComboRemaining", src)
        self.assertIn("DeadlineProbe", src)
        self.assertNotIn("Max(1000, [Math]::Min", src)
        self.assertNotIn("comboDeadline", src)
        self.assertNotIn("EnsureOpen()", src)
        self.assertIn("cts.CancelAfter(timeoutMs)", src)
        self.assertIn("'next','ok'", src)
        self.assertNotIn("-ExecutionPolicy", src)

    def test_42_ps1_static_per_combo_deadline_enforcement(self):
        """HA-ACCEPT-003: Static check on ps1 for per-combo 60s deadline contract and reconnection."""
        with open(CAPTURE_PS1, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("comboTimeoutMs = 60000", content, "ps1 must define 60000ms combo timeout")
        self.assertIn("combo_timeout", content, "ps1 must handle combo_timeout error")
        self.assertIn("EnsureOpen", content, "ps1 must have EnsureOpen reconnection method")
        self.assertIn("CancellationTokenSource.CreateLinkedTokenSource", content, "ps1 must link cancellation token for per-call timeout")


    def _login_node(self, scenarios):
        node = shutil.which("node")
        self.assertIsNotNone(node, "node required")
        with open(CAPTURE_PS1, encoding="utf-8") as f:
            src = f.read()
        m = re.search(r"\$loginPollJs\s*=\s*@'\r?\n(.*?)\r?\n'@", src, re.DOTALL)
        self.assertIsNotNone(m)
        js = """const loginPollJs = (""" + m.group(1) + """);
let tick = 0, sample = 0, frame = [];
Date.now = () => tick;
global.window = {innerWidth: 1400};
function el(tag, text, children = []) {
  return {tagName:tag, innerText:text, textContent:text, children:children,
    querySelector: s => children.find(c => c.tagName === s.toUpperCase()) || null,
    querySelectorAll: s => children.filter(c => c.tagName === s.toUpperCase()),
    getBoundingClientRect: () => ({right:1380,top:47,bottom:90})};
}
global.document = {
  querySelectorAll: s => frame,
  querySelector: s => s === 'header' ? frame.find(e => e.tagName === 'HEADER') : null
};
global.setTimeout = fn => { tick += 500; sample++; fn(); };
(async () => {
  const output = [];
  for (const seq of SCENARIOS) {
    tick = 0; sample = 0;
    Object.defineProperty(global.document, 'querySelectorAll', {
      configurable:true, value:() => {
        frame = seq[Math.min(sample,seq.length-1)].map(n =>
          n === 'login' ? el('BUTTON','Login') :
          n === 'menu' ? el('HEADER','',[el('BUTTON','',[el('SVG','')])]) :
          el('DIV','Log out'));
        const all = [];
        for (const e of frame) {all.push(e,...e.children,...e.children.flatMap(c=>c.children));}
        return all;
      }});
    output.push(JSON.parse(await loginPollJs(10000,500)));
  }
  process.stdout.write(JSON.stringify(output));
})().catch(e => {console.error(e);process.exitCode=1});
""".replace("SCENARIOS", json.dumps(scenarios))
        proc = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_46_login_transient(self):
        a, b = self._login_node([
            [["login"], ["menu"], ["menu"]],
            [["login"], [], [], [], []],
        ])
        self.assertEqual((a["state"], a["evidence"]), ("true", "user_menu_button"))
        self.assertEqual((b["state"], b["evidence"]), ("unknown", "timeout"))

    def test_47_login_stable(self):
        result = self._login_node([[["login"]]])[0]
        self.assertEqual((result["state"], result["evidence"]), ("false", "login_leaf"))
        self.assertGreaterEqual(result["waited_ms"], 2000)

    def test_48_run_deadline(self):
        with open(CAPTURE_PS1, encoding="utf-8") as f:
            src = f.read()
        parent = src.split("if (-not $ComboWorker -and -not $ProbeWorker -and -not $DryRun) {", 1)[1].split("if (-not $ProbeWorker)", 1)[0]
        for part in ("Stopwatch]::StartNew()", "$runLimitMs = 230000",
                     "$remaining -lt 20000", "[Math]::Min(60000, $remaining - 5000)",
                     "-BudgetMs $workerBudgetMs", "error='run_deadline'",
                     "PHASE start", "PHASE combo $ix start", "PHASE combo $ix end",
                     "Send-JsonAndExit"):
            self.assertIn(part, parent)
        cfg = self.make_config()
        fake = os.path.join(self.test_dir, "partial.py")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("import json\nprint(json.dumps({'status':'partial','results':[{'coin':'BTC','mode':'pair','timeframe':'24 hour','model':1,'ok':False,'error':'run_deadline','login_required':False,'elapsed_ms':0}]}))\n")
        res = self.run_wrapper(cfg, fake)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("status=partial", res.stdout)
        with open(os.path.join(self.data_root, "runs.jsonl"), encoding="utf-8") as f:
            row = json.loads(f.readlines()[-1])
        self.assertEqual(row["results"][0]["error"], "run_deadline")

    def test_49_timeout_diag(self):
        cfg = self.make_config()
        fake = os.path.join(self.test_dir, "stalled.py")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("import sys,time\nprint('PHASE start',file=sys.stderr,flush=True)\nprint('PHASE combo 0 start t=7',file=sys.stderr,flush=True)\ntime.sleep(20)\n")
        with open(ARCHIVE_PY, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("timeout=280", src)
        short = os.path.join(self.test_dir, "short_archive.py")
        with open(short, "w", encoding="utf-8") as f:
            f.write(src.replace("timeout=280", "timeout=0.3"))
        cmd = [sys.executable, short, "--config", cfg, "--data-root",
               self.data_root, "--ps-script", CAPTURE_PS1,
               "--powershell", sys.executable + " " + fake]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("status=powershell_timeout", res.stdout)
        with open(os.path.join(self.data_root, "runs.jsonl"), encoding="utf-8") as f:
            row = json.loads(f.readlines()[-1])
        self.assertEqual(row["status"], "powershell_timeout")
        self.assertIn("PHASE combo 0 start t=7", row["stderr_tail"])
        self.assertLessEqual(len(row["stderr_tail"]), 300)

    def test_50_login_and_menu(self):
        steady, transient = self._login_node([
            [["login", "menu"]],
            [["login", "menu"], ["menu"], ["menu"]],
        ])
        self.assertEqual((steady["state"], steady["evidence"]),
                         ("false", "login_leaf"))
        self.assertGreaterEqual(steady["waited_ms"], 2000)
        self.assertEqual((transient["state"], transient["evidence"]),
                         ("true", "user_menu_button"))


    @unittest.skipUnless(
        shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh"),
        "PowerShell unavailable")
    def test_51_phase_line_run(self):
        """Execute the actual PHASE end expression in PowerShell 5.1."""
        exe = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        with open(CAPTURE_PS1, encoding="utf-8") as f:
            lines = f.readlines()
        phase = [line.strip() for line in lines
                 if 'PHASE combo $ix end ok=$(' in line]
        self.assertEqual(len(phase), 1, "Expected one production PHASE end line")
        for expected, value in (("true", "$true"), ("false", "$false")):
            with self.subTest(expected=expected):
                script = ("$ErrorActionPreference='Stop'; "
                          "$ix=0; $runClock=[Diagnostics.Stopwatch]::StartNew(); "
                          "$results=@(@{ok=" + value + "}); " + phase[0])
                proc = subprocess.run([exe, "-NoProfile", "-Command", script],
                                      capture_output=True, text=True, timeout=20)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("PHASE combo 0 end ok=" + expected, proc.stderr)


    def _canvas_ps(self, case):
        exe = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        self.assertIsNotNone(exe)
        with open(CAPTURE_PS1, encoding="utf-8") as fh:
            source = fh.read()
        bodies = []
        for name in ("Get-ComboRemaining", "Wait-Combo",
                     "Wait-CanvasReady", "Get-ChartCapture"):
            pattern = r"(?ms)^function " + name + r" \\{.*?^\\}"
            match = re.search(pattern.replace("\\\\", "\\"), source)
            self.assertIsNotNone(match, name)
            bodies.append(match.group())
        scenarios = {
            "a": (['{"err":"no canvas"}', '{"ok":true}'], [False, True, True]),
            "b": (['{"err":"no canvas"}', '{"ok":true}'], [True, False, True, True]),
            "c": (['{"err":"no canvas"}', '{"err":"no canvas"}'], [True, True]),
            "d": (['{"err":"no canvas"}'], [False] * 30),
            "e": (['{"err":"login_required"}'], []),
            "e2": (['{"err":"worker_error"}'], []),
            "f": (['{"err":"no canvas"}'], []),
        }
        captures, samples = scenarios[case]
        ps = "\n".join(bodies) + """
$ErrorActionPreference = 'Stop'
$script:caps = @(@CAPS@)
$script:bools = @(@BOOLS@)
$script:capN = 0
$script:sampleN = 0
function Invoke-CdpEval {
    param($Sess, [string]$Expression, [int]$TimeoutMs, $Clock)
    if ($Expression.Contains('toDataURL')) {
        $result = $script:caps[$script:capN]
        $script:capN++
        return $result
    }
    if ($Expression.Contains('width>0')) {
        $result = $script:bools[[Math]::Min($script:sampleN, $script:bools.Count - 1)]
        $script:sampleN++
        return $result
    }
    throw 'unexpected_expression'
}
$clock = [Diagnostics.Stopwatch]::StartNew()
try {
    if ('@CASE@' -eq 'd') {
        $ready = Wait-CanvasReady -Sess $null -Clock $clock -MaxMs 1500 -StepMs 200
        $result = @{ready=$ready}
    } elseif ('@CASE@' -eq 'f') {
        [void]$clock.ElapsedMilliseconds
        Start-Sleep -Milliseconds 15
        [void](Get-ComboRemaining -Clock $clock -LimitMs 1)
        $result = @{err='not_timed_out'}
    } else {
        $result = Get-ChartCapture -Sess $null -Clock $clock -Js 'toDataURL'
    }
} catch {
    $result = @{err=$_.Exception.Message}
}
@{result=$result; captures=$script:capN; samples=$script:sampleN; elapsed=$clock.ElapsedMilliseconds} | ConvertTo-Json -Depth 5 -Compress
""".replace("@CAPS@", ",".join("'" + v + "'" for v in captures)).replace(
            "@BOOLS@", ",".join("$true" if v else "$false" for v in samples)
        ).replace("@CASE@", case)
        proc = subprocess.run([exe, "-NoProfile", "-Command", ps],
                              capture_output=True, text=True, timeout=25)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    @unittest.skipUnless(
        shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh"),
        "PowerShell unavailable")
    def test_52_canvas_wait_run(self):
        a = self._canvas_ps("a")
        self.assertTrue(a["result"]["ok"])
        self.assertEqual((a["captures"], a["samples"]), (2, 3))
        b = self._canvas_ps("b")
        self.assertTrue(b["result"]["ok"])
        self.assertEqual((b["captures"], b["samples"]), (2, 4))
        d = self._canvas_ps("d")
        self.assertFalse(d["result"]["ready"])
        self.assertEqual(d["captures"], 0)
        self.assertGreaterEqual(d["samples"], 2)
        self.assertLess(d["elapsed"], 3500)

    @unittest.skipUnless(
        shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh"),
        "PowerShell unavailable")
    def test_53_no_canvas_retry(self):
        c = self._canvas_ps("c")
        self.assertEqual(c["result"]["err"], "no canvas")
        self.assertEqual((c["captures"], c["samples"]), (2, 2))
        for case, error in (("e", "login_required"), ("e2", "worker_error")):
            row = self._canvas_ps(case)
            self.assertEqual(row["result"]["err"], error)
            self.assertEqual((row["captures"], row["samples"]), (1, 0))
        f = self._canvas_ps("f")
        self.assertIn("combo_timeout", f["result"]["err"])


    def _native_retry(self, mode):
        exe = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
        self.assertIsNotNone(exe)
        with open(CAPTURE_PS1, encoding="utf-8") as fh:
            src = fh.read()
        names = ("Get-ComboRemaining", "Wait-Combo", "Wait-CanvasReady", "Get-ChartCapture")
        parts = []
        for name in names:
            match = re.search(r"(?ms)^function " + name + r" \{.*?^\}", src)
            self.assertIsNotNone(match, name)
            parts.append(match.group())
        outer = src.split("        # A single retry is shared by switch and canvas capture.\n", 1)
        self.assertEqual(len(outer), 2)
        loop = outer[1].split("        # A failed or overdue combo publishes", 1)[0]
        ps = "\n".join(parts) + """
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$script:probeScenario = '@MODE@'
$script:capN = 0; $script:sampleN = 0; $script:switchN = 0; $script:reloadN = 0
$script:events = @(); $script:limits = @(); $script:clockBad = 0
function Invoke-CdpEval {
    param($Sess,[string]$Expression,[int]$TimeoutMs,$Clock,[switch]$AwaitPromise)
    if (-not [object]::ReferenceEquals($Clock,$script:outerClock)) { $script:clockBad++ }
    if ($Expression.Contains('width>0')) {
        $script:sampleN++
        $script:events += 'sample'
        $script:limits += $TimeoutMs
        if ($script:probeScenario -in @('slow','slow_throw','fast')) {
            Start-Sleep -Milliseconds ([Math]::Min(9000, $TimeoutMs))
            if ($script:probeScenario -eq 'slow_throw') { throw 'combo_timeout' }
            return $true
        }
        if ($script:probeScenario -eq 'wait_timeout') { throw 'combo_timeout' }
        if ($script:probeScenario -eq 'transport') { throw 'cdp_transport' }
        if ($script:probeScenario -eq 'reset') { return @($true,$false,$true,$true)[$script:sampleN-1] }
        if ($script:probeScenario -in @('no_ready','always_false')) { return $false }
        return $true
    }
    if ($Expression.Contains('toDataURL')) {
        $script:capN++
        $script:events += 'capture'
        if ($script:probeScenario -in @('switch','no_ready','persistent')) { return '{"err":"no canvas"}' }
        if ($script:probeScenario -eq 'login') { return '{"err":"login_required"}' }
        if ($script:probeScenario -eq 'worker') { return '{"err":"worker_error"}' }
        if ($script:probeScenario -eq 'switch_throw') { return '{"chart_data_ok":false,"w":1,"h":1}' }
        if ($script:capN -eq 1) { return '{"err":"no canvas"}' }
        if ($script:probeScenario -eq 'recap_err' -and $script:capN -eq 2) { throw 'cdp_transient' }
        if ($script:probeScenario -eq 'recap_err' -and $script:capN -eq 3) { return '{"err":"no canvas"}' }
        if ($script:probeScenario -in @('recap_timeout','exhaust')) { throw 'combo_timeout' }
        if ($script:probeScenario -eq 'after_ok') { return '{"chart_data_ok":false,"w":"not_integer","h":1}' }
        return '{"chart_data_ok":false,"w":1,"h":1}'
    }
    $script:switchN++
    $script:events += 'switch'
    if ($script:probeScenario -eq 'switch' -and $script:switchN -eq 1) { return '{"err":"switch_transient"}' }
    if ($script:probeScenario -eq 'switch_throw' -and $script:switchN -eq 1) { throw 'cdp_transient' }
    return '{"head":"BTC"}'
}
function Invoke-CdpCall { param($Sess,[string]$Method,$Params,[int]$TimeoutMs,$Clock)
    if ($Method -eq 'Page.reload') { $script:reloadN++; $script:events += 'reload' }
}
$sw = [Diagnostics.Stopwatch]::StartNew()
$script:outerClock = $sw
$sess=$null; $coin='BTC'; $model=1; $timeframe='24 hour'; $mode='pair'
$driverJsCode='driver'; $captureChartJsCode='toDataURL'; $comboTimeoutMs=60000
$OutDir=$null; $SavePng=0; $comboOk=$false; $comboError=$null
$loginRequired=$false; $loggedIn=$true; $chartDataOk=$false
if ($script:probeScenario -in @('slow','slow_throw','fast','reset','always_false','wait_timeout','transport')) {
    $capClock = [Diagnostics.Stopwatch]::StartNew()
    try {
        if ($script:probeScenario -eq 'fast') { $ready = Wait-CanvasReady -Sess $null -Clock $sw -MaxMs 250 -StepMs 50 }
        elseif ($script:probeScenario -eq 'wait_timeout' -or $script:probeScenario -eq 'transport') {
            $ready = Wait-CanvasReady -Sess $null -Clock $sw -MaxMs 1500 -StepMs 200
        } else { $ready = Wait-CanvasReady -Sess $null -Clock $sw }
        $result = @{ready=$ready; elapsed=$capClock.ElapsedMilliseconds}
    } catch {
        $result = @{ready=$false; elapsed=$capClock.ElapsedMilliseconds; error=$_.Exception.Message}
    }
} else {
    # Execute the extracted production outer retry loop with its real helper.
@LOOP@
    $result = @{ok=$comboOk; error=$comboError; png=$pngFile; chart=$chartFile}
}
$nativeJson = @{scenario=$script:probeScenario; result=$result; captures=$script:capN; samples=$script:sampleN;
  reloads=$script:reloadN; switches=$script:switchN; limits=@($script:limits);
  events=@($script:events); clock_bad=$script:clockBad; elapsed=$sw.ElapsedMilliseconds
} | ConvertTo-Json -Depth 5 -Compress
[Console]::Out.WriteLine($nativeJson)
""".replace("@MODE@", mode).replace("@LOOP@", loop)
        proc = subprocess.run([exe, "-NoProfile", "-Command", ps],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="strict", timeout=22)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = proc.stdout.strip().splitlines()
        self.assertEqual(len(rows), 1, (proc.stdout, proc.stderr))
        row = json.loads(rows[0])
        required = ("scenario", "result", "captures", "samples", "reloads",
                    "switches", "limits", "events", "clock_bad", "elapsed")
        for key in required:
            self.assertIn(key, row, "harness ERROR: missing " + key)
        self.assertEqual(row["scenario"], mode, "harness ERROR: wrong scenario")
        self.assertEqual(row["clock_bad"], 0, "harness ERROR: stopwatch changed")
        for key in ("captures", "samples", "reloads", "switches"):
            self.assertIsInstance(row[key], int)
        if mode in ("slow", "slow_throw", "fast", "reset", "always_false",
                    "wait_timeout", "transport"):
            for key in ("ready", "elapsed"):
                self.assertIn(key, row["result"], "harness ERROR: " + key)
        else:
            for key in ("ok", "error", "png", "chart"):
                self.assertIn(key, row["result"], "harness ERROR: " + key)
            self.assertIsNone(row["result"]["png"])
            self.assertIsNone(row["result"]["chart"])
        return row

    @unittest.skipUnless(
        shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh"),
        "PowerShell unavailable")
    def test_54_canvas_cap(self):
        slow = self._native_retry("slow")
        self.assertFalse(slow["result"]["ready"])
        self.assertEqual(slow["samples"], 1)
        self.assertLess(slow["result"]["elapsed"], 8700)
        fast = self._native_retry("fast")
        self.assertFalse(fast["result"]["ready"])
        self.assertLess(fast["result"]["elapsed"], 1000)
        reset = self._native_retry("reset")
        self.assertTrue(reset["result"]["ready"])
        self.assertEqual(reset["samples"], 4)
        self.assertEqual(reset["clock_bad"], 0)
        self.assertEqual(reset["events"], ["sample"] * 4)
        for case in ("slow", "slow_throw"):
            row = slow if case == "slow" else self._native_retry(case)
            self.assertFalse(row["result"]["ready"])
            self.assertEqual(row["samples"], 1)
            self.assertLess(row["result"]["elapsed"], 8700)
            self.assertLessEqual(row["limits"][0], 8000)
            self.assertGreater(row["limits"][0], 7500)
            self.assertEqual(row["clock_bad"], 0)
        never = self._native_retry("always_false")
        self.assertFalse(never["result"]["ready"])
        self.assertGreater(never["samples"], 2)
        self.assertLess(never["result"]["elapsed"], 8700)
        self.assertLessEqual(never["limits"][0], 8000)
        self.assertGreater(never["limits"][0], never["limits"][-1])
        self.assertEqual(never["events"], ["sample"] * never["samples"])
        for case, error in (("wait_timeout", "combo_timeout"),
                            ("transport", "cdp_transport")):
            row = self._native_retry(case)
            self.assertEqual(row["result"]["error"], error)
            self.assertEqual(row["samples"], 1)
            self.assertEqual(row["captures"], 0)

    @unittest.skipUnless(
        shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh"),
        "PowerShell unavailable")
    def test_55_outer_retry(self):
        for mode, error, caps, reloads in (
            ("recap_err", "cdp_transient", 2, 0),
            ("recap_timeout", "combo_timeout", 2, 0),
            ("switch", "no canvas", 1, 1),
            ("after_ok", "not_integer", 2, 0),
        ):
            with self.subTest(mode=mode):
                row = self._native_retry(mode)
                self.assertFalse(row["result"]["ok"])
                self.assertIn(error, row["result"]["error"])
                self.assertEqual(row["captures"], caps)
                self.assertEqual(row["reloads"], reloads)
        no_ready = self._native_retry("no_ready")
        self.assertEqual(no_ready["captures"], 1)
        self.assertEqual(no_ready["result"]["error"], "no canvas")
        self.assertEqual(no_ready["reloads"], 0)
        self.assertGreater(no_ready["samples"], 2)
        self.assertFalse(no_ready["result"]["ok"])
        for mode, good, err, caps, reloads, samples in (
            ("success", True, None, 2, 0, 2),
            ("persistent", False, "no canvas", 2, 0, 2),
            ("switch_throw", True, None, 1, 1, 0),
            ("login", False, "login_required", 1, 0, 0),
            ("worker", False, "worker_error", 1, 0, 0),
        ):
            with self.subTest(mode=mode):
                row = self._native_retry(mode)
                self.assertEqual(row["result"]["ok"], good)
                self.assertEqual(row["result"]["error"], err)
                self.assertEqual((row["captures"], row["reloads"], row["samples"]),
                                 (caps, reloads, samples))
                self.assertLessEqual(row["captures"], 2)
                self.assertLessEqual(row["reloads"], 1)
                self.assertEqual(row["clock_bad"], 0)
                self.assertLess(row["elapsed"], 60000)
        bad = self._native_retry("recap_err")
        self.assertEqual(bad["events"], ["switch", "capture", "sample",
                                         "sample", "capture"])
        self.assertEqual((bad["captures"], bad["reloads"]), (2, 0))
        switched = self._native_retry("switch")
        self.assertEqual(switched["events"],
                         ["switch", "reload", "switch", "capture"])
        self.assertEqual(switched["samples"], 0)
        self.assertLess(switched["elapsed"], 60000)


if __name__ == "__main__":
    unittest.main()
