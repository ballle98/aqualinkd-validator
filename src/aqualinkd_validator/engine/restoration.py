from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..domain import DeviceState, EquipmentSnapshot, EquipmentStateError

SnapshotReader = Callable[[], Awaitable[EquipmentSnapshot]]
SetpointRestorer = Callable[[str, int], Awaitable[None]]
DeviceRestorer = Callable[[str, bool], Awaitable[None]]

_HEATERS = frozenset({"Pool_Heater", "Spa_Heater", "Solar_Heater"})
_SPA_MODES = frozenset({"Spa", "Spa_Mode"})
_FILTER_PUMP = "Filter_Pump"


@dataclass(frozen=True)
class RestorationAction:
    target: str
    property: str
    value: int | bool
    status: str = "restored"


@dataclass(frozen=True)
class RestorationResult:
    actions: tuple[RestorationAction, ...]
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.errors


class RestorationSession:
    """Tracks mutations and orchestrates restoration to an initial snapshot.

    The session owns mutation bookkeeping, dependency-aware ordering, and
    duplicate-request suppression. Protocol-specific execution remains behind
    callbacks so PDA log correlation is not coupled to the safety policy.
    """

    def __init__(self) -> None:
        self._initial_snapshot: EquipmentSnapshot | None = None
        self._touched_devices: set[str] = set()
        self._touched_setpoints: set[str] = set()
        self._requested_states: dict[str, bool] = {}

    @property
    def initial_snapshot(self) -> EquipmentSnapshot | None:
        return self._initial_snapshot

    @property
    def has_pending_mutations(self) -> bool:
        return bool(self._touched_devices or self._touched_setpoints)

    def capture_initial(self, snapshot: EquipmentSnapshot) -> None:
        if self.has_pending_mutations and snapshot is not self._initial_snapshot:
            raise RuntimeError("cannot replace initial state with pending mutations")
        self._initial_snapshot = snapshot

    def begin_case(self) -> None:
        self._touched_devices.clear()
        self._touched_setpoints.clear()

    def touch_device(self, identifier: str) -> None:
        self._require_initial_device(identifier)
        self._touched_devices.add(identifier)

    def touch_setpoint(self, identifier: str) -> None:
        self._require_initial_device(identifier)
        self._touched_setpoints.add(identifier)

    def forget_requested_state(self, identifier: str) -> None:
        self._requested_states.pop(identifier, None)

    def requested_state(self, identifier: str) -> bool | None:
        return self._requested_states.get(identifier)

    def mark_requested_state(self, identifier: str, enabled: bool) -> None:
        self._requested_states[identifier] = enabled

    def initial_device_enabled(self, identifier: str) -> bool:
        return self._require_initial_device(identifier).enabled

    async def restore(
        self,
        *,
        read_snapshot: SnapshotReader,
        restore_setpoint: SetpointRestorer,
        restore_device: DeviceRestorer,
    ) -> RestorationResult:
        if self._initial_snapshot is None:
            return RestorationResult(actions=(), errors=())

        actions: list[RestorationAction] = []
        errors: list[str] = []

        for identifier in sorted(self._touched_setpoints):
            try:
                original = self._require_original_setpoint(identifier)
                current = self._require_device(await read_snapshot(), identifier)
                if current.setpoint != original:
                    await restore_setpoint(identifier, original)
                actions.append(
                    RestorationAction(identifier, "setpoint", original)
                )
            except Exception as error:
                errors.append(f"{identifier} setpoint: {error}")

        for identifier in sorted(
            self._touched_devices,
            key=self._restoration_order,
        ):
            try:
                expected = self.initial_device_enabled(identifier)
                await restore_device(identifier, expected)
                actions.append(RestorationAction(identifier, "state", expected))
            except Exception as error:
                errors.append(f"{identifier} state: {error}")

        if not errors:
            self.begin_case()
        return RestorationResult(tuple(actions), tuple(errors))

    def _restoration_order(self, identifier: str) -> tuple[int, int, str]:
        expected = self.initial_device_enabled(identifier)
        if not expected:
            # Remove heat demand before changing water mode, then leave the
            # filter pump until last so panel-controlled cooldown can finish.
            rank = (
                0
                if identifier in _HEATERS
                else 2
                if identifier in _SPA_MODES
                else 3
                if identifier == _FILTER_PUMP
                else 1
            )
            return (0, rank, identifier)

        # When the original state was on, restore dependencies in reverse:
        # circulation and water mode before heaters.
        rank = (
            0
            if identifier == _FILTER_PUMP
            else 1
            if identifier in _SPA_MODES
            else 3
            if identifier in _HEATERS
            else 2
        )
        return (1, rank, identifier)

    def _require_initial_device(self, identifier: str) -> DeviceState:
        if self._initial_snapshot is None:
            raise RuntimeError("initial equipment state has not been captured")
        return self._require_device(self._initial_snapshot, identifier)

    def _require_original_setpoint(self, identifier: str) -> int:
        value = self._require_initial_device(identifier).setpoint
        if value is None:
            raise EquipmentStateError(
                f"device {identifier} has no numeric original setpoint"
            )
        return value

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
