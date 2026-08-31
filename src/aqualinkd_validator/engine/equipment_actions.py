from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..domain import DeviceState, EquipmentSnapshot, EquipmentStateError
from ..interfaces import AqualinkApi, EventTimeline, OrderedLogEvents
from ..protocols.pda import PdaProgrammerObserver
from .restoration import RestorationSession

StableSnapshotWaiter = Callable[
    [str, str, EquipmentSnapshot, float],
    Awaitable[EquipmentSnapshot],
]
MeasurementSink = Callable[[dict[str, Any]], None]
SkipSink = Callable[[str, str], None]


class EquipmentActionFailure(RuntimeError):
    """Raised when equipment action state cannot be validated."""


@dataclass(frozen=True)
class EquipmentActionTimeouts:
    activation_seconds: float
    completion_seconds: float
    convergence_seconds: float
    stabilization_seconds: float


@dataclass(frozen=True)
class ProgrammerMarkers:
    task_name: str
    active: str | tuple[str, ...]
    completed: str | tuple[str, ...]


class EquipmentActions:
    """Executes measured equipment mutations through typed dependencies."""

    def __init__(
        self,
        *,
        api: AqualinkApi,
        events: OrderedLogEvents,
        timeline: EventTimeline,
        programmer: PdaProgrammerObserver,
        restoration: RestorationSession,
        timeouts: EquipmentActionTimeouts,
        wait_for_stable: StableSnapshotWaiter,
        record_measurement: MeasurementSink,
        record_skip: SkipSink,
        poll_seconds: float = 0.25,
    ) -> None:
        self._api = api
        self._events = events
        self._timeline = timeline
        self._programmer = programmer
        self._restoration = restoration
        self._timeouts = timeouts
        self._wait_for_stable = wait_for_stable
        self._record_measurement = record_measurement
        self._record_skip = record_skip
        self._poll_seconds = poll_seconds

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
    ) -> None:
        self._restoration.touch_device(identifier)
        if not phase.startswith("restoration."):
            self._restoration.forget_requested_state(identifier)
        snapshot = await self._api.devices()
        device = self._require_device(snapshot, identifier)
        if self._device_transitioning(device):
            snapshot = await self._wait_for_stable(
                identifier,
                f"{phase}.{identifier}.precondition",
                snapshot,
                self._timeouts.stabilization_seconds,
            )
            device = self._require_device(snapshot, identifier)

        requested_state = device.requested_state_label(enabled)
        if self._device_enabled(device) == enabled:
            self._record_skip(
                f"{phase}.{identifier}.{requested_state}",
                "Device is already in the requested state",
            )
            return

        cursor = self._events.cursor
        started = self._timeline.offset_ns()
        await self._timeline.write(
            "scenario_action_started",
            phase=phase,
            action="set_device",
            target=identifier,
            value=enabled,
        )
        await self._api.set_device(identifier, enabled)
        acknowledged = self._timeline.offset_ns()
        await self._timeline.write(
            "scenario_http_acknowledged",
            phase=phase,
            action="set_device",
            target=identifier,
            value=enabled,
            requested_offset_ns=started,
        )
        active = await self._programmer.wait_for_active(
            self._events,
            self._timeline,
            task_name=markers.task_name,
            marker=markers.active,
            after=cursor,
            requested_offset_ns=started,
            timeout_seconds=(
                activation_timeout_seconds
                if activation_timeout_seconds is not None
                else self._timeouts.activation_seconds
            ),
        )
        completed = await self._programmer.wait_for_completion(
            self._events,
            self._timeline,
            task_name=markers.task_name,
            marker=markers.completed,
            active=active,
            timeout_seconds=(
                completion_timeout_seconds
                if completion_timeout_seconds is not None
                else self._timeouts.completion_seconds
            ),
        )
        convergence_timeout = (
            convergence_timeout_seconds
            if convergence_timeout_seconds is not None
            else self._timeouts.convergence_seconds
        )
        print(
            f"[ WAIT ] {identifier}: waiting for API state {requested_state} "
            f"(timeout {convergence_timeout:g}s)",
            flush=True,
        )
        observed: int | None = None
        try:
            observed = await self._programmer.wait_for_state_or_error(
                self._events,
                task_name=markers.task_name,
                after=active.sequence,
                state_wait=self._wait_for_device_state(
                    identifier,
                    enabled,
                    timeout_seconds=convergence_timeout,
                ),
                timeout_seconds=convergence_timeout,
            )
        finally:
            self._record_action_measurement(
                name=f"{phase}.{identifier}.{requested_state}",
                category="device",
                phase=phase,
                target=identifier,
                requested_value=enabled,
                started=started,
                acknowledged=acknowledged,
                active=active.offset_ns,
                completed=completed.offset_ns,
                observed=observed,
            )
        state_seconds = (observed - completed.offset_ns) / 1_000_000_000
        print(
            f"[STATE ] {identifier} became {requested_state} "
            f"{state_seconds:.3f}s after programmer completion",
            flush=True,
        )
        await self._timeline.write(
            "scenario_action_finished",
            phase=phase,
            action="set_device",
            target=identifier,
            value=enabled,
            status="passed",
        )

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
    ) -> None:
        self._restoration.touch_setpoint(identifier)
        cursor = self._events.cursor
        started = self._timeline.offset_ns()
        await self._timeline.write(
            "scenario_action_started",
            phase=phase,
            action="set_setpoint",
            target=identifier,
            value=value,
        )
        await self._api.set_setpoint(identifier, value)
        acknowledged = self._timeline.offset_ns()
        await self._timeline.write(
            "scenario_http_acknowledged",
            phase=phase,
            action="set_setpoint",
            target=identifier,
            value=value,
            requested_offset_ns=started,
        )
        active = await self._programmer.wait_for_active(
            self._events,
            self._timeline,
            task_name=markers.task_name,
            marker=markers.active,
            after=cursor,
            requested_offset_ns=started,
            timeout_seconds=(
                activation_timeout_seconds
                if activation_timeout_seconds is not None
                else self._timeouts.activation_seconds
            ),
        )
        completed = await self._programmer.wait_for_completion(
            self._events,
            self._timeline,
            task_name=markers.task_name,
            marker=markers.completed,
            active=active,
            timeout_seconds=(
                completion_timeout_seconds
                if completion_timeout_seconds is not None
                else self._timeouts.completion_seconds
            ),
        )
        convergence_timeout = (
            convergence_timeout_seconds
            if convergence_timeout_seconds is not None
            else self._timeouts.convergence_seconds
        )
        print(
            f"[ WAIT ] {identifier}: waiting for API setpoint {value} "
            f"(timeout {convergence_timeout:g}s)",
            flush=True,
        )
        observed: int | None = None
        try:
            observed = await self._programmer.wait_for_state_or_error(
                self._events,
                task_name=markers.task_name,
                after=active.sequence,
                state_wait=self._wait_for_setpoint(
                    identifier,
                    value,
                    timeout_seconds=convergence_timeout,
                ),
                timeout_seconds=convergence_timeout,
            )
        finally:
            self._record_action_measurement(
                name=f"{phase}.{value}",
                category=category,
                phase=phase,
                target=identifier,
                requested_value=value,
                started=started,
                acknowledged=acknowledged,
                active=active.offset_ns,
                completed=completed.offset_ns,
                observed=observed,
            )
        state_seconds = (observed - completed.offset_ns) / 1_000_000_000
        print(
            f"[STATE ] {identifier} reached setpoint {value} "
            f"{state_seconds:.3f}s after programmer completion",
            flush=True,
        )

    async def wait_for_device_state(
        self,
        identifier: str,
        enabled: bool,
        *,
        timeout_seconds: float,
    ) -> int:
        return await self._wait_for_device_state(
            identifier,
            enabled,
            timeout_seconds=timeout_seconds,
        )

    async def wait_for_device_value(
        self,
        identifier: str,
        value: int,
        *,
        timeout_seconds: float,
    ) -> int:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_value: object = None
        while asyncio.get_running_loop().time() < deadline:
            device = self._require_device(await self._api.devices(), identifier)
            last_value = device.raw.get("value")
            try:
                if last_value is not None and round(float(last_value)) == value:
                    return self._timeline.offset_ns()
            except (TypeError, ValueError):
                pass
            await asyncio.sleep(self._poll_seconds)
        raise EquipmentActionFailure(
            f"{identifier} value did not become {value} within "
            f"{timeout_seconds:g}s; last value was {last_value!r}"
        )

    async def _wait_for_device_state(
        self,
        identifier: str,
        enabled: bool,
        *,
        timeout_seconds: float,
    ) -> int:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        device: DeviceState | None = None
        while asyncio.get_running_loop().time() < deadline:
            device = self._require_device(await self._api.devices(), identifier)
            if (
                not self._device_transitioning(device)
                and self._device_enabled(device) == enabled
            ):
                return self._timeline.offset_ns()
            await asyncio.sleep(self._poll_seconds)
        requested = (
            device.requested_state_label(enabled) if device is not None else enabled
        )
        raise EquipmentActionFailure(
            f"{identifier} did not become {requested} within {timeout_seconds:g}s"
        )

    async def _wait_for_setpoint(
        self,
        identifier: str,
        expected: int,
        *,
        timeout_seconds: float,
    ) -> int:
        timeout = timeout_seconds
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            device = self._require_device(await self._api.devices(), identifier)
            if self._device_setpoint(device) == expected:
                return self._timeline.offset_ns()
            await asyncio.sleep(self._poll_seconds)
        raise EquipmentActionFailure(
            f"{identifier} setpoint did not become {expected} within {timeout:g}s"
        )

    def _record_action_measurement(
        self,
        *,
        name: str,
        category: str,
        phase: str,
        target: str,
        requested_value: int | bool,
        started: int,
        acknowledged: int,
        active: int,
        completed: int,
        observed: int | None,
    ) -> None:
        final = observed if observed is not None else completed
        self._record_measurement(
            {
                "name": name,
                "category": category,
                "status": "passed" if observed is not None else "failed",
                "phase": phase,
                "target": target,
                "requested_value": requested_value,
                "start_offset_ns": started,
                "api_ack_offset_ns": acknowledged,
                "task_active_offset_ns": active,
                "log_completion_offset_ns": completed,
                "state_observed_offset_ns": observed,
                "completed_offset_ns": final,
                "duration_ms": round((final - started) / 1_000_000, 3),
                "api_ack_ms": round((acknowledged - started) / 1_000_000, 3),
                "activation_ms": round((active - started) / 1_000_000, 3),
                "programmer_duration_ms": round(
                    (completed - active) / 1_000_000,
                    3,
                ),
                "state_convergence_ms": (
                    round((observed - completed) / 1_000_000, 3)
                    if observed is not None
                    else None
                ),
            }
        )

    @staticmethod
    def _require_device(
        snapshot: EquipmentSnapshot,
        identifier: str,
    ) -> DeviceState:
        try:
            return snapshot.devices[identifier]
        except KeyError as error:
            raise EquipmentActionFailure(
                f"Required device {identifier} is absent from /api/devices"
            ) from error

    @staticmethod
    def _device_enabled(device: DeviceState) -> bool:
        try:
            return device.enabled
        except EquipmentStateError as error:
            raise EquipmentActionFailure(str(error)) from error

    @staticmethod
    def _device_transitioning(device: DeviceState) -> bool:
        try:
            return device.transitioning
        except EquipmentStateError as error:
            raise EquipmentActionFailure(str(error)) from error

    @staticmethod
    def _device_setpoint(device: DeviceState) -> int | None:
        try:
            return device.setpoint
        except EquipmentStateError as error:
            raise EquipmentActionFailure(str(error)) from error
