from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from .model import (
    AssertDeviceStep,
    AssertDeviceValueStep,
    AssertLogStep,
    AssertNoLogStep,
    ExerciseDiscoveredDevicesStep,
    ExerciseHeaterStep,
    ExerciseProbeTransitionStep,
    ExerciseSpaHeatingStep,
    ExerciseStatusRetryStep,
    ExpectPanelCommandStep,
    ExpectSerialStep,
    HttpRequestStep,
    ObserveSleepCycleStep,
    RestoreOriginalStateStep,
    ReturnPdaHomeStep,
    SerialSendStep,
    SetDeviceStep,
    SetPowerCenterModeStep,
    SetPowerCenterTemperatureStep,
    SetSetpointStep,
    TestcaseDefinition,
    TestcaseStep,
    VerifyEquipmentStatusStep,
    WaitForStableEquipmentStep,
    WaitForStep,
    WaitHttpJsonStep,
)


class TestcaseKeywords(Protocol):
    """Operations that a protocol/runtime adapter exposes to YAML tests."""

    async def wait_for(self, step: WaitForStep) -> None: ...

    async def return_pda_home(self, step: ReturnPdaHomeStep) -> None: ...

    async def set_power_center_mode(self, step: SetPowerCenterModeStep) -> None: ...

    async def set_power_center_temperature(
        self, step: SetPowerCenterTemperatureStep
    ) -> None: ...

    async def serial_send(self, step: SerialSendStep) -> None: ...

    async def expect_serial(self, step: ExpectSerialStep) -> None: ...

    async def http_request(self, step: HttpRequestStep) -> None: ...

    async def wait_http_json(self, step: WaitHttpJsonStep) -> None: ...

    async def expect_panel_command(self, step: ExpectPanelCommandStep) -> None: ...

    async def set_device(self, step: SetDeviceStep) -> None: ...

    async def set_setpoint(self, step: SetSetpointStep) -> None: ...

    async def exercise_heater(self, step: ExerciseHeaterStep) -> None: ...

    async def exercise_spa_heating(self, step: ExerciseSpaHeatingStep) -> None: ...

    async def assert_device(self, step: AssertDeviceStep) -> None: ...

    async def assert_device_value(self, step: AssertDeviceValueStep) -> None: ...

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


class UnsupportedTestcaseKeywords:
    """Explicit defaults for runtimes that implement only some keywords."""

    async def wait_for(self, step: WaitForStep) -> None:
        self._unsupported(step.keyword)

    async def return_pda_home(self, step: ReturnPdaHomeStep) -> None:
        self._unsupported(step.keyword)

    async def set_power_center_mode(self, step: SetPowerCenterModeStep) -> None:
        self._unsupported(step.keyword)

    async def set_power_center_temperature(
        self, step: SetPowerCenterTemperatureStep
    ) -> None:
        self._unsupported(step.keyword)

    async def serial_send(self, step: SerialSendStep) -> None:
        self._unsupported(step.keyword)

    async def expect_serial(self, step: ExpectSerialStep) -> None:
        self._unsupported(step.keyword)

    async def http_request(self, step: HttpRequestStep) -> None:
        self._unsupported(step.keyword)

    async def wait_http_json(self, step: WaitHttpJsonStep) -> None:
        self._unsupported(step.keyword)

    async def expect_panel_command(self, step: ExpectPanelCommandStep) -> None:
        self._unsupported(step.keyword)

    async def set_device(self, step: SetDeviceStep) -> None:
        self._unsupported(step.keyword)

    async def set_setpoint(self, step: SetSetpointStep) -> None:
        self._unsupported(step.keyword)

    async def exercise_heater(self, step: ExerciseHeaterStep) -> None:
        self._unsupported(step.keyword)

    async def exercise_spa_heating(self, step: ExerciseSpaHeatingStep) -> None:
        self._unsupported(step.keyword)

    async def assert_device(self, step: AssertDeviceStep) -> None:
        self._unsupported(step.keyword)

    async def assert_device_value(self, step: AssertDeviceValueStep) -> None:
        self._unsupported(step.keyword)

    async def assert_log(self, step: AssertLogStep) -> None:
        self._unsupported(step.keyword)

    async def assert_no_log(self, step: AssertNoLogStep) -> None:
        self._unsupported(step.keyword)

    async def wait_for_stable_equipment(
        self, step: WaitForStableEquipmentStep
    ) -> None:
        self._unsupported(step.keyword)

    async def restore_original_state(self, step: RestoreOriginalStateStep) -> None:
        self._unsupported(step.keyword)

    async def verify_equipment_status(
        self, step: VerifyEquipmentStatusStep
    ) -> None:
        self._unsupported(step.keyword)

    async def exercise_discovered_devices(
        self, step: ExerciseDiscoveredDevicesStep
    ) -> None:
        self._unsupported(step.keyword)

    async def observe_sleep_cycle(self, step: ObserveSleepCycleStep) -> None:
        self._unsupported(step.keyword)

    async def exercise_status_retry(self, step: ExerciseStatusRetryStep) -> None:
        self._unsupported(step.keyword)

    async def exercise_probe_transition(
        self, step: ExerciseProbeTransitionStep
    ) -> None:
        self._unsupported(step.keyword)

    @staticmethod
    def _unsupported(keyword: str) -> None:
        raise TestcaseExecutionFailure(
            f"keyword {keyword!r} is unavailable in this runtime"
        )


