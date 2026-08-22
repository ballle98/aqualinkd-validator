from __future__ import annotations

import unittest
from typing import cast

from aqualinkd_validator import testcases as testcase_types
from aqualinkd_validator.engine import RestorationSession, ScenarioRecorder
from aqualinkd_validator.interfaces import ScenarioContext
from aqualinkd_validator.testing import (
    FakeOrderedLogEvents,
    FakeTimeline,
    MemoryArtifactStore,
)


class DeclarativeScenarioRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_serializes_suite_results_and_timeline(self) -> None:
        report: dict[str, object] = {
            "api_base_url": "http://127.0.0.1:8080",
            "api_endpoint_source": "test",
            "cases": [],
            "restoration": {"status": "not-needed"},
            "measurements": [],
            "skipped": [],
        }
        cases = (self._testcase("one"), self._testcase("two"))
        artifacts = MemoryArtifactStore()
        timeline = FakeTimeline()
        context = ScenarioContext(
            artifacts=artifacts,
            monitor=FakeOrderedLogEvents(),
            timeline=timeline,
        )

        async def unexpected_restore(
            context: ScenarioContext,
            name: str,
        ) -> list[str]:
            raise AssertionError(f"unexpected restoration: {context} {name}")

        outcome = await testcase_types.DeclarativeScenarioRunner(
            suite_name="test-suite",
            testcases=cases,
            report=report,
            recorder=ScenarioRecorder(report),
            restoration=RestorationSession(),
            keywords=lambda identifier: cast(
                testcase_types.TestcaseKeywords,
                object(),
            ),
            restore=unexpected_restore,
            initialized=lambda: True,
        ).run(context)

        self.assertEqual(outcome.status, "passed")
        self.assertEqual(report["testcases"], ["one", "two"])
        self.assertEqual(len(report["cases"]), 2)  # type: ignore[arg-type]
        self.assertIn("scenario.json", artifacts.values)
        kinds = [event["kind"] for event in timeline.events]
        self.assertEqual(kinds.count("scenario_started"), 2)
        self.assertEqual(kinds.count("scenario_finished"), 2)

    @staticmethod
    def _testcase(identifier: str) -> testcase_types.TestcaseDefinition:
        return testcase_types.TestcaseDefinition(
            schema=1,
            identifier=identifier,
            description=identifier,
            mode="physical-panel",
            access="read-only",
            requirements=testcase_types.TestcaseRequirements(protocol="pda"),
            steps=(),
            finally_steps=(),
        )
