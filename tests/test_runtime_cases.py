from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from aqualinkd_validator.engine import RestorationSession, ScenarioRecorder
from aqualinkd_validator.engine.runtime_cases import RuntimeCaseRunner
from aqualinkd_validator.run_targets import RuntimeCaseId
from aqualinkd_validator.supervisor import OutputMonitor, ScenarioContext, Timeline


class RuntimeCaseRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_failure_and_continues_noninitialization_cases(
        self,
    ) -> None:
        report: dict[str, object] = {
            "api_base_url": "http://127.0.0.1:8080",
            "api_endpoint_source": "test",
            "cases": [],
            "restoration": {"status": "not-needed"},
            "measurements": [],
            "skipped": [],
        }
        observed: list[RuntimeCaseId] = []

        async def operation(case_id: RuntimeCaseId) -> None:
            observed.append(case_id)
            if case_id == RuntimeCaseId.AQUAPDA_TRANSPORT:
                raise RuntimeError("injected assertion failure")

        async def unexpected_restore(name: str) -> list[str]:
            raise AssertionError(f"unexpected restoration: {name}")

        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            timeline = Timeline(artifact_dir / "timeline.jsonl", time.monotonic_ns())
            context = ScenarioContext(
                artifact_dir=artifact_dir,
                monitor=OutputMonitor(),
                timeline=timeline,
            )
            try:
                outcome = await RuntimeCaseRunner(
                    suite_name="runtime-cases",
                    case_ids=(
                        RuntimeCaseId.INITIALIZATION,
                        RuntimeCaseId.AQUAPDA_TRANSPORT,
                        RuntimeCaseId.AQUAPDA_MENU_WALK,
                    ),
                    report=report,
                    recorder=ScenarioRecorder(report),
                    restoration=RestorationSession(),
                    operation=operation,
                    restore=unexpected_restore,
                    initialized=lambda: True,
                ).run(context)
            finally:
                timeline.close()

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "case_failures")
        self.assertEqual(observed, list(RuntimeCaseId))
        self.assertTrue(report["safe_to_continue"])
        self.assertEqual(
            [case["status"] for case in report["cases"]],  # type: ignore[index]
            ["passed", "failed", "passed"],
        )