@dataclass(frozen=True)
class StepExecution:
    section: str
    index: int
    keyword: str
    duration_seconds: float


@dataclass(frozen=True)
class StepProgress:
    testcase_id: str
    section: str
    index: int
    keyword: str
    state: Literal["running", "passed", "failed"]
    duration_seconds: float | None = None
    error: str | None = None


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
        progress: Callable[[str], None] | None = None,
        observer: Callable[[StepProgress], None] | None = None,
    ) -> None:
        self._keywords = keywords
        self._clock = clock
        self._progress = progress or (lambda message: None)
        self._observer = observer or (lambda event: None)

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
        label = f"{testcase_id} {section}[{index}] {step.keyword}"
        self._progress(f"[ RUN  ] {label}")
        self._observer(
            StepProgress(
                testcase_id,
                section,
                index,
                step.keyword,
                "running",
            )
        )
        try:
            await self._dispatch(step)
        except BaseException as error:
            duration = self._clock() - started
            self._progress(
                f"[ FAIL ] {label} completed in {duration:.3f}s — "
                f"{type(error).__name__}: {error}"
            )
            self._observer(
                StepProgress(
                    testcase_id,
                    section,
                    index,
                    step.keyword,
                    "failed",
                    duration,
                    f"{type(error).__name__}: {error}",
                )
            )
            if isinstance(
                error,
                (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
            ):
                raise
            raise TestcaseExecutionFailure(
                f"{testcase_id}.{section}[{index}].{step.keyword}: {error}"
            ) from error
        duration = self._clock() - started
        self._progress(f"[ PASS ] {label} completed in {duration:.3f}s")
        self._observer(
            StepProgress(
                testcase_id,
                section,
                index,
                step.keyword,
                "passed",
                duration,
            )
        )
        return StepExecution(
            section=section,
            index=index,
            keyword=step.keyword,
            duration_seconds=duration,
        )

    async def _dispatch(self, step: TestcaseStep) -> None:
        if isinstance(step, WaitForStep):
            await self._keywords.wait_for(step)
        elif isinstance(step, ReturnPdaHomeStep):
            await self._keywords.return_pda_home(step)
        elif isinstance(step, SetPowerCenterModeStep):
            await self._keywords.set_power_center_mode(step)
        elif isinstance(step, SetPowerCenterTemperatureStep):
            await self._keywords.set_power_center_temperature(step)
        elif isinstance(step, SerialSendStep):
            await self._keywords.serial_send(step)
        elif isinstance(step, ExpectSerialStep):
            await self._keywords.expect_serial(step)
        elif isinstance(step, HttpRequestStep):
            await self._keywords.http_request(step)
        elif isinstance(step, WaitHttpJsonStep):
            await self._keywords.wait_http_json(step)
        elif isinstance(step, ExpectPanelCommandStep):
            await self._keywords.expect_panel_command(step)
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
        elif isinstance(step, AssertDeviceValueStep):
            await self._keywords.assert_device_value(step)
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
