import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xdmovies import usage


class UsageFormatTests(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(usage.format_bytes(0), "0 B")
        self.assertEqual(usage.format_bytes(1024), "1.00 KiB")
        self.assertEqual(usage.format_bytes(1024 ** 3), "1.00 GiB")

    def test_format_duration(self):
        self.assertEqual(usage.format_duration(45), "45s")
        self.assertEqual(usage.format_duration(3661), "1h 01m")
        self.assertEqual(usage.format_duration(90061), "1d 01h")

    def test_render_html(self):
        text = usage.render_html({
            "interface": "eth0",
            "month_rx": 1024 ** 3,
            "month_tx": 512 * 1024 ** 2,
            "month_error": None,
            "since_rx": 100,
            "since_tx": 50,
            "elapsed": 3661,
            "rebaselined": False,
        })
        self.assertIn("Overall — current month", text)
        self.assertIn("Since last bot restart", text)
        self.assertIn("1.00 GiB", text)
        self.assertIn("1h 01m", text)


class UsageVnstatTests(unittest.TestCase):
    def test_parse_current_month_list(self):
        now = datetime.datetime(2026, 9, 30)
        payload = {
            "interfaces": [{
                "name": "eth0",
                "traffic": {
                    "month": [
                        {"id": 8, "date": {"year": 2026, "month": 8}, "rx": 10, "tx": 20},
                        {"id": 9, "date": {"year": 2026, "month": 9}, "rx": 30, "tx": 40},
                    ]
                },
            }]
        }
        self.assertEqual(usage.parse_vnstat_month(payload, "eth0", now), (30, 40))

    def test_parse_month_id_only(self):
        now = datetime.datetime(2026, 9, 30)
        payload = {
            "interfaces": [{
                "name": "eth0",
                "traffic": {"month": [{"id": 9, "rx": 70, "tx": 80}]},
            }]
        }
        self.assertEqual(usage.parse_vnstat_month(payload, "eth0", now), (70, 80))

    def test_parse_month_dict(self):
        payload = {
            "interfaces": [{
                "name": "eth0",
                "traffic": {"month": {"rx": 50, "tx": 60}},
            }]
        }
        self.assertEqual(usage.parse_vnstat_month(payload, "eth0"), (50, 60))

    def test_missing_interface_is_unavailable(self):
        with self.assertRaises(usage.UsageUnavailable):
            usage.parse_vnstat_month({"interfaces": []}, "eth0")


class UsageSnapshotTests(unittest.TestCase):
    def test_delta_since_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            state_path = state_dir / "usage.json"
            with patch.object(usage, "_STATE_DIR", state_dir), patch.object(usage, "_STATE_PATH", state_path):
                with patch.object(usage, "_read_interface_counters", return_value=("eth0", 100, 50)):
                    usage.reset_baseline(now=1000.0)
                with patch.object(usage, "_read_interface_counters", return_value=("eth0", 300, 120)), patch.object(
                    usage, "_vnstat_month", return_value=((500, 60), None)
                ):
                    snapshot = usage.build_snapshot(now=2000.0)
                self.assertEqual(snapshot["since_rx"], 200)
                self.assertEqual(snapshot["since_tx"], 70)
                self.assertEqual(snapshot["month_rx"], 500)
                self.assertEqual(snapshot["elapsed"], 1000.0)
                self.assertFalse(snapshot["rebaselined"])

    def test_counter_reset_rebaselines(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            state_path = state_dir / "usage.json"
            with patch.object(usage, "_STATE_DIR", state_dir), patch.object(usage, "_STATE_PATH", state_path):
                with patch.object(usage, "_read_interface_counters", return_value=("eth0", 1000, 500)):
                    usage.reset_baseline(now=1000.0)
                with patch.object(usage, "_read_interface_counters", return_value=("eth0", 10, 5)), patch.object(
                    usage, "_vnstat_month", return_value=(None, "no data")
                ):
                    snapshot = usage.build_snapshot(now=2000.0)
                self.assertTrue(snapshot["rebaselined"])
                self.assertEqual(snapshot["since_rx"], 0)
                self.assertEqual(snapshot["since_tx"], 0)


if __name__ == "__main__":
    unittest.main()