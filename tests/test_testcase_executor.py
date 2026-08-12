from __future__ import annotations

import asyncio
import unittest

from aqualinkd_validator.testcases import (
    AssertDeviceStep,
    AssertLogStep,
    AssertNoLogStep,
    RestoreOriginalStateStep,
    SetDeviceStep,
    SetSetpointStep,
    TestcaseDefinition,
    TestcaseExecutionFailure,
    TestcaseExecutor,
    TestcaseRequirements,
    WaitForStableEquipmentStep,
    WaitForStep,
)


class RecordingKeywords:
    def __init__(self, fail_keyword: str | None = None) -> None:
        self.calls: list[str] = []
        self._fail_keyword = fail_keyword

    async def wait_for(self, step: WaitForStep) -> None:
        await self._record(step.keyword)

    async def set_device(self, step: SetDeviceStep) -> None:
        await self._record(step.keyword)

    async def set_setpoint(self, step: SetSetpointStep) -> None:
        await self._record(step.keyword)

    async def assert_device(self, step: AssertDeviceStep) -> None:
        await self._record(step.keyword)

    async def assert_log(self, step: AssertLogStep) -> None:
        await self._record(step.keyword)

    async def assert_no_log(self, step: AssertNoLogStep) -> None:
        await self._record(step.keyword)

    async def wait_for_stable_equipment(
        self,
        step: WaitForStableEquipmentStep,
    ) -> None:
        await self._record(step.keyword)

    async def restore_original_state(
        self,
        step: RestoreOriginalStateStep,
    ) -> None:
        await self._record(step.keyword)

    async def _record(self, keyword: str) -> None:
        self.calls.append(keyword)
        if self._fail_keyword == keyword:
            raise TimeoutError("simulated timeout")


class TestcaseExecutorTests(unittest.TestCase):
    def test_executes_typed_steps_in_order_and_always_runs_finally(self) -> None:
        asyncio.run(self._execute_success())

    def test_step_failure_is_contextual_and_runs_restoration(self) -> None:
        asyncio.run(self._execute_failure())

    def test_cleanup_failure_does_not_hide_primary_failure(self) -> None:
        asyncio.run(self._execute_two_failures())

    def test_cancellation_propagates_after_restoration(self) -> None:
        asyncio.run(self._execute_cancellation())

    async def _execute_success(self) -> None:
        keywords = RecordingKeywords()
        result = await TestcaseExecutor(keywords).execute(testcase())

        self.assertEqual(
            keywords.calls,
            ["wait_for", "set_device", "assert_device", "restore_original_state"],
        )
        self.assertEqual(result.identifier, "pda.filter")
        self.assertEqual(
            [execution.section for execution in result.steps],
            ["steps", "steps", "steps", "finally"],
        )

    async def _execute_failure(self) -> None:
        keywords = RecordingKeywords(fail_keyword="assert_device")
        with self.assertRaisesRegex(
            TestcaseExecutionFailure,
            r"pda\.filter\.steps\[2\]\.assert_device: simulated timeout",
        ):
            await TestcaseExecutor(keywords).execute(testcase())
        self.assertEqual(
            keywords.calls,
            ["wait_for", "set_device", "assert_device", "restore_original_state"],
        )

    async def _execute_two_failures(self) -> None:
        class TwoFailures(RecordingKeywords):
            async def assert_device(self, step: AssertDeviceStep) -> None:
                self.calls.append(step.keyword)
                raise AssertionError("state mismatch")

            async def restore_original_state(
                self,
                step: RestoreOriginalStateStep,
            ) -> None:
                self.calls.append(step.keyword)
                raise TimeoutError("restore timeout")

        with self.assertRaises(BaseExceptionGroup) as caught:
            await TestcaseExecutor(TwoFailures()).execute(testcase())
        self.assertEqual(len(caught.exception.exceptions), 2)
        self.assertIn("failed and cleanup also failed", str(caught.exception))

    async def _execute_cancellation(self) -> None:
        started = asyncio.Event()
        restored = asyncio.Event()

        class CancellableKeywords(RecordingKeywords):
            async def wait_for(self, step: WaitForStep) -> None:
                self.calls.append(step.keyword)
                started.set()
                await asyncio.Event().wait()

            async def restore_original_state(
                self,
                step: RestoreOriginalStateStep,
            ) -> None:
                self.calls.append(step.keyword)
                restored.set()

        keywords = CancellableKeywords()
        task = asyncio.create_task(TestcaseExecutor(keywords).execute(testcase()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(restored.is_set())
        self.assertEqual(keywords.calls, ["wait_for", "restore_original_state"])


def testcase() -> TestcaseDefinition:
    return TestcaseDefinition(
        schema=1,
        identifier="pda.filter",
        description="Filter action",
        mode="physical-panel",
        access="read-write",
        requirements=TestcaseRequirements(protocol="pda"),
        steps=(
            WaitForStep("pda.initialized", 180),
            SetDeviceStep("Filter_Pump", "on", 130, 90, 10),
            AssertDeviceStep("Filter_Pump", "requested", 10),
        ),
        finally_steps=(RestoreOriginalStateStep(300),),
    )


if __name__ == "__main__":
    unittest.main()
