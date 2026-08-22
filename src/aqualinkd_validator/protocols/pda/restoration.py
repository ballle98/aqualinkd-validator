from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ...domain import DeviceState, EquipmentSnapshot, EquipmentStateError
from ...engine.restoration import RestorationResult, RestorationSession
from ...interfaces import AqualinkApi

SetDevice = Callable[[str, bool, str, float], Awaitable[None]]
SetSetpoint = Callable[[str, int], Awaitable[None]]
StableSnapshotWaiter = Callable[
    [Sequence[str], str, float, EquipmentSnapshot],
    Awaitable[EquipmentSnapshot],
]
DeviceStateWaiter = Callable[[str, bool, float], Awaitable[None]]
ProgressSink = Callable[[str], None]


@dataclass(frozen=True)
class PdaRestorationConfig:
    timeout_seconds: float


class PdaRestorationService:
    """Apply PDA-specific transition handling around restoration policy."""

    def __init__(
        self,
        *,
        api: AqualinkApi,
        session: RestorationSession,
        config: PdaRestorationConfig,
        set_device: SetDevice,
        set_setpoint: SetSetpoint,
        wait_for_stable: StableSnapshotWaiter,
        wait_for_device_state: DeviceStateWaiter,
        progress: ProgressSink,
    ) -> None:
        self._api = api
        self._session = session
        self._config = config
        self._set_device = set_device
        self._set_setpoint = set_setpoint
        self._wait_for_stable = wait_for_stable
        self._wait_for_device_state = wait_for_device_state
        self._progress = progress

    async def restore(
        self,
        initial_snapshot: EquipmentSnapshot,
    ) -> RestorationResult:
        if self._session.initial_snapshot is not initial_snapshot:
            self._session.capture_initial(initial_snapshot)
        return await self._session.restore(
            read_snapshot=self._api.devices,
            restore_setpoint=self._set_setpoint,
            restore_device=self._restore_device,
        )

    async def _restore_device(self, identifier: str, expected: bool) -> None:
        snapshot = await self._api.devices()
        device = self._require_device(snapshot, identifier)
        requested = self._session.requested_state(identifier)
        if device.transitioning:
            requested_state = device.requested_state_label(expected)
            self._progress(
                f"[ WAIT ] {identifier}: equipment transition is already "
                "pending; not sending another toggle "
                f"(timeout {self._config.timeout_seconds:g}s)"
            )
            snapshot = await self._wait_for_stable(
                [identifier],
                f"restoration.{identifier}.pending_transition",
                self._config.timeout_seconds,
                snapshot,
            )
            device = self._require_device(snapshot, identifier)
            if device.enabled == expected:
                self._progress(
                    f"[STATE ] {identifier} completed the pending "
                    f"{requested_state} transition"
                )
                return

        if device.enabled == expected:
            return

        if requested == expected:
            self._progress(
                f"[ WAIT ] {identifier}: a restoration request was already "
                "sent; not sending another toggle "
                f"(timeout {self._config.timeout_seconds:g}s)"
            )
            await self._wait_for_device_state(
                identifier,
                expected,
                self._config.timeout_seconds,
            )
            self._progress(
                f"[STATE ] {identifier} completed the pending "
                f"{device.requested_state_label(expected)} transition"
            )
            return

        self._session.mark_requested_state(identifier, expected)
        await self._set_device(
            identifier,
            expected,
            "restoration.device",
            self._config.timeout_seconds,
        )

    @staticmethod
    def _require_device(
        snapshot: EquipmentSnapshot,
        identifier: str,
    ) -> DeviceState:
        try:
            return snapshot.devices[identifier]
        except KeyError as error:
            raise EquipmentStateError(
                f"required device {identifier} is absent from equipment state"
            ) from error
