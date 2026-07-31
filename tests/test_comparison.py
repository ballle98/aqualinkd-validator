from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.comparison import format_comparison, load_comparison


class ComparisonTests(unittest.TestCase):
    def test_formats_runs_and_warns_about_config_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._write_run(root / "first", "rel-2.3.7", "a" * 40, "one")
            second = self._write_run(
                root / "second", "upstream-master", "b" * 40, "two"
            )
            comparison = load_comparison([first, second])
            rendered = format_comparison(comparison)
            self.assertIn("rel-2.3.7", rendered)
            self.assertIn("upstream-master", rendered)
            self.assertIn("Scenario timings (ms)", rendered)
            self.assertIn("pda.init", rendered)
            self.assertIn("pda.init.programmer", rendered)
            self.assertIn("config fingerprint differs", rendered)

    def test_requires_two_runs(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least two"):
            load_comparison([Path("/tmp/only-one")])

    def _write_run(
        self, path: Path, label: str, commit: str, config_hash: str
    ) -> Path:
        path.mkdir()
        manifest = {
            "label": label,
            "source": {"commit": commit},
            "host": {
                "architecture": "aarch64",
                "cpu_model": "Pi",
                "kernel": "test",
                "container": "docker",
            },
            "config": {"sha256": config_hash},
            "sampling": {"interval_seconds": 1.0},
        }
        performance = {
            "process": {
                "sample_count": 10,
                "cpu": {"utilization_percent": 1.25, "total_seconds": 0.5},
                "rss_bytes": {"maximum": 10 * 1024 * 1024},
                "threads": {"average": 3.0},
                "context_switches": {"voluntary": 10, "nonvoluntary": 2},
            },
            "scenario": {
                "measurements": [
                    {
                        "name": "pda.init",
                        "activation_ms": 12.0,
                        "programmer_duration_ms": 111.456,
                        "duration_ms": 123.456,
                    },
                ]
            },
        }
        (path / "manifest.yaml").write_text(json.dumps(manifest), encoding="utf-8")
        (path / "performance.json").write_text(
            json.dumps(performance), encoding="utf-8"
        )
        (path / "result.json").write_text(
            json.dumps({"status": "passed"}), encoding="utf-8"
        )
        return path


if __name__ == "__main__":
    unittest.main()
