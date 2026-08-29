from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...domain import EquipmentSnapshot

FILTER_PUMP = "Filter_Pump"
_AUX_IDENTIFIER = re.compile(r"Aux_(\d+)$", re.IGNORECASE)
_SPA_MODE_IDENTIFIERS = frozenset({"Spa", "Spa_Mode"})
_STATUS_HYDRAULIC_CONTROLS = frozenset(
    {
        *_SPA_MODE_IDENTIFIERS,
        # AqualinkD exposes the PDA solar-heater position under this legacy
        # identifier even when the panel does not have solar configured.
        "Extra_Aux",
        "Solar_Heater",
    }
)

SkipSink = Callable[[str, str], None]


class PdaDeviceSelectionFailure(RuntimeError):
    """Raised when an explicitly requested PDA device cannot be exercised."""


@dataclass(frozen=True)
class PdaDeviceSelectionConfig:
    requested: tuple[str, ...] = ()
    disabled_button_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class PdaDeviceConstraints:
    excluded: tuple[dict[str, Any], ...]
    button_number_by_identifier: dict[str, int]


class PdaDeviceSelector:
    """Resolve safe PDA test devices from panel identity and API discovery."""

    def __init__(
        self,
        config: PdaDeviceSelectionConfig,
        *,
        record_skip: SkipSink,
    ) -> None:
        self._config = config
        self._record_skip = record_skip
        self._snapshot: EquipmentSnapshot | None = None
        self._reported_panel_size: int | None = None
        self._reported_panel_combo: bool | None = None
        self._constraints = PdaDeviceConstraints((), {})

    @property
    def constraints(self) -> PdaDeviceConstraints:
        return self._constraints

    def configure(
        self,
        snapshot: EquipmentSnapshot,
        *,
        reported_panel_size: int | None,
        reported_panel_combo: bool | None,
    ) -> PdaDeviceConstraints:
        self._snapshot = snapshot
        self._reported_panel_size = reported_panel_size
        self._reported_panel_combo = reported_panel_combo
        button_numbers = {
            identifier: number
            for identifier in snapshot.devices
            if (number := self._button_number(identifier)) is not None
        }
        disabled = set(self._config.disabled_button_numbers)
        excluded: list[dict[str, Any]] = []
        for identifier, device in snapshot.devices.items():
            if device.kind not in {"switch", "setpoint_thermo"}:
                continue
            reasons: list[str] = []
            button_number = button_numbers.get(identifier)
            if button_number is not None and button_number in disabled:
                reasons.append(
                    f"button_{button_number:02d}_label is configured as NONE"
                )
            api_name = device.name.strip()
            if api_name.casefold() == "none":
                reasons.append("API device name is NONE")
            auxiliary = _AUX_IDENTIFIER.fullmatch(identifier)
            if auxiliary is not None and reported_panel_size is not None:
                auxiliary_number = int(auxiliary.group(1))
                if auxiliary_number >= reported_panel_size:
                    reasons.append(
                        f"Aux_{auxiliary_number} is beyond reported "
                        f"panel size {reported_panel_size}"
                    )
            if reasons:
                excluded.append(
                    {
                        "button": button_number,
                        "identifier": identifier,
                        "name": api_name,
                        "reasons": reasons,
                    }
                )
        self._constraints = PdaDeviceConstraints(tuple(excluded), button_numbers)
        return self._constraints

    def status_candidates(self, *, phase: str) -> tuple[str, ...]:
        snapshot = self._require_snapshot()
        candidates = tuple(
            identifier
            for identifier, device in snapshot.devices.items()
            if device.kind in {"switch", "setpoint_thermo"}
            and identifier not in _STATUS_HYDRAULIC_CONTROLS
            and not self.skip_unactionable(identifier, phase=phase)
        )
        deferred = sorted(_STATUS_HYDRAULIC_CONTROLS & snapshot.devices.keys())
        if deferred:
            self._record_skip(
                "devices.status_menu.spa_hydraulics",
                "Left unchanged because the general status test must not route "
                "water or demand solar heat: " + ", ".join(deferred),
            )
        return candidates

    def consecutive_switches(self, *, phase: str) -> tuple[str, ...]:
        identifiers = self._requested_or_discovered_switches()
        selected = tuple(
            identifier
            for identifier in identifiers
            if not self._skip_hydraulic_control(identifier, phase=phase)
            and not self.skip_unactionable(identifier, phase=phase)
        )
        if not selected:
            self._record_skip(
                phase,
                "No switch devices were discovered in /api/devices",
            )
        return selected

    def sleep_switch(self, *, phase: str) -> str | None:
        identifiers = tuple(
            identifier
            for identifier in self._requested_or_discovered_switches()
            if not self.skip_unactionable(identifier, phase=phase)
        )
        if not identifiers:
            self._record_skip(
                phase,
                "No actionable switch devices were discovered",
            )
            return None
        return max(identifiers, key=self._sleep_priority)

    def skip_unactionable(self, identifier: str, *, phase: str) -> bool:
        excluded = next(
            (
                item
                for item in self._constraints.excluded
                if item["identifier"] == identifier
            ),
            None,
        )
        if excluded is None:
            return False
        self._record_skip(
            f"{phase}.{identifier}",
            "; ".join(excluded["reasons"]),
        )
        return True

    def _requested_or_discovered_switches(self) -> tuple[str, ...]:
        snapshot = self._require_snapshot()
        requested = tuple(dict.fromkeys(self._config.requested))
        if not requested:
            return tuple(
                identifier
                for identifier, device in snapshot.devices.items()
                if device.kind == "switch"
            )
        for identifier in requested:
            try:
                device = snapshot.devices[identifier]
            except KeyError as error:
                raise PdaDeviceSelectionFailure(
                    f"Required device {identifier} is absent from /api/devices"
                ) from error
            if device.kind != "switch":
                raise PdaDeviceSelectionFailure(
                    f"{identifier} is not a switch device"
                )
        return requested

    def _button_number(self, identifier: str) -> int | None:
        if identifier == FILTER_PUMP:
            return 1
        if identifier in _SPA_MODE_IDENTIFIERS:
            return 2 if self._reported_panel_combo else None
        auxiliary = _AUX_IDENTIFIER.fullmatch(identifier)
        if auxiliary is None:
            return None
        offset = 2 if self._reported_panel_combo else 1
        return int(auxiliary.group(1)) + offset

    def _sleep_priority(self, identifier: str) -> tuple[int, int, str]:
        auxiliary = _AUX_IDENTIFIER.fullmatch(identifier)
        if auxiliary is not None:
            return (2, int(auxiliary.group(1)), identifier)
        if identifier == FILTER_PUMP:
            return (0, 0, identifier)
        button_number = self._constraints.button_number_by_identifier.get(
            identifier, 0
        )
        return (1, button_number, identifier)

    def _skip_hydraulic_control(self, identifier: str, *, phase: str) -> bool:
        if identifier not in _STATUS_HYDRAULIC_CONTROLS:
            return False
        reason = (
            "Spa mode changes water routing and is covered by pda-live-spa"
            if identifier in _SPA_MODE_IDENTIFIERS
            else "Solar heating is excluded from general equipment tests"
        )
        self._record_skip(
            f"{phase}.{identifier}",
            reason,
        )
        return True

    def _require_snapshot(self) -> EquipmentSnapshot:
        if self._snapshot is None:
            raise PdaDeviceSelectionFailure(
                "PDA device selector used before equipment discovery"
            )
        return self._snapshot
