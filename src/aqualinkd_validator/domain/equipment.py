from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any


class EquipmentStateError(ValueError):
    """Raised when AqualinkD returns an unusable equipment state."""


@dataclass(frozen=True)
class DeviceState(Mapping[str, Any]):
    """Typed interpretation of one raw ``/api/devices`` entry.

    The mapping interface is temporary migration support for protocol-specific
    fields such as ``spvalue``. New code should prefer the typed properties.
    """

    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", dict(self.raw))

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.raw)

    def __len__(self) -> int:
        return len(self.raw)

    @property
    def identifier(self) -> str:
        value = self.raw.get("id")
        if not isinstance(value, str) or not value:
            raise EquipmentStateError("device has no valid id")
        return value

    @property
    def name(self) -> str:
        value = self.raw.get("name")
        return value if isinstance(value, str) else ""

    @property
    def kind(self) -> str:
        value = self.raw.get("type")
        return value if isinstance(value, str) else ""

    @property
    def int_status(self) -> int:
        value = self.raw.get("int_status")
        if not isinstance(value, (int, str)):
            raise EquipmentStateError(
                f"device {self.raw.get('id', '<unknown>')} has invalid "
                f"int_status {value!r}"
            )
        try:
            return int(value)
        except ValueError as error:
            raise EquipmentStateError(
                f"device {self.raw.get('id', '<unknown>')} has invalid "
                f"int_status {value!r}"
            ) from error

    @property
    def state(self) -> str:
        return str(self.raw.get("state", "")).strip().casefold()

    @property
    def status(self) -> str:
        return str(self.raw.get("status", "")).strip().casefold()

    @property
    def enabled(self) -> bool:
        return self.int_status != 0

    @property
    def active(self) -> bool:
        return self.int_status == 1

    @property
    def transitioning(self) -> bool:
        return (
            self.int_status in {2, 4}
            or "***" in self.state
            or "***" in self.status
            or self.status in {"flash", "flashing", "pending", "unknown"}
        )

    @property
    def setpoint(self) -> int | None:
        value = self.raw.get("spvalue")
        if value is None:
            return None
        try:
            return round(float(value))
        except (TypeError, ValueError) as error:
            raise EquipmentStateError(
                f"device {self.raw.get('id', '<unknown>')} has invalid "
                f"spvalue {value!r}"
            ) from error

    def requested_state_label(self, enabled: bool) -> str:
        if self.kind == "setpoint_thermo":
            return "enabled" if enabled else "disabled"
        return "on" if enabled else "off"


@dataclass(frozen=True, init=False)
class EquipmentSnapshot:
    temp_units: str
    devices: dict[str, DeviceState]

    def __init__(
        self,
        *,
        temp_units: str,
        devices: Mapping[str, DeviceState | Mapping[str, Any]],
    ) -> None:
        normalized = {
            identifier: (
                device if isinstance(device, DeviceState) else DeviceState(device)
            )
            for identifier, device in devices.items()
        }
        object.__setattr__(self, "temp_units", temp_units)
        object.__setattr__(self, "devices", normalized)
