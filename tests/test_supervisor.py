from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from aqualinkd_validator.supervisor import (
    ScenarioContext,
    ScenarioOutcome,
    supervise,
)


class ReadyScenario:
    async def run(self, context: ScenarioContext) -> ScenarioOutcome:
        await context.monitor.wait_for("READY", timeout_seconds=1.0)
        return ScenarioOutcome(status="passed", reason="scenario_completed")


class SupervisorTests(unittest.TestCase):
    def test_scenario_completion_without_duration_stops_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            result = asyncio.run(
                supervise(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import time;"
                            "print('READY',flush=True);"
                            "time.sleep(60)"
                        ),
                    ],
                    artifact_dir,
                    cwd=None,
                    duration_seconds=None,
                    sample_interval_seconds=0.02,
                    terminate_grace_seconds=1.0,
                    scenario=ReadyScenario(),
                )
            )
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.reason, "scenario_completed")
            self.assertIsNotNone(result.child_returncode)

    def test_captures_stdout_stderr_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            console = io.StringIO()
            with redirect_stdout(console):
                result = asyncio.run(
                    supervise(
                        [
                            sys.executable,
                            "-c",
                            (
                                "import sys,time;"
                                "print('Notice: Waiting for Control Panel "
                                "probe',flush=True);"
                                "print(\"Notice: Got probe on '0x60'\","
                                "flush=True);"
                                "print(\"Info: Starting programming thread "
                                "'Init PDA'\",flush=True);"
                                "print('Warning: test warning',flush=True);"
                                "print('Error: test error',"
                                "file=sys.stderr,flush=True);"
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
            self.assertEqual(
                (artifact_dir / "stdout.log").read_text(),
                "Notice: Waiting for Control Panel probe\n"
                "Notice: Got probe on '0x60'\n"
                "Info: Starting programming thread 'Init PDA'\n"
                "Warning: test warning\n",
            )
            self.assertEqual(
                (artifact_dir / "stderr.log").read_text(),
                "Error: test error\n",
            )
            self.assertIn(
                "[AQUALINKD WARNING] Warning: test warning",
                console.getvalue(),
            )
            self.assertIn(
                "[AQUALINKD ERROR] Error: test error",
                console.getvalue(),
            )
            self.assertIn(
                "[STATE ] Waiting on control-panel probe",
                console.getvalue(),
            )
            self.assertIn(
                "[STATE ] Control-panel probe received",
                console.getvalue(),
            )
            self.assertIn(
                "[STATE ] Init PDA task created; waiting to become active",
                console.getvalue(),
            )
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
