from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ...domain import (
    DeviceState,
    EquipmentSnapshot,
    EquipmentStateError,
    device_state_details,
)
from ...interfaces import AqualinkApi, OrderedLogEvents

POOL_HEATER = "Pool_Heater"
SPA_HEATER = "Spa_Heater"
HEATER_CONTROLS = frozenset({POOL_HEATER, SPA_HEATER})

SetDevice = Callable[[str, bool, str, float], Awaitable[None]]
SetSetpoint = Callable[[str, int, str], Awaitable[None]]
StableSnapshotWaiter = Callable[
    [Sequence[str], str, float],
    Awaitable[EquipmentSnapshot],
]
SkipSink = Callable[[str, str], None]
ProgressSink = Callable[[str], None]


class PdaEquipmentSetupFailure(RuntimeError):
    """Raised when safe EQUIPMENT STATUS setup cannot be proven."""


@dataclass(frozen=True)
class PdaEquipmentSetupConfig:
    status_timeout_seconds: float
    restoration_timeout_seconds: float
    poll_seconds: float = 0.25


@dataclass(frozen=True)
class PdaEquipmentSetupResult:
    controls: tuple[str, ...]
    states: dict[str, dict[str, Any]]


class PdaEquipmentStatusSetup:
    """Maximize status-menu occupancy without demanding active heat."""

    def __init__(
        self,
        *,
        api: AqualinkApi,
        events: OrderedLogEvents,
        config: PdaEquipmentSetupConfig,
        set_device: SetDevice,
        set_setpoint: SetSetpoint,
        wait_for_stable: StableSnapshotWaiter,
        record_skip: SkipSink,
        progress: ProgressSink,
    ) -> None:
        self._api = api
        self._events = events
        self._config = config
        self._set_device = set_device
        self._set_setpoint = set_setpoint
        self._wait_for_stable = wait_for_stable
        self._record_skip = record_skip
        self._progress = progress

    async def prepare(
        self,
        initial_snapshot: EquipmentSnapshot,
        candidates: Sequence[str],
    ) -> PdaEquipmentSetupResult:
        controls = await self._make_heaters_nonheating(
            initial_snapshot,
            candidates,
        )
        if not controls:
            return PdaEquipmentSetupResult((), {})

        await self._wait_for_stable(
            controls,
            "devices.status_menu.precondition",
            self._config.status_timeout_seconds,
        )
        heaters = [item for item in controls if item in HEATER_CONTROLS]
        non_heaters = [item for item in controls if item not in HEATER_CONTROLS]
        circulation_cursor = self._events.cursor
        for identifier in non_heaters:
            await self._set_device(
                identifier,
                True,
                "devices.status_menu.setup",
                self._config.status_timeout_seconds,
            )

        eligible_heaters: list[str] = []
        for identifier in heaters:
            if identifier == POOL_HEATER and not await self._pool_is_above_minimum(
                after=circulation_cursor,
                minimum=self.minimum_setpoint(initial_snapshot.temp_units),
            ):
                self._record_skip(
                    f"devices.status_menu.setup.{identifier}",
                    "Pool temperature did not become available above the "
                    "minimum heater setpoint after circulation started",
                )
                continue
            await self._set_device(
                identifier,
                True,
                "devices.status_menu.setup",
                self._config.status_timeout_seconds,
            )
            eligible_heaters.append(identifier)
        controls = [
            identifier
            for identifier in controls
            if identifier not in HEATER_CONTROLS or identifier in eligible_heaters
        ]
        self._progress(
            f"[STATE ] Equipment status setup: enabled "
            f"{len(controls)} configured controls"
        )

        setup_snapshot = await self._wait_for_stable(
            controls,
            "devices.status_menu.setup_complete",
            self._config.status_timeout_seconds,
        )
        try:
            states = {
                identifier: device_state_details(
                    self._require_device(setup_snapshot, identifier)
                )
                for identifier in controls
            }
        except EquipmentStateError as error:
            raise PdaEquipmentSetupFailure(str(error)) from error
        setup_failures = [
            identifier
            for identifier, state in states.items()
            if not state["enabled"]
        ]
        if setup_failures:
            raise PdaEquipmentSetupFailure(
                "Equipment status setup did not remain enabled after "
                "transitions settled: " + ", ".join(setup_failures)
            )
        active_heaters = [
            identifier
            for identifier, state in states.items()
            if identifier in HEATER_CONTROLS and state["active"]
        ]
        if active_heaters:
            for identifier in active_heaters:
                await self._set_device(
                    identifier,
                    False,
                    "devices.status_menu.emergency_heat_disable",
                    self._config.restoration_timeout_seconds,
                )
            raise PdaEquipmentSetupFailure(
                "Non-heating EQUIPMENT STATUS setup unexpectedly activated: "
                + ", ".join(active_heaters)
            )
        return PdaEquipmentSetupResult(tuple(controls), states)

    async def _make_heaters_nonheating(
        self,
        initial_snapshot: EquipmentSnapshot,
        candidates: Sequence[str],
    ) -> list[str]:
        spa_mode_enabled = any(
            initial_snapshot.devices[identifier].enabled
            for identifier in ("Spa", "Spa_Mode")
            if identifier in initial_snapshot.devices
        )
        controls: list[str] = []
        for identifier in candidates:
            device = self._require_device(initial_snapshot, identifier)
            if device.kind != "setpoint_thermo":
                controls.append(identifier)
                continue
            if identifier not in HEATER_CONTROLS:
                self._record_skip(
                    f"devices.status_menu.setup.{identifier}",
                    "Unknown heater type cannot be made non-heating safely",
                )
                continue
            if device.active:
                self._record_skip(
                    f"devices.status_menu.setup.{identifier}",
                    "Heater was already actively heating at test start",
                )
                continue
            if spa_mode_enabled:
                self._record_skip(
                    f"devices.status_menu.setup.{identifier}",
                    "Spa mode was already enabled at test start",
                )
                continue
            try:
                minimum = self.minimum_setpoint(initial_snapshot.temp_units)
            except PdaEquipmentSetupFailure:
                self._record_skip(
                    f"devices.status_menu.setup.{identifier}",
                    "Unknown temperature units prevent a safe minimum setpoint",
                )
                continue
            if device.enabled:
                await self._set_device(
                    identifier,
                    False,
                    "devices.status_menu.safety_disable",
                    self._config.restoration_timeout_seconds,
                )
            if device.setpoint != minimum:
                await self._set_setpoint(
                    identifier,
                    minimum,
                    "devices.status_menu.safe_setpoint",
                )
            controls.append(identifier)
        return controls

    async def _pool_is_above_minimum(
        self,
        *,
        after: int,
        minimum: int,
    ) -> bool:
        timeout = self._config.status_timeout_seconds
        self._progress(
            "[ WAIT ] Pool Heater safety: waiting for a home-screen water "
            f"temperature (timeout {timeout:g}s)"
        )
        try:
            await self._events.wait_for(
                "PDA Menu Line 1 = AIR",
                after=after,
                timeout_seconds=timeout,
            )
        except TimeoutError:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            snapshot = await self._api.devices()
            heater = self._require_device(snapshot, POOL_HEATER)
            raw_temperature = heater.raw.get("value")
            try:
                temperature = (
                    float(raw_temperature)
                    if isinstance(raw_temperature, (int, float, str))
                    else None
                )
            except ValueError:
                temperature = None
            if temperature is not None and temperature > minimum:
                self._progress(
                    f"[STATE ] Pool temperature {temperature:g} is above "
                    f"the safe {minimum}° heater test setpoint"
                )
                return True
            if temperature is not None and temperature > -100:
                return False
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(self._config.poll_seconds)

    @staticmethod
    def minimum_setpoint(temp_units: str) -> int:
        minimum = {"f": 36, "c": 0}.get(temp_units.casefold())
        if minimum is None:
            raise PdaEquipmentSetupFailure(
                f"Unknown temperature units {temp_units!r}"
            )
        return minimum

    @staticmethod
    def _require_device(
        snapshot: EquipmentSnapshot,
        identifier: str,
    ) -> DeviceState:
        try:
            return snapshot.devices[identifier]
        except KeyError as error:
            raise PdaEquipmentSetupFailure(
                f"Required device {identifier} is absent from /api/devices"
            ) from error
