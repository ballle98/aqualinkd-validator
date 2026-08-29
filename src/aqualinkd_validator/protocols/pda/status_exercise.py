from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ...domain import EquipmentSnapshot
from ...interfaces import EventTimeline, OrderedLogEvents
from .equipment_setup import PdaEquipmentSetupResult
from .equipment_status import PdaEquipmentStatusResult, PdaEquipmentStatusService

ProgressSink = Callable[[str], None]
SkipSink = Callable[[str, str], None]
MeasurementSink = Callable[..., None]
PrepareForStatus = Callable[[], Awaitable[None]]


class EquipmentStatusSetup(Protocol):
    async def prepare(
        self,
        initial_snapshot: EquipmentSnapshot,
        candidates: Sequence[str],
    ) -> PdaEquipmentSetupResult: ...


@dataclass(frozen=True)
class PdaEquipmentStatusExerciseResult:
    verification: PdaEquipmentStatusResult | None


class PdaEquipmentStatusExercise:
    """Prepare equipment and validate one complete PDA status-menu cycle."""

    def __init__(
        self,
        *,
        events: OrderedLogEvents,
        timeline: EventTimeline,
        setup: EquipmentStatusSetup,
        status: PdaEquipmentStatusService,
        record_skip: SkipSink,
        record_measurement: MeasurementSink,
        progress: ProgressSink,
        prepare_for_status: PrepareForStatus | None = None,
    ) -> None:
        self._events = events
        self._timeline = timeline
        self._setup = setup
        self._status = status
        self._record_skip = record_skip
        self._record_measurement = record_measurement
        self._progress = progress
        self._prepare_for_status = prepare_for_status

    async def run(
        self,
        *,
        initial_snapshot: EquipmentSnapshot,
        candidates: Sequence[str],
    ) -> PdaEquipmentStatusExerciseResult:
        setup = await self._setup.prepare(initial_snapshot, candidates)
        controls = tuple(setup.controls)
        if not controls:
            self._record_skip(
                "devices.status_menu",
                "No configured equipment can be enabled for status testing",
            )
            return PdaEquipmentStatusExerciseResult(verification=None)

        wait_started = self._timeline.offset_ns()
        cursor = self._events.cursor
        if self._prepare_for_status is not None:
            await self._prepare_for_status()
        loop = await self._status.wait_for_complete_loop(after=cursor)
        verification = await self._status.verify(
            initial_snapshot=initial_snapshot,
            controls=controls,
            events=loop.events,
            setup_states=setup.states,
        )
        report = verification.report
        swg_suffix = (
            f"; SWG {report['swg']['percent']}%"
            if report["swg"]["percent"] is not None
            else ("; SWG status observed" if report["swg"]["present"] else "")
        )
        self._progress(
            f"[STATE ] Equipment status verified "
            f"{verification.verified_count}/{verification.expected_count} devices"
            f"{swg_suffix}"
        )
        self._record_measurement(
            name="pda.status_menu.complete",
            category="state_wait",
            phase="devices.status_menu",
            target="equipment_status_menu",
            requested_value="complete",
            start_offset_ns=wait_started,
            api_ack_offset_ns=None,
            log_completion_offset_ns=loop.reconciled.offset_ns,
            state_observed_offset_ns=self._timeline.offset_ns(),
        )
        return PdaEquipmentStatusExerciseResult(verification=verification)
