from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ...domain import EquipmentSnapshot
from ...engine import (
    EquipmentActionFailure,
    EquipmentActions,
    EquipmentActionTimeouts,
    EquipmentStabilityConfig,
    EquipmentStabilityFailure,
    EquipmentStabilityService,
    ProgrammerMarkers,
    RestorationSession,
)
from ...interfaces import AqualinkApi, EventTimeline, OrderedLogEvents
from .programmer import PdaProgrammerFailure, PdaProgrammerObserver


class PdaEquipmentControlFailure(RuntimeError):
    """Raised when shared PDA equipment control cannot converge safely."""


@dataclass(frozen=True)
class PdaEquipmentControlConfig:
    activation_timeout_seconds: float
    action_timeout_seconds: float
    state_timeout_seconds: float
    restoration_timeout_seconds: float
    poll_seconds: float = 0.25
    stable_seconds: float = 0.5


class PdaEquipmentController:
    """Bind common equipment actions and stability to one PDA run."""

    def __init__(
        self,
        *,
        api: AqualinkApi,
        events: OrderedLogEvents,
        timeline: EventTimeline,
        programmer: PdaProgrammerObserver,
        restoration: RestorationSession,
        config: PdaEquipmentControlConfig,
        record_measurement: Callable[[dict[str, Any]], None],
        record_observation: Callable[[dict[str, Any]], None],
        record_skip: Callable[[str, str], None],
        progress: Callable[[str], None],
    ) -> None:
        self._api = api
        self._events = events
        self._timeline = timeline
        self._programmer = programmer
        self._restoration = restoration
        self._config = config
        self._record_measurement = record_measurement
        self._record_observation = record_observation
        self._record_skip = record_skip
        self._progress = progress

    def actions(self) -> EquipmentActions:
        async def wait_for_stable(
            identifier: str,
            phase: str,
            initial: EquipmentSnapshot,
            timeout_seconds: float,
        ) -> EquipmentSnapshot:
            return await self.wait_for_stable(
                [identifier],
                phase=phase,
                timeout_seconds=timeout_seconds,
                initial_snapshot=initial,
            )

        return EquipmentActions(
            api=self._api,
            events=self._events,
            timeline=self._timeline,
            programmer=self._programmer,
            restoration=self._restoration,
            timeouts=EquipmentActionTimeouts(
                activation_seconds=self._config.activation_timeout_seconds,
                completion_seconds=self._config.action_timeout_seconds,
                convergence_seconds=self._config.state_timeout_seconds,
                stabilization_seconds=self._config.restoration_timeout_seconds,
            ),
            wait_for_stable=wait_for_stable,
            record_measurement=self._record_measurement,
            record_skip=self._record_skip,
        )

    async def wait_for_stable(
        self,
        identifiers: Sequence[str],
        *,
        phase: str,
        timeout_seconds: float,
        initial_snapshot: EquipmentSnapshot | None = None,
    ) -> EquipmentSnapshot:
        try:
            return await EquipmentStabilityService(
                api=self._api,
                timeline=self._timeline,
                config=EquipmentStabilityConfig(
                    stable_seconds=self._config.stable_seconds,
                    poll_seconds=self._config.poll_seconds,
                ),
                record_observation=self._record_observation,
                progress=self._progress,
            ).wait(
                identifiers,
                phase=phase,
                timeout_seconds=timeout_seconds,
                initial_snapshot=initial_snapshot,
            )
        except EquipmentStabilityFailure as error:
            raise PdaEquipmentControlFailure(str(error)) from error

    async def set_device(
        self,
        identifier: str,
        enabled: bool,
        *,
        phase: str,
        markers: ProgrammerMarkers,
        convergence_timeout_seconds: float | None = None,
    ) -> None:
        try:
            await self.actions().set_device(
                identifier,
                enabled,
                phase=phase,
                markers=markers,
                convergence_timeout_seconds=convergence_timeout_seconds,
            )
        except (EquipmentActionFailure, PdaProgrammerFailure) as error:
            raise PdaEquipmentControlFailure(str(error)) from error

    async def set_setpoint(
        self,
        identifier: str,
        value: int,
        *,
        phase: str,
        category: str,
        markers: ProgrammerMarkers,
    ) -> None:
        try:
            await self.actions().set_setpoint(
                identifier,
                value,
                phase=phase,
                category=category,
                markers=markers,
            )
        except (EquipmentActionFailure, PdaProgrammerFailure) as error:
            raise PdaEquipmentControlFailure(str(error)) from error

    async def wait_for_device_state(
        self,
        identifier: str,
        enabled: bool,
        *,
        timeout_seconds: float,
    ) -> int:
        try:
            return await self.actions().wait_for_device_state(
                identifier,
                enabled,
                timeout_seconds=timeout_seconds,
            )
        except EquipmentActionFailure as error:
            raise PdaEquipmentControlFailure(str(error)) from error
