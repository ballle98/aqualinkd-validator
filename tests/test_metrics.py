from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.metrics import summarize_metrics


class MetricsTests(unittest.TestCase):
    def test_summarizes_process_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.jsonl"
            samples = [
                {
                    "offset_ns": 1_000_000_000,
                    "cpu_user_ticks": 10,
                    "cpu_system_ticks": 5,
                    "rss_bytes": 100,
                    "threads": 2,
                    "voluntary_context_switches": 10,
                    "nonvoluntary_context_switches": 1,
                    "read_bytes": 1000,
                    "write_bytes": 2000,
                },
                {
                    "offset_ns": 3_000_000_000,
                    "cpu_user_ticks": 30,
                    "cpu_system_ticks": 15,
                    "rss_bytes": 300,
                    "threads": 4,
                    "voluntary_context_switches": 16,
                    "nonvoluntary_context_switches": 3,
                    "read_bytes": 1500,
                    "write_bytes": 2600,
                },
            ]
            path.write_text(
                "".join(json.dumps(sample) + "\n" for sample in samples),
                encoding="utf-8",
            )
            summary = summarize_metrics(path)
            ticks = os.sysconf("SC_CLK_TCK")
            self.assertEqual(summary["sample_count"], 2)
            self.assertEqual(summary["sample_window_seconds"], 2.0)
            self.assertAlmostEqual(summary["cpu"]["total_seconds"], 30 / ticks)
            self.assertEqual(summary["rss_bytes"]["average"], 200)
            self.assertEqual(summary["rss_bytes"]["maximum"], 300)
            self.assertEqual(summary["context_switches"]["voluntary"], 6)
            self.assertEqual(summary["io_bytes"]["write"], 600)


if __name__ == "__main__":
    unittest.main()
