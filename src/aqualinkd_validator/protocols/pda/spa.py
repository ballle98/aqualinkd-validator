from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ...domain import DeviceState, EquipmentSnapshot
from ...interfaces import AqualinkApi

FILTER_PUMP = "Filter_Pump"
SPA_HEATER = "Spa_Heater"
SPA_MODE_IDENTIFIERS = ("Spa", "Spa_Mode")

DeviceSetter = Callable[[str, bool, str, float], Awaitable[None]]
SetpointSetter = Callable[[str, int, str], Awaitable[None]]
SkipRecorder = Callable[[str, str], None]


class MeasurementRecorder(Protocol):
    def __call__(
        self,
        *,
        name: str,
        category: str,
        phase: str,
        target: str,
        requested_value: Any,
        start_offset_ns: int,
        api_ack_offset_ns: int | None,
        log_completion_offset_ns: int | None,
        state_observed_offset_ns: int | None,
        task_active_offset_ns: int | None = None,
        status: str = "passed",
    ) -> None: ...


class SpaExerciseFailure(RuntimeError):
    """Raised when the spa heating lifecycle cannot be completed safely."""


@dataclass(frozen=True)
class SpaExerciseConfig:
    fill_seconds: float
    active_timeout_seconds: float
    transition_timeout_seconds: float
    poll_seconds: float = 0.25


