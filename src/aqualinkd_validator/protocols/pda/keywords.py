from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from ...domain import EquipmentSnapshot
from ...engine.equipment_actions import ProgrammerMarkers
from ...engine.restoration import RestorationSession
from ...interfaces import OrderedLogEvents
from ...testcases.model import (
    AssertDeviceStep,
    AssertLogStep,
    AssertNoLogStep,
    ExerciseDiscoveredDevicesStep,
    ExerciseHeaterStep,
    ExerciseProbeTransitionStep,
    ExerciseStatusRetryStep,
    ObserveSleepCycleStep,
    RestoreOriginalStateStep,
    SetDeviceStep,
    SetSetpointStep,
    VerifyEquipmentStatusStep,
    WaitForStableEquipmentStep,
    WaitForStep,
)

InitializePda = Callable[[], Awaitable[None]]
StableEquipmentWaiter = Callable[[tuple[str, ...], float], Awaitable[EquipmentSnapshot]]
RestoreEquipment = Callable[[float], Awaitable[None]]
ComplexPdaOperation = Callable[[], Awaitable[None]]
SkipRecorder = Callable[[str, str], None]


class PdaEquipmentActions(Protocol):
    async def set_device(
        self,
        identifier: str,
        enabled: bool,
        *,
        phase: str,
        markers: ProgrammerMarkers,
        activation_timeout_seconds: float | None = None,
        completion_timeout_seconds: float | None = None,
        convergence_timeout_seconds: float | None = None,
    ) -> None: ...

    async def set_setpoint(
        self,
        identifier: str,
        value: int,
        *,
        phase: str,
        category: str,
        markers: ProgrammerMarkers,
        activation_timeout_seconds: float | None = None,
        completion_timeout_seconds: float | None = None,
        convergence_timeout_seconds: float | None = None,
    ) -> None: ...

    async def wait_for_device_state(
        self,
        identifier: str,
        enabled: bool,
        *,
        timeout_seconds: float,
    ) -> int: ...


EquipmentActionsFactory = Callable[[], PdaEquipmentActions]


class PdaKeywordFailure(RuntimeError):
    """Raised when a declarative PDA keyword cannot be satisfied safely."""


@dataclass(frozen=True)
class PdaKeywordMarkers:
    device: ProgrammerMarkers
    setpoints: Mapping[str, ProgrammerMarkers]


