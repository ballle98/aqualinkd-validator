from __future__ import annotations

import contextlib
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ...domain import DeviceState, EquipmentSnapshot
from ...interfaces import LineEvent, OrderedLogEvents

STATUS_MENU_PRESENT = "PDA Start new Equiptment loop"
LEGACY_STATUS_MENU_PRESENT = "PDA Start new Equipment loop"
STATUS_MENU_PRESENT_MARKERS = (
    STATUS_MENU_PRESENT,
    LEGACY_STATUS_MENU_PRESENT,
)
STATUS_MENU_FINISHED = "PDA End Equiptment loop"
LEGACY_STATUS_MENU_FINISHED = "PDA End Equipment loop"
STATUS_MENU_FINISHED_MARKERS = (
    STATUS_MENU_FINISHED,
    LEGACY_STATUS_MENU_FINISHED,
)

_STATUS_HEADER = "Pass Equiptment msg 'EQUIPMENT STATUS"
_STATUS_MESSAGE = re.compile(r"\*\*\* Pass Equiptment msg '([^']*)'")
_FOUND_STATUS = re.compile(
    r"Found(?: EQ CTL)? Status for (.+?)\s*=\s*['\"]?(.+?)['\"]?\s*$",
    re.IGNORECASE,
)
_SWG_PERCENT = re.compile(r"AquaPure\s*=\s*(\d+)", re.IGNORECASE)

StableSnapshotWaiter = Callable[
    [tuple[str, ...], str, float],
    Awaitable[EquipmentSnapshot],
]
ProgressSink = Callable[[str], None]


@dataclass(frozen=True)
class PdaEquipmentStatusLoop:
    started: LineEvent
    finished: LineEvent
    reconciled: LineEvent
    events: tuple[LineEvent, ...]


@dataclass(frozen=True)
class PdaEquipmentStatusResult:
    report: dict[str, Any]

    @property
    def verified_count(self) -> int:
        return len(self.report["verified_devices"])

    @property
    def expected_count(self) -> int:
        return len(self.report["expected_devices"])


class PdaEquipmentStatusFailure(RuntimeError):
    """Raised when a complete PDA equipment-status loop does not reconcile."""

    def __init__(
        self,
        message: str,
        result: PdaEquipmentStatusResult | None = None,
    ) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class _StatusObservation:
    messages: tuple[str, ...]
    found_names: frozenset[str]
    found_status: dict[str, str]
    heater_ids: frozenset[str]
    swg_percent: int | None


