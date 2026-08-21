from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..domain import DeviceState, EquipmentSnapshot, EquipmentStateError
from ..interfaces import AqualinkApi, EventTimeline

ObservationSink = Callable[[dict[str, Any]], None]


class EquipmentStabilityFailure(RuntimeError):
    """Raised when selected equipment does not reach a stable API state."""


@dataclass(frozen=True)
class EquipmentStabilityConfig:
    stable_seconds: float = 0.5
    poll_seconds: float = 0.25


class EquipmentStabilityService:
    """Poll typed equipment state until transitions and state changes stop."""

    def __init__(
        self,
        *,
        api: AqualinkApi,
        timeline: EventTimeline,
        config: EquipmentStabilityConfig,
        record_observation: ObservationSink,
        progress: Callable[[str], None],
    ) -> None:
        self._api = api
        self._timeline = timeline
        self._config = config
        self._record_observation = record_observation
        self._progress = progress

    async def wait(
        self,
        identifiers: Sequence[str],
        *,
        phase: str,
        timeout_seconds: float,
        initial_snapshot: EquipmentSnapshot | None = None,
    ) -> EquipmentSnapshot:
        selected = tuple(identifiers)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        stable_since: float | None = None
        previous_signature: tuple[tuple[str, int, str, str], ...] | None = None
        recorded_signature: tuple[tuple[str, int, str, str], ...] | None = None
        pending: list[str] = []
        snapshot = initial_snapshot
        self._progress(
            f"[ WAIT ] Equipment state: waiting for {phase} to stabilize "
            f"(timeout {timeout_seconds:g}s)"
        )
        while loop.time() < deadline:
            if snapshot is None:
                snapshot = await self._api.devices()
            states = {
                identifier: self._state_details(
                    self._require_device(snapshot, identifier)
                )
                for identifier in selected
            }
            signature = tuple(
                (
                    identifier,
                    state["int_status"],
                    state["state"],
                    state["status"],
                )
                for identifier, state in states.items()
            )
            pending = [
                identifier
                for identifier, state in states.items()
                if state["transitioning"]
            ]
            now = loop.time()
            if pending or signature != previous_signature:
                stable_since = None if pending else now
            elif stable_since is None:
                stable_since = now

            if signature != recorded_signature:
                await self._record(
                    phase=phase,
                    states=states,
                    pending=pending,
                    stable=False,
                )
                recorded_signature = signature

            if (
                not pending
                and stable_since is not None
                and now - stable_since >= self._config.stable_seconds
            ):
                await self._record(
                    phase=phase,
                    states=states,
                    pending=[],
                    stable=True,
                )
                self._progress(f"[STATE ] Equipment state stable for {phase}")
                return snapshot

            previous_signature = signature
            snapshot = None
            await asyncio.sleep(self._config.poll_seconds)

        pending_text = ", ".join(pending) if pending else "state kept changing"
        raise EquipmentStabilityFailure(
            f"Equipment state did not stabilize for {phase} within "
            f"{timeout_seconds:g}s ({pending_text})"
        )

    async def _record(
        self,
        *,
        phase: str,
        states: dict[str, dict[str, Any]],
        pending: list[str],
        stable: bool,
    ) -> None:
        observation = {
            "offset_ns": self._timeline.offset_ns(),
            "phase": phase,
            "stable": stable,
            "pending": pending,
            "devices": states,
        }
        self._record_observation(observation)
        await self._timeline.write(
            "equipment_state_observation",
            phase=phase,
            stable=stable,
            pending=pending,
            devices=states,
        )

    @staticmethod
    def _require_device(
        snapshot: EquipmentSnapshot,
        identifier: str,
    ) -> DeviceState:
        try:
            return snapshot.devices[identifier]
        except KeyError as error:
            raise EquipmentStabilityFailure(
                f"Required device {identifier} is absent from /api/devices"
            ) from error

    @staticmethod
    def _state_details(device: DeviceState) -> dict[str, Any]:
        try:
            return {
                "int_status": device.int_status,
                "state": device.state,
                "status": device.status,
                "enabled": device.enabled,
                "active": device.active,
                "transitioning": device.transitioning,
            }
        except EquipmentStateError as error:
            raise EquipmentStabilityFailure(str(error)) from error