class PdaSpaExercise:
    """Owns pool-specific hydraulic, heat-demand, and cooldown policy."""

    def __init__(
        self,
        *,
        api: AqualinkApi,
        config: SpaExerciseConfig,
        set_device: DeviceSetter,
        set_setpoint: SetpointSetter,
        record_measurement: MeasurementRecorder,
        record_skip: SkipRecorder,
        offset_ns: Callable[[], int],
    ) -> None:
        self._api = api
        self._config = config
        self._set_device = set_device
        self._set_setpoint = set_setpoint
        self._record_measurement = record_measurement
        self._record_skip = record_skip
        self._offset_ns = offset_ns

    async def run(self, initial: EquipmentSnapshot) -> None:
        spa_mode = next(
            (
                identifier
                for identifier in SPA_MODE_IDENTIFIERS
                if identifier in initial.devices
            ),
            None,
        )
        missing = [
            identifier
            for identifier in (FILTER_PUMP, SPA_HEATER)
            if identifier not in initial.devices
        ]
        if spa_mode is None:
            missing.append("Spa or Spa_Mode")
        if missing:
            self._record_skip(
                "spa.heating",
                "Panel has no complete pool/spa heating path: " + ", ".join(missing),
            )
            return
        assert spa_mode is not None

        if (await self._api.devices()).devices[SPA_HEATER].enabled:
            await self._change_device(SPA_HEATER, False, "spa.precondition")
        await self._change_device(spa_mode, False, "spa.precondition")
        await self._change_device(FILTER_PUMP, True, "spa.fill")
        await self._fill(spa_mode)
        await self._change_device(spa_mode, True, "spa.mode")

        snapshot, water_temperature = await self._wait_for_spa_temperature()
        heater = self._device(snapshot, SPA_HEATER)
        original_setpoint = self._device(initial, SPA_HEATER).setpoint
        if original_setpoint is None:
            raise SpaExerciseFailure("Spa_Heater has no numeric setpoint")
        minimum, maximum = self._temperature_bounds(snapshot.temp_units)
        target_setpoint = min(
            maximum,
            max(minimum, original_setpoint, math.floor(water_temperature) + 2),
        )
        if target_setpoint <= water_temperature:
            raise SpaExerciseFailure(
                f"Cannot safely force spa heating: water is {water_temperature:g} "
                f"and maximum setpoint is {maximum}"
            )
        if target_setpoint != heater.setpoint:
            await self._set_setpoint(
                SPA_HEATER,
                target_setpoint,
                "spa.heater.setpoint",
            )

        await self._change_device(SPA_HEATER, True, "spa.heater")
        await self._wait_for_active_heat()
        await self._change_device(SPA_HEATER, False, "spa.cooldown")
        await self._change_device(spa_mode, False, "spa.cooldown")

    async def _wait_for_spa_temperature(self) -> tuple[EquipmentSnapshot, float]:
        print("[ WAIT ] Spa mode: waiting for a valid water temperature", flush=True)
        deadline = time.monotonic() + self._config.active_timeout_seconds
        last_value: object = None
        while True:
            snapshot = await self._api.devices()
            heater = self._device(snapshot, SPA_HEATER)
            last_value = heater.raw.get("value")
            temperature = self._numeric_temperature(last_value)
            plausible = (
                -40 <= temperature <= 130
                if snapshot.temp_units.casefold() == "f"
                else -40 <= temperature <= 55
                if snapshot.temp_units.casefold() == "c"
                else False
            )
            if plausible:
                print(
                    f"[STATE ] Spa water temperature is {temperature:g}"
                    f"°{snapshot.temp_units.upper()}",
                    flush=True,
                )
                return snapshot, temperature
            if time.monotonic() >= deadline:
                raise SpaExerciseFailure(
                    "Spa temperature did not become available within "
                    f"{self._config.active_timeout_seconds:g}s "
                    f"(last value={last_value!r})"
                )
            await asyncio.sleep(self._config.poll_seconds)

    async def _fill(self, spa_mode: str) -> None:
        started = self._offset_ns()
        print(
            f"[ WAIT ] Spa fill: circulating in pool mode for "
            f"{self._config.fill_seconds / 60:g} minutes",
            flush=True,
        )
        await asyncio.sleep(self._config.fill_seconds)
        finished = self._offset_ns()
        print("[STATE ] Spa fill interval complete", flush=True)
        self._record_measurement(
            name="spa.pool_mode_fill",
            category="spa_hydraulics",
            phase="spa.fill",
            target=spa_mode,
            requested_value=False,
            start_offset_ns=started,
            api_ack_offset_ns=None,
            log_completion_offset_ns=None,
            state_observed_offset_ns=finished,
        )

    async def _wait_for_active_heat(self) -> None:
        started = self._offset_ns()
        deadline = time.monotonic() + self._config.active_timeout_seconds
        while True:
            heater = self._device(await self._api.devices(), SPA_HEATER)
            if heater.active:
                break
            if time.monotonic() >= deadline:
                raise SpaExerciseFailure(
                    f"{SPA_HEATER} did not become actively heating within "
                    f"{self._config.active_timeout_seconds:g}s "
                    f"(status={heater.status}, int_status={heater.int_status})"
                )
            await asyncio.sleep(self._config.poll_seconds)
        finished = self._offset_ns()
        print("[STATE ] Spa heater is actively heating", flush=True)
        self._record_measurement(
            name="spa.heater.active",
            category="spa_heating",
            phase="spa.heater",
            target=SPA_HEATER,
            requested_value=True,
            start_offset_ns=started,
            api_ack_offset_ns=None,
            log_completion_offset_ns=None,
            state_observed_offset_ns=finished,
        )

    async def _change_device(self, identifier: str, enabled: bool, phase: str) -> None:
        await self._set_device(
            identifier,
            enabled,
            phase,
            self._config.transition_timeout_seconds,
        )

    @staticmethod
    def _device(snapshot: EquipmentSnapshot, identifier: str) -> DeviceState:
        try:
            return snapshot.devices[identifier]
        except KeyError as error:
            raise SpaExerciseFailure(
                f"Required device {identifier} is absent"
            ) from error

    @staticmethod
    def _temperature_bounds(units: str) -> tuple[int, int]:
        try:
            return {"f": (36, 104), "c": (0, 40)}[units.casefold()]
        except KeyError as error:
            raise SpaExerciseFailure(f"Unknown temperature units {units!r}") from error

    @staticmethod
    def _numeric_temperature(value: object) -> float:
        try:
            temperature = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return math.nan
        if not math.isfinite(temperature):
            return math.nan
        return temperature
