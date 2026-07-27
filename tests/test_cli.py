from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.cli import main
from aqualinkd_validator.config import (
    ConfigurationError,
    validate_live_serial_device,
)


class CliTests(unittest.TestCase):
    def test_run_requires_matching_explicit_character_device(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "aqualinkd.conf"
            config.write_text("serial_port=/dev/null\n", encoding="utf-8")
            self.assertEqual(
                validate_live_serial_device(config, Path("/dev/null")),
                Path("/dev/null"),
            )
            with self.assertRaisesRegex(ConfigurationError, "does not match"):
                validate_live_serial_device(config, Path("/dev/zero"))

    def test_run_creates_performance_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mock = root / "mock-aqualinkd"
            mock.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "import time\n"
                "print('mock started', flush=True)\n"
                "print('mock warning', file=sys.stderr, flush=True)\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            mock.chmod(0o755)
            config = root / "aqualinkd.conf"
            config.write_text("serial_port=/dev/null\n", encoding="utf-8")
            artifacts = root / "artifacts"

            with self.assertRaises(SystemExit) as exit_context:
                main(
                    [
                        "run",
                        "--mode",
                        "live-panel",
                        "--allow-live-panel",
                        "--serial-device",
                        "/dev/null",
                        "--aqualinkd",
                        str(mock),
                        "--config",
                        str(config),
                        "--duration",
                        "0.12",
                        "--sample-interval",
                        "0.02",
                        "--terminate-grace",
                        "1",
                        "--label",
                        "mock",
                        "--artifacts",
                        str(artifacts),
                    ]
                )
            self.assertEqual(exit_context.exception.code, 0)
            run_dirs = list(artifacts.iterdir())
            self.assertEqual(len(run_dirs), 1)
            run_dir = run_dirs[0]
            result = json.loads((run_dir / "result.json").read_text())
            performance = json.loads((run_dir / "performance.json").read_text())
            manifest = json.loads((run_dir / "manifest.yaml").read_text())
            self.assertEqual(result["status"], "passed")
            self.assertGreater(performance["process"]["sample_count"], 1)
            self.assertEqual(manifest["config"]["name"], "aqualinkd.conf")
            self.assertIn("mock started", (run_dir / "stdout.log").read_text())


if __name__ == "__main__":
    unittest.main()
