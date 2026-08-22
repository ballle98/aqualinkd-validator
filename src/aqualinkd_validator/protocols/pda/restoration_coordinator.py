from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from ...domain import EquipmentSnapshot
from ...engine import ProgrammerMarkers, RestorationResult, RestorationSession
from ...interfaces import AqualinkApi
from .restoration import PdaRestorationConfig, PdaRestorationService


class PdaEquipmentControl(Protocol):
    async def set_device(
        self,
        identifier: str,
        enabled: bool,
        *,
        phase: str,
        markers: ProgrammerMarkers,
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
    ) -> None: ...

    async def wait_for_stable(
        self,
        identifiers: Sequence[str],
        *,
        phase: str,
        timeout_seconds: float,
        initial_snapshot: EquipmentSnapshot | None = None,
    ) -> EquipmentSnapshot: ...

    async def wait_for_device_state(
        self,
        identifier: str,
        enabled: bool,
        *,
        timeout_seconds: float,
    ) -> int: ...


@dataclass(frozen=True)
class PdaRestorationCoordinatorConfig:
    timeout_seconds: float
    device_markers: ProgrammerMarkers
    setpoint_markers: Mapping[str, ProgrammerMarkers]


class PdaRestorationCoordinator:
    """Bind restoration policy to PDA equipment actions and markers."""

    def __init__(
        self,
        *,
        api: AqualinkApi,
        session: RestorationSession,
        control: PdaEquipmentControl,
        config: PdaRestorationCoordinatorConfig,
        progress: Callable[[str], None],
    ) -> None:
        self._api = api
        self._session = session
        self._control = control
        self._config = config
        self._progress = progress

    async def restore(
        self,
        initial_snapshot: EquipmentSnapshot,
    ) -> RestorationResult:
        async def set_device(
            identifier: str,
            enabled: bool,
            phase: str,
            timeout_seconds: float,
        ) -> None:
            await self._control.set_device(
                identifier,
                enabled,
                phase=phase,
                markers=self._config.device_markers,
                convergence_timeout_seconds=timeout_seconds,
            )

        async def set_setpoint(identifier: str, value: int) -> None:
            markers = self._config.setpoint_markers.get(identifier)
            if markers is None:
                raise ValueError(
                    f"No restoration programmer markers for {identifier}"
                )
            await self._control.set_setpoint(
                identifier,
                value,
                phase="restoration.setpoint",
                category="restoration",
                markers=markers,
            )

        async def wait_for_stable(
            identifiers: Sequence[str],
            phase: str,
            timeout_seconds: float,
            initial: EquipmentSnapshot,
        ) -> EquipmentSnapshot:
            return await self._control.wait_for_stable(
                identifiers,
                phase=phase,
                timeout_seconds=timeout_seconds,
                initial_snapshot=initial,
            )

        async def wait_for_device_state(
            identifier: str,
            enabled: bool,
            timeout_seconds: float,
        ) -> None:
            await self._control.wait_for_device_state(
                identifier,
                enabled,
                timeout_seconds=timeout_seconds,
            )

        return await PdaRestorationService(
            api=self._api,
            session=self._session,
            config=PdaRestorationConfig(
                timeout_seconds=self._config.timeout_seconds
            ),
            set_device=set_device,
            set_setpoint=set_setpoint,
            wait_for_stable=wait_for_stable,
            wait_for_device_state=wait_for_device_state,
            progress=self._progress,
        ).restore(initial_snapshot)