class PdaEquipmentStatusService:
    """Observe and reconcile one complete multi-page EQUIPMENT STATUS loop."""

    def __init__(
        self,
        *,
        events: OrderedLogEvents,
        wait_for_stable: StableSnapshotWaiter,
        status_timeout_seconds: float,
        state_timeout_seconds: float,
        progress: ProgressSink,
    ) -> None:
        self._events = events
        self._wait_for_stable = wait_for_stable
        self._status_timeout_seconds = status_timeout_seconds
        self._state_timeout_seconds = state_timeout_seconds
        self._progress = progress

    async def wait_for_complete_loop(self, *, after: int) -> PdaEquipmentStatusLoop:
        self._progress(
            "[ WAIT ] Equipment status: waiting for the PDA home menu "
            f"(timeout {self._status_timeout_seconds:g}s)"
        )
        home = await self._events.wait_for(
            "PDA Menu Line 1 = AIR",
            after=after,
            timeout_seconds=self._status_timeout_seconds,
        )
        self._progress("[STATE ] PDA returned to the home menu")
        self._progress(
            "[ WAIT ] Equipment status: waiting for a complete multi-page "
            f"loop (timeout {self._status_timeout_seconds:g}s)"
        )
        started = await self._events.wait_for_any(
            STATUS_MENU_PRESENT_MARKERS,
            after=home.sequence,
            timeout_seconds=self._status_timeout_seconds,
        )
        self._progress("[STATE ] EQUIPMENT STATUS loop started")
        finished = await self._events.wait_for_any(
            STATUS_MENU_FINISHED_MARKERS,
            after=started.sequence,
            timeout_seconds=self._status_timeout_seconds,
        )
        reconciled = await self._events.wait_for(
            "Start new equipment cycle bitmask",
            after=finished.sequence,
            timeout_seconds=self._state_timeout_seconds,
        )
        self._progress("[STATE ] EQUIPMENT STATUS loop completed and reconciled")
        return PdaEquipmentStatusLoop(
            started=started,
            finished=finished,
            reconciled=reconciled,
            events=self._loop_events(started, reconciled),
        )

    async def verify(
        self,
        *,
        initial_snapshot: EquipmentSnapshot,
        controls: Sequence[str],
        events: Sequence[LineEvent],
        setup_states: dict[str, dict[str, Any]] | None = None,
    ) -> PdaEquipmentStatusResult:
        selected = tuple(controls)
        observed = self.parse(events)
        missing, verified = self._presence(initial_snapshot, selected, observed)
        snapshot = await self._wait_for_stable(
            selected,
            "devices.status_menu.verification",
            self._status_timeout_seconds,
        )
        incorrect_states = [
            identifier
            for identifier in selected
            if not self._require_device(snapshot, identifier).enabled
        ]
        swg = self._swg_status(snapshot, observed)
        heater_states = self._heater_states(snapshot, selected, observed)
        enabled_mismatches = [
            identifier
            for identifier, state in heater_states.items()
            if state["pda_enabled"] is not None
            and state["pda_enabled"] != state["enabled"]
        ]
        active_mismatches = [
            identifier
            for identifier, state in heater_states.items()
            if state["pda_active"] is not None
            and state["pda_active"] != state["active"]
        ]
        report = {
            "setup_states": setup_states or {},
            "expected_devices": list(selected),
            "verified_devices": verified,
            "missing_devices": missing,
            "incorrect_api_states": incorrect_states,
            "status_messages": list(observed.messages),
            "heater_states": heater_states,
            "heater_enabled_mismatches": enabled_mismatches,
            "heater_active_mismatches": active_mismatches,
            "swg": swg,
        }
        result = PdaEquipmentStatusResult(report)
        self._report_heaters(heater_states)
        failures = self._failures(
            missing=missing,
            incorrect_states=incorrect_states,
            heater_enabled_mismatches=enabled_mismatches,
            heater_active_mismatches=active_mismatches,
            swg=swg,
        )
        if failures:
            raise PdaEquipmentStatusFailure("; ".join(failures), result)
        return result

    def _loop_events(
        self,
        started: LineEvent,
        finished: LineEvent,
    ) -> tuple[LineEvent, ...]:
        history = self._events.recent_events()
        first_sequence = started.sequence
        for event in reversed(history):
            if event.sequence >= started.sequence:
                continue
            if _STATUS_HEADER in event.text:
                first_sequence = event.sequence
                break
        return tuple(
            event
            for event in history
            if first_sequence <= event.sequence <= finished.sequence
        )

    @classmethod
    def parse(cls, events: Sequence[LineEvent]) -> _StatusObservation:
        messages: list[str] = []
        found_names: set[str] = set()
        found_status: dict[str, str] = {}
        heater_ids: set[str] = set()
        swg_percent: int | None = None
        for event in events:
            message = _STATUS_MESSAGE.search(event.text)
            if message is not None:
                messages.append(message.group(1).strip())
            found = _FOUND_STATUS.search(event.text)
            if found is not None:
                normalized = cls.normalize_name(found.group(1))
                found_names.add(normalized)
                found_status[normalized] = found.group(2).strip()
            lowered = event.text.casefold()
            if "pool hearter is enabled" in lowered:
                heater_ids.add("Pool_Heater")
            if "spa hearter is enabled" in lowered:
                heater_ids.add("Spa_Heater")
            percent = _SWG_PERCENT.search(event.text)
            if percent is not None:
                swg_percent = int(percent.group(1))
        return _StatusObservation(
            messages=tuple(messages),
            found_names=frozenset(found_names),
            found_status=found_status,
            heater_ids=frozenset(heater_ids),
            swg_percent=swg_percent,
        )

    @classmethod
    def _presence(
        cls,
        initial: EquipmentSnapshot,
        controls: tuple[str, ...],
        observed: _StatusObservation,
    ) -> tuple[list[str], list[str]]:
        missing: list[str] = []
        verified: list[str] = []
        for identifier in controls:
            device = cls._require_device(initial, identifier)
            name = cls.normalize_name(device.name)
            if identifier in observed.heater_ids or name in observed.found_names:
                verified.append(identifier)
            else:
                missing.append(identifier)
        return missing, verified

    @classmethod
    def _heater_states(
        cls,
        snapshot: EquipmentSnapshot,
        controls: tuple[str, ...],
        observed: _StatusObservation,
    ) -> dict[str, dict[str, Any]]:
        states: dict[str, dict[str, Any]] = {}
        for identifier in controls:
            device = cls._require_device(snapshot, identifier)
            if device.kind != "setpoint_thermo":
                continue
            candidates = {
                cls.normalize_name(identifier),
                cls.normalize_name(device.name),
            }
            if identifier == "Pool_Heater":
                candidates.update({"poolheat", "poolheater"})
            elif identifier == "Spa_Heater":
                candidates.update({"spaheat", "spaheater"})
            pda_lines = [
                message
                for message in observed.messages
                if any(
                    cls.normalize_name(message).startswith(candidate)
                    for candidate in candidates
                    if candidate
                )
            ]
            pda_lines.extend(
                status
                for name, status in observed.found_status.items()
                if name in candidates and status not in pda_lines
            )
            pda_enabled: bool | None = None
            pda_active: bool | None = None
            for line in (cls.normalize_name(message) for message in pda_lines):
                if line.endswith("off"):
                    pda_enabled = False
                    pda_active = False
                elif line.endswith(("ena", "enabled")):
                    pda_enabled = True
                    pda_active = False
                elif line.endswith("on") or line in candidates:
                    pda_enabled = True
                    pda_active = True
            states[identifier] = {
                **cls._state_details(device),
                "pda_status_lines": pda_lines,
                "pda_enabled": pda_enabled,
                "pda_active": pda_active,
                "pda_enabled_marker": identifier in observed.heater_ids,
                "found_status": observed.found_status.get(
                    cls.normalize_name(device.name)
                ),
            }
        return states

    @staticmethod
    def _swg_status(
        snapshot: EquipmentSnapshot,
        observed: _StatusObservation,
    ) -> dict[str, Any]:
        devices = [
            device
            for device in snapshot.devices.values()
            if device.kind == "setpoint_swg"
        ]
        present = bool(devices)
        seen = (
            any(
                any(
                    marker in message.casefold()
                    for marker in ("aquapure", "salt", "boost")
                )
                for message in observed.messages
            )
            or observed.swg_percent is not None
        )
        api_percent: int | None = None
        if devices:
            with contextlib.suppress(KeyError, TypeError, ValueError):
                api_percent = round(float(devices[0]["spvalue"]))
        return {
            "present": present,
            "observed": seen,
            "percent": observed.swg_percent,
            "api_percent": api_percent,
        }

    @staticmethod
    def _failures(
        *,
        missing: list[str],
        incorrect_states: list[str],
        heater_enabled_mismatches: list[str],
        heater_active_mismatches: list[str],
        swg: dict[str, Any],
    ) -> list[str]:
        failures: list[str] = []
        if missing:
            failures.append("missing status entries for " + ", ".join(missing))
        if incorrect_states:
            failures.append(
                "API marked expected-on devices off after status processing: "
                + ", ".join(incorrect_states)
            )
        if heater_enabled_mismatches:
            failures.append(
                "PDA heater enabled status disagreed with the API: "
                + ", ".join(heater_enabled_mismatches)
            )
        if heater_active_mismatches:
            failures.append(
                "PDA heater active status disagreed with the API: "
                + ", ".join(heater_active_mismatches)
            )
        if swg["present"] and not swg["observed"]:
            failures.append("SWG is present but no SWG status was captured")
        if (
            swg["percent"] is not None
            and swg["api_percent"] is not None
            and swg["percent"] != swg["api_percent"]
        ):
            failures.append(
                f"SWG status reported {swg['percent']}% but API reported "
                f"{swg['api_percent']}%"
            )
        return failures

    def _report_heaters(self, states: dict[str, dict[str, Any]]) -> None:
        for identifier, state in states.items():
            enabled = (
                "enabled"
                if state["pda_enabled"] is True
                else "disabled"
                if state["pda_enabled"] is False
                else "not reported"
            )
            active = (
                "active"
                if state["pda_active"] is True
                else "inactive"
                if state["pda_active"] is False
                else "not reported"
            )
            self._progress(
                f"[STATE ] {identifier}: "
                f"{'enabled' if state['enabled'] else 'disabled'}, "
                f"{'actively heating' if state['active'] else 'not actively heating'}, "
                f"PDA {enabled}/{active}"
            )

    @staticmethod
    def _require_device(
        snapshot: EquipmentSnapshot,
        identifier: str,
    ) -> DeviceState:
        try:
            return snapshot.devices[identifier]
        except KeyError as error:
            raise PdaEquipmentStatusFailure(
                f"Required device {identifier} is absent from /api/devices"
            ) from error

    @staticmethod
    def _state_details(device: DeviceState) -> dict[str, Any]:
        return {
            "int_status": device.int_status,
            "state": device.state,
            "status": device.status,
            "enabled": device.enabled,
            "active": device.active,
            "transitioning": device.transitioning,
        }

    @staticmethod
    def normalize_name(value: str) -> str:
        return "".join(
            character for character in value.casefold() if character.isalnum()
        )
