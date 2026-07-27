from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.supervisor import supervise


class SupervisorTests(unittest.TestCase):
    def test_captures_stdout_stderr_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            result = asyncio.run(
                supervise(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import sys,time;"
                            "print('hello',flush=True);"
                            "print('warning',file=sys.stderr,flush=True);"
                            "time.sleep(0.15)"
                        ),
                    ],
                    artifact_dir,
                    cwd=None,
                    duration_seconds=None,
                    sample_interval_seconds=0.02,
                    terminate_grace_seconds=1.0,
                )
            )
            self.assertEqual(result.status, "passed")
            self.assertEqual((artifact_dir / "stdout.log").read_text(), "hello\n")
            self.assertEqual((artifact_dir / "stderr.log").read_text(), "warning\n")
            self.assertTrue((artifact_dir / "metrics.jsonl").read_text())
            events = [
                json.loads(line)
                for line in (artifact_dir / "timeline.jsonl").read_text().splitlines()
            ]
            streams = {
                event.get("stream")
                for event in events
                if event["kind"] == "process_output"
            }
            self.assertEqual(streams, {"stdout", "stderr"})

    def test_duration_stops_only_spawned_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            result = asyncio.run(
                supervise(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    artifact_dir,
                    cwd=None,
                    duration_seconds=0.05,
                    sample_interval_seconds=0.01,
                    terminate_grace_seconds=1.0,
                )
            )
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.reason, "duration_elapsed")
            self.assertIsNotNone(result.child_returncode)


if __name__ == "__main__":
    unittest.main()
