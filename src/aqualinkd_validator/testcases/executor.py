from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .model import (
    AssertDeviceStep,
    AssertLogStep,
    AssertNoLogStep,
    ExerciseDiscoveredDevicesStep,
    ExerciseHeaterStep,
    ExerciseProbeTransitionStep,
    ExerciseSpaHeatingStep,
    ExerciseStatusRetryStep,
    ObserveSleepCycleStep,
    RestoreOriginalStateStep,
    SetDeviceStep,
    SetSetpointStep,
    TestcaseDefinition,
    TestcaseStep,
    VerifyEquipmentStatusStep,
    WaitForStableEquipmentStep,
    WaitForStep,
)


class TestcaseKeywords(Protocol):
    """Operations that a protocol/runtime adapter exposes to YAML tests."""

    async def wait_for(self, step: WaitForStep) -> None: ...

    async def set_device(self, step: SetDeviceStep) -> None: ...

    async def set_setpoint(self, step: SetSetpointStep) -> None: ...

    async def exercise_heater(self, step: ExerciseHeaterStep) -> None: ...

    async def exercise_spa_heating(self, step: ExerciseSpaHeatingStep) -> None: ...

    async def assert_device(self, step: AssertDeviceStep) -> None: ...

    async def assert_log(self, step: AssertLogStep) -> None: ...

    async def assert_no_log(self, step: AssertNoLogStep) -> None: ...

    async def wait_for_stable_equipment(
        self,
        step: WaitForStableEquipmentStep,
    ) -> None: ...

    async def restore_original_state(
        self,
        step: RestoreOriginalStateStep,
    ) -> None: ...

    async def verify_equipment_status(
        self, step: VerifyEquipmentStatusStep
    ) -> None: ...

    async def exercise_discovered_devices(
        self, step: ExerciseDiscoveredDevicesStep
    ) -> None: ...

    async def observe_sleep_cycle(self, step: ObserveSleepCycleStep) -> None: ...

    async def exercise_status_retry(self, step: ExerciseStatusRetryStep) -> None: ...

    async def exercise_probe_transition(
        self, step: ExerciseProbeTransitionStep
    ) -> None: ...


@dataclass(frozen=True)
class StepExecution:
    section: str
    index: int
    keyword: str
    duration_seconds: float


@dataclass(frozen=True)
class TestcaseExecution:
    identifier: str
    steps: tuple[StepExecution, ...]
    duration_seconds: float


class TestcaseExecutionFailure(RuntimeError):
    """Adds testcase and step context to a keyword failure."""


class TestcaseExecutor:
    """Runs a validated testcase through a typed keyword implementation."""

    def __init__(
        self,
        keywords: TestcaseKeywords,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._keywords = keywords
        self._clock = clock

    async def execute(self, testcase: TestcaseDefinition) -> TestcaseExecution:
        started = self._clock()
        executions: list[StepExecution] = []
        primary_error: BaseException | None = None
        cleanup_errors: list[BaseException] = []

        try:
            for index, step in enumerate(testcase.steps):
                executions.append(
                    await self._execute_step(
                        testcase.identifier,
                        "steps",
                        index,
                        step,
                    )
                )
        except BaseException as error:
            primary_error = error

        for index, step in enumerate(testcase.finally_steps):
            try:
                executions.append(
                    await self._execute_step(
                        testcase.identifier,
                        "finally",
                        index,
                        step,
                    )
                )
            except BaseException as error:
                cleanup_errors.append(error)

        if primary_error is not None and cleanup_errors:
            raise BaseExceptionGroup(
                f"{testcase.identifier} failed and cleanup also failed",
                [primary_error, *cleanup_errors],
            )
        if primary_error is not None:
            raise primary_error
        if cleanup_errors:
            if len(cleanup_errors) == 1:
                raise cleanup_errors[0]
            raise BaseExceptionGroup(
                f"{testcase.identifier} cleanup failed",
                cleanup_errors,
            )

        return TestcaseExecution(
            identifier=testcase.identifier,
            steps=tuple(executions),
            duration_seconds=self._clock() - started,
        )

    async def _execute_step(
        self,
        testcase_id: str,
        section: str,
        index: int,
        step: TestcaseStep,
    ) -> StepExecution:
        started = self._clock()
        try:
            await self._dispatch(step)
        except BaseException as error:
            if isinstance(
                error,
                (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
            ):
                raise
            raise TestcaseExecutionFailure(
                f"{testcase_id}.{section}[{index}].{step.keyword}: {error}"
            ) from error
        return StepExecution(
            section=section,
            index=index,
            keyword=step.keyword,
            duration_seconds=self._clock() - started,
        )

    async def _dispatch(self, step: TestcaseStep) -> None:
        if isinstance(step, WaitForStep):
            await self._keywords.wait_for(step)
        elif isinstance(step, SetDeviceStep):
            await self._keywords.set_device(step)
        elif isinstance(step, SetSetpointStep):
            await self._keywords.set_setpoint(step)
        elif isinstance(step, ExerciseHeaterStep):
            await self._keywords.exercise_heater(step)
        elif isinstance(step, ExerciseSpaHeatingStep):
            await self._keywords.exercise_spa_heating(step)
        elif isinstance(step, AssertDeviceStep):
            await self._keywords.assert_device(step)
        elif isinstance(step, AssertLogStep):
            await self._keywords.assert_log(step)
        elif isinstance(step, AssertNoLogStep):
            await self._keywords.assert_no_log(step)
        elif isinstance(step, WaitForStableEquipmentStep):
            await self._keywords.wait_for_stable_equipment(step)
        elif isinstance(step, RestoreOriginalStateStep):
            await self._keywords.restore_original_state(step)
        elif isinstance(step, VerifyEquipmentStatusStep):
            await self._keywords.verify_equipment_status(step)
        elif isinstance(step, ExerciseDiscoveredDevicesStep):
            await self._keywords.exercise_discovered_devices(step)
        elif isinstance(step, ObserveSleepCycleStep):
            await self._keywords.observe_sleep_cycle(step)
        elif isinstance(step, ExerciseStatusRetryStep):
            await self._keywords.exercise_status_retry(step)
        elif isinstance(step, ExerciseProbeTransitionStep):
            await self._keywords.exercise_probe_transition(step)
