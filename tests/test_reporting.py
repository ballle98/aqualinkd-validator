from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.adapters import FileArtifactStore
from aqualinkd_validator.engine import ScenarioRecorder


class ScenarioRecorderTests(unittest.TestCase):
    def test_records_measurement_skip_and_artifact(self) -> None:
        report: dict[str, object] = {"measurements": [], "skipped": []}
        recorder = ScenarioRecorder(report)
        recorder.append_measurement(
            name="toggle",
            category="device",
            phase="test",
            target="Filter_Pump",
            requested_value=True,
            start_offset_ns=1_000_000,
            api_ack_offset_ns=2_000_000,
            task_active_offset_ns=3_000_000,
            log_completion_offset_ns=7_000_000,
            state_observed_offset_ns=8_000_000,
        )
        recorder.skip("optional", "not configured")

        with tempfile.TemporaryDirectory() as directory:
            recorder.write(FileArtifactStore(Path(directory)))
            written = json.loads(
                (Path(directory) / "scenario.json").read_text(encoding="utf-8")
            )

        measurement = written["measurements"][0]
        self.assertEqual(measurement["duration_ms"], 7.0)
        self.assertEqual(measurement["programmer_duration_ms"], 4.0)
        self.assertEqual(
            written["skipped"],
            [{"name": "optional", "reason": "not configured"}],
        )

    def test_unwraps_single_exception_group(self) -> None:
        error = ExceptionGroup("task group", [RuntimeError("bad state")])
        self.assertEqual(
            ScenarioRecorder.format_exception(error),
            "RuntimeError: bad state",
        )