class PdaTestcaseKeywords:
    """Binds schema-v1 testcase keywords to PDA runtime services."""

    def __init__(
        self,
        *,
        events: OrderedLogEvents,
        actions: EquipmentActionsFactory,
        restoration: RestorationSession,
        markers: PdaKeywordMarkers,
        initialize: InitializePda,
        wait_for_stable: StableEquipmentWaiter,
        restore: RestoreEquipment,
        verify_status: ComplexPdaOperation | None = None,
        exercise_devices: ComplexPdaOperation | None = None,
        observe_sleep: ComplexPdaOperation | None = None,
        exercise_status_retry: ComplexPdaOperation | None = None,
        exercise_probe_transition: ComplexPdaOperation | None = None,
        record_skip: SkipRecorder = lambda name, reason: None,
        phase_prefix: str = "testcase",
    ) -> None:
        self._events = events
        self._actions = actions
        self._restoration = restoration
        self._markers = markers
        self._initialize = initialize
        self._wait_for_stable = wait_for_stable
        self._restore = restore
        self._verify_status = verify_status
        self._exercise_devices = exercise_devices
        self._observe_sleep = observe_sleep
        self._exercise_status_retry = exercise_status_retry
        self._exercise_probe_transition = exercise_probe_transition
        self._record_skip = record_skip
        self._phase_prefix = phase_prefix
        self._initialized = restoration.initial_snapshot is not None
        self._requested_states: dict[str, bool] = {}
        self._log_cursor = events.cursor

    async def wait_for(self, step: WaitForStep) -> None:
        if step.condition != "pda.initialized":
            raise PdaKeywordFailure(f"unsupported PDA condition {step.condition!r}")
        if self._initialized:
            return
        try:
            async with asyncio.timeout(step.timeout_seconds):
                await self._initialize()
        except TimeoutError as error:
            raise PdaKeywordFailure(
                f"PDA initialization did not complete within {step.timeout_seconds:g}s"
            ) from error
        if self._restoration.initial_snapshot is None:
            raise PdaKeywordFailure(
                "PDA initialization did not capture initial equipment state"
            )
        self._initialized = True

    async def set_device(self, step: SetDeviceStep) -> None:
        self._require_initialized()
        enabled = self._resolve_device_state(step.identifier, step.state)
        await self._actions().set_device(
            step.identifier,
            enabled,
            phase=f"{self._phase_prefix}.set_device",
            markers=self._markers.device,
            activation_timeout_seconds=step.activation_timeout_seconds,
            completion_timeout_seconds=step.completion_timeout_seconds,
            convergence_timeout_seconds=step.convergence_timeout_seconds,
        )
        self._requested_states[step.identifier] = enabled

    async def set_setpoint(self, step: SetSetpointStep) -> None:
        self._require_initialized()
        try:
            markers = self._markers.setpoints[step.identifier]
        except KeyError as error:
            raise PdaKeywordFailure(
                f"no PDA setpoint programmer markers for {step.identifier}"
            ) from error
        value = (
            self._initial_setpoint(step.identifier)
            if step.value == "original"
            else step.value
        )
        await self._actions().set_setpoint(
            step.identifier,
            value,
            phase=f"{self._phase_prefix}.set_setpoint.{step.identifier}",
            category="testcase_setpoint",
            markers=markers,
            activation_timeout_seconds=step.activation_timeout_seconds,
            completion_timeout_seconds=step.completion_timeout_seconds,
            convergence_timeout_seconds=step.convergence_timeout_seconds,
        )

    async def exercise_heater(self, step: ExerciseHeaterStep) -> None:
        self._require_initialized()
        snapshot = self._restoration.initial_snapshot
        assert snapshot is not None
        heater = snapshot.devices.get(step.identifier)
        if heater is None or heater.kind != "setpoint_thermo":
            self._skip_optional(
                step,
                f"{step.identifier} is not a setpoint heater",
            )
            return
        original_setpoint = heater.setpoint
        if original_setpoint is None:
            self._skip_optional(step, f"{step.identifier} has no setpoint")
            return
        bounds = {"f": (36, 104), "c": (0, 40)}.get(snapshot.temp_units)
        if bounds is None:
            self._skip_optional(
                step,
                f"unknown temperature units {snapshot.temp_units!r}",
            )
            return
        try:
            setpoint_markers = self._markers.setpoints[step.identifier]
        except KeyError as error:
            raise PdaKeywordFailure(
                f"no PDA setpoint programmer markers for {step.identifier}"
            ) from error

        minimum, maximum = bounds
        test_values = [
            value
            for value in (
                max(minimum, original_setpoint - 1),
                min(maximum, original_setpoint + 1),
            )
            if value != original_setpoint
        ]
        for value in (*test_values, original_setpoint):
            await self._actions().set_setpoint(
                step.identifier,
                value,
                phase=f"{self._phase_prefix}.exercise_heater.setpoint",
                category="heater_setpoint",
                markers=setpoint_markers,
                activation_timeout_seconds=step.activation_timeout_seconds,
                completion_timeout_seconds=step.completion_timeout_seconds,
                convergence_timeout_seconds=step.convergence_timeout_seconds,
            )

        original_enabled = heater.enabled
        for enabled in (not original_enabled, original_enabled):
            await self._actions().set_device(
                step.identifier,
                enabled,
                phase=f"{self._phase_prefix}.exercise_heater.state",
                markers=self._markers.device,
                activation_timeout_seconds=step.activation_timeout_seconds,
                completion_timeout_seconds=step.completion_timeout_seconds,
                convergence_timeout_seconds=step.convergence_timeout_seconds,
            )
            self._requested_states[step.identifier] = enabled

    async def assert_device(self, step: AssertDeviceStep) -> None:
        self._require_initialized()
        enabled = self._resolve_device_state(step.identifier, step.state)
        await self._actions().wait_for_device_state(
            step.identifier,
            enabled,
            timeout_seconds=step.timeout_seconds,
        )

    async def assert_log(self, step: AssertLogStep) -> None:
        event = await self._events.wait_for(
            step.contains,
            after=self._log_cursor,
            timeout_seconds=step.timeout_seconds,
        )
        self._log_cursor = event.sequence

    async def assert_no_log(self, step: AssertNoLogStep) -> None:
        def matches(event: object) -> bool:
            text = getattr(event, "text", "")
            if not isinstance(text, str):
                return False
            if step.contains is not None and step.contains not in text:
                return False
            if step.level is None:
                return True
            return f"{step.level}:" in text.casefold()

        description = " and ".join(
            item
            for item in (
                f"contains {step.contains!r}" if step.contains else None,
                f"level {step.level}" if step.level else None,
            )
            if item is not None
        )
        try:
            event = await self._events.wait_for_match(
                matches,
                description=description,
                after=self._log_cursor,
                timeout_seconds=step.duration_seconds,
            )
        except TimeoutError:
            self._log_cursor = self._events.cursor
            return
        self._log_cursor = event.sequence
        raise PdaKeywordFailure(
            f"unexpected log matching {description}: {event.text.strip()}"
        )

    async def wait_for_stable_equipment(
        self,
        step: WaitForStableEquipmentStep,
    ) -> None:
        self._require_initialized()
        await self._wait_for_stable(step.identifiers, step.timeout_seconds)

    async def restore_original_state(
        self,
        step: RestoreOriginalStateStep,
    ) -> None:
        self._require_initialized()
        try:
            async with asyncio.timeout(step.timeout_seconds):
                await self._restore(step.timeout_seconds)
        except TimeoutError as error:
            raise PdaKeywordFailure(
                f"equipment restoration did not complete within "
                f"{step.timeout_seconds:g}s"
            ) from error

    async def verify_equipment_status(self, step: VerifyEquipmentStatusStep) -> None:
        self._require_initialized()
        if self._verify_status is None:
            raise PdaKeywordFailure("equipment-status verification is unavailable")
        async with asyncio.timeout(step.timeout_seconds):
            await self._verify_status()

    async def exercise_discovered_devices(
        self, step: ExerciseDiscoveredDevicesStep
    ) -> None:
        self._require_initialized()
        if self._exercise_devices is None:
            raise PdaKeywordFailure("discovered-device exercise is unavailable")
        async with asyncio.timeout(step.timeout_seconds):
            await self._exercise_devices()

    async def observe_sleep_cycle(self, step: ObserveSleepCycleStep) -> None:
        self._require_initialized()
        if self._observe_sleep is None:
            raise PdaKeywordFailure("sleep-cycle observation is unavailable")
        async with asyncio.timeout(step.timeout_seconds):
            await self._observe_sleep()

    async def exercise_status_retry(self, step: ExerciseStatusRetryStep) -> None:
        self._require_initialized()
        if self._exercise_status_retry is None:
            raise PdaKeywordFailure("STATUS-retry exercise is unavailable")
        async with asyncio.timeout(step.timeout_seconds):
            await self._exercise_status_retry()

    async def exercise_probe_transition(
        self, step: ExerciseProbeTransitionStep
    ) -> None:
        self._require_initialized()
        if self._exercise_probe_transition is None:
            raise PdaKeywordFailure("probe-transition exercise is unavailable")
        async with asyncio.timeout(step.timeout_seconds):
            await self._exercise_probe_transition()

    def _resolve_device_state(self, identifier: str, state: str) -> bool:
        original = self._restoration.initial_device_enabled(identifier)
        if state == "on":
            return True
        if state == "off":
            return False
        if state == "original":
            return original
        if state == "opposite-of-original":
            return not original
        if state == "requested":
            try:
                return self._requested_states[identifier]
            except KeyError as error:
                raise PdaKeywordFailure(
                    f"no requested state has been recorded for {identifier}"
                ) from error
        raise PdaKeywordFailure(f"unsupported device state {state!r}")

    def _initial_setpoint(self, identifier: str) -> int:
        snapshot = self._restoration.initial_snapshot
        if snapshot is None:
            raise PdaKeywordFailure("initial equipment state is unavailable")
        try:
            value = snapshot.devices[identifier].setpoint
        except KeyError as error:
            raise PdaKeywordFailure(
                f"initial equipment state has no {identifier}"
            ) from error
        if value is None:
            raise PdaKeywordFailure(
                f"initial equipment state has no setpoint for {identifier}"
            )
        return value

    def _require_initialized(self) -> None:
        if not self._initialized or self._restoration.initial_snapshot is None:
            raise PdaKeywordFailure(
                "pda.initialized must be awaited before equipment keywords"
            )

    def _skip_optional(self, step: ExerciseHeaterStep, reason: str) -> None:
        if not step.optional:
            raise PdaKeywordFailure(reason)
        self._record_skip(
            f"{self._phase_prefix}.exercise_heater.{step.identifier}",
            reason,
        )
