from __future__ import annotations

import asyncio
import unittest

from aqualinkd_validator.testcases import (
    AssertDeviceStep,
    AssertLogStep,
    AssertNoLogStep,
    ExerciseHeaterStep,
    ExpectSerialStep,
    HttpRequestStep,
    RestoreOriginalStateStep,
    SerialSendStep,
    SetDeviceStep,
    SetSetpointStep,
    UnsupportedTestcaseKeywords,
    WaitForStableEquipmentStep,
    WaitForStep,
)
from aqualinkd_validator.testcases import (
    TestcaseDefinition as DeclarativeCase,
)
from aqualinkd_validator.testcases import (
    TestcaseExecutionFailure as ExecutionFailure,
)
from aqualinkd_validator.testcases import (
    TestcaseExecutor as DeclarativeExecutor,
)
from aqualinkd_validator.testcases import (
    TestcaseRequirements as CaseRequirements,
)


class RecordingKeywords(UnsupportedTestcaseKeywords):
    def __init__(self, fail_keyword: str | None = None) -> None:
        self.calls: list[str] = []
        self._fail_keyword = fail_keyword

    async def wait_for(self, step: WaitForStep) -> None:
        await self._record(step.keyword)

    async def serial_send(self, step: SerialSendStep) -> None:
        await self._record(step.keyword)

    async def expect_serial(self, step: ExpectSerialStep) -> None:
        await self._record(step.keyword)

    async def http_request(self, step: HttpRequestStep) -> None:
        await self._record(step.keyword)

    async def set_device(self, step: SetDeviceStep) -> None:
        await self._record(step.keyword)

    async def set_setpoint(self, step: SetSetpointStep) -> None:
        await self._record(step.keyword)

    async def exercise_heater(self, step: ExerciseHeaterStep) -> None:
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

    def test_dispatches_serial_keywords(self) -> None:
        asyncio.run(self._execute_serial())

    async def _execute_success(self) -> None:
        keywords = RecordingKeywords()
        result = await DeclarativeExecutor(keywords).execute(make_testcase())

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
            ExecutionFailure,
            r"pda\.filter\.steps\[2\]\.assert_device: simulated timeout",
        ):
            await DeclarativeExecutor(keywords).execute(make_testcase())
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
            await DeclarativeExecutor(TwoFailures()).execute(make_testcase())
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
        task = asyncio.create_task(
            DeclarativeExecutor(keywords).execute(make_testcase())
        )
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(restored.is_set())
        self.assertEqual(keywords.calls, ["wait_for", "restore_original_state"])

    async def _execute_serial(self) -> None:
        keywords = RecordingKeywords()
        testcase = DeclarativeCase(
            schema=1,
            identifier="rs485.probe",
            description="Probe exchange",
            mode="rs485-panel-emulator",
            access="read-write",
            requirements=CaseRequirements(protocol="rs485"),
            steps=(
                SerialSendStep(bytes.fromhex("100260001003"), 1),
                ExpectSerialStep(bytes.fromhex("1002000100031003"), 1),
                HttpRequestStep("PUT", "/api/Filter_Pump/set", "1", 2),
            ),
            finally_steps=(),
        )
        result = await DeclarativeExecutor(keywords).execute(testcase)
        self.assertEqual(
            keywords.calls,
            ["serial_send", "expect_serial", "http_request"],
        )
        self.assertEqual(
            [execution.keyword for execution in result.steps],
            ["serial_send", "expect_serial", "http_request"],
        )


def make_testcase() -> DeclarativeCase:
    return DeclarativeCase(
        schema=1,
        identifier="pda.filter",
        description="Filter action",
        mode="physical-panel",
        access="read-write",
        requirements=CaseRequirements(protocol="pda"),
        steps=(
            WaitForStep("pda.initialized", 180),
            SetDeviceStep("Filter_Pump", "on", 130, 90, 10),
            AssertDeviceStep("Filter_Pump", "requested", 10),
        ),
        finally_steps=(RestoreOriginalStateStep(300),),
    )


if __name__ == "__main__":
    unittest.main()
