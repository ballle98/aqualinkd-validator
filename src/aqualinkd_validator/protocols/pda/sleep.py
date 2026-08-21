from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ...interfaces import EventTimeline, OrderedLogEvents
from ...supervisor import LineEvent
from .programmer import PdaProgrammerFailure, PdaProgrammerObserver

PDA_SLEEPING = "PDA Aqualink daemon in sleep mode"
PDA_ADDRESS_STATUS = "To 0x60 of type           Status"
PDA_ADDRESS_PROBE = "To 0x60 of type            Probe"
WAKE_INIT_ACTIVE = "is active (PDA init after wake)"
WAKE_INIT_FINISHED = "(PDA init after wake) finished"


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


@dataclass(frozen=True)
class PdaSleepWakeConfig:
    sleep_timeout_seconds: float
    action_timeout_seconds: float
    status_retry_delay_seconds: float
    probe_command_min_delay_seconds: float


@dataclass(frozen=True)
class PdaSleepCycleResult:
    report: dict[str, float]


@dataclass(frozen=True)
class PdaStatusRetryWindow:
    sleep_event: LineEvent
    retry_count: int


@dataclass(frozen=True)
class PdaProbeWindow:
    sleep_event: LineEvent
    probe_event: LineEvent
    probe_delay_seconds: float


class PdaSleepWakeFailure(RuntimeError):
    """Raised when an expected PDA sleep/wake transition is not observed."""


class PdaSleepWakeService:
    """Observe PDA sleep, wake, STATUS-retry, and probe transitions."""

    def __init__(
        self,
        *,
        events: OrderedLogEvents,
        timeline: EventTimeline,
        programmer: PdaProgrammerObserver,
        config: PdaSleepWakeConfig,
        record_measurement: MeasurementRecorder,
        progress: Callable[[str], None],
    ) -> None:
        self._events = events
        self._timeline = timeline
        self._programmer = programmer
        self._config = config
        self._record_measurement = record_measurement
        self._progress = progress

    async def observe_natural_cycle(self) -> PdaSleepCycleResult:
        entered = await self._wait_for_sleep(
            phase="devices.sleeping",
            measurement_name="pda.sleep.enter",
        )
        self._progress(
            "[STATE ] PDA entered sleep; observing one natural wake cycle"
        )
        try:
            wake_active = await self._programmer.wait_for_active(
                self._events,
                self._timeline,
                task_name="PDA init after wake",
                marker=WAKE_INIT_ACTIVE,
                after=entered.sequence,
                requested_offset_ns=entered.offset_ns,
                timeout_seconds=self._config.sleep_timeout_seconds,
                wait_reason="waiting for the natural PDA wake",
            )
            wake_finished = await self._programmer.wait_for_completion(
                self._events,
                self._timeline,
                task_name="PDA init after wake",
                marker=WAKE_INIT_FINISHED,
                active=wake_active,
                timeout_seconds=self._config.action_timeout_seconds,
            )
        except PdaProgrammerFailure as error:
            raise PdaSleepWakeFailure(str(error)) from error
        self._progress(
            "[STATE ] Post-wake equipment status refresh complete; "
            "waiting for PDA sleep"
        )
        try:
            returned = await self._events.wait_for(
                PDA_SLEEPING,
                after=wake_finished.sequence,
                timeout_seconds=self._config.sleep_timeout_seconds,
            )
        except TimeoutError as error:
            raise PdaSleepWakeFailure(
                "PDA did not return to sleep within "
                f"{self._config.sleep_timeout_seconds:g}s after the "
                "post-wake status refresh"
            ) from error

        asleep_ns = wake_active.offset_ns - entered.offset_ns
        refresh_ns = wake_finished.offset_ns - wake_active.offset_ns
        return_ns = returned.offset_ns - wake_finished.offset_ns
        awake_ns = returned.offset_ns - wake_active.offset_ns
        cycle_ns = returned.offset_ns - entered.offset_ns
        awake_percent = 100 * awake_ns / cycle_ns
        sleep_percent = 100 * asleep_ns / cycle_ns
        report = {
            "sleep_ms": round(asleep_ns / 1_000_000, 3),
            "status_refresh_ms": round(refresh_ns / 1_000_000, 3),
            "return_to_sleep_ms": round(return_ns / 1_000_000, 3),
            "awake_ms": round(awake_ns / 1_000_000, 3),
            "cycle_ms": round(cycle_ns / 1_000_000, 3),
            "awake_percent": round(awake_percent, 3),
            "sleep_percent": round(sleep_percent, 3),
        }
        self._record_cycle_measurements(
            entered=entered,
            wake_active=wake_active,
            wake_finished=wake_finished,
            returned=returned,
        )
        self._progress(
            "[STATE ] PDA returned to sleep: "
            f"asleep {asleep_ns / 1_000_000_000:.3f}s, "
            f"status refresh {refresh_ns / 1_000_000_000:.3f}s, "
            f"post-status awake {return_ns / 1_000_000_000:.3f}s, "
            f"cycle {cycle_ns / 1_000_000_000:.3f}s, "
            f"awake {awake_percent:.1f}% / sleep {sleep_percent:.1f}%"
        )
        return PdaSleepCycleResult(report)

    async def wait_for_status_retry_window(self) -> PdaStatusRetryWindow:
        entered = await self._wait_for_sleep(
            phase="devices.sleep.status_retry",
            measurement_name="pda.sleep.status_retry.command_ready",
        )
        delay = self._config.status_retry_delay_seconds
        self._progress(
            f"[ WAIT ] PDA STATUS retry phase: delaying {delay:g}s "
            "after sleep begins"
        )
        await asyncio.sleep(delay)
        events = [
            event
            for event in self._events.recent_events()
            if event.sequence > entered.sequence
        ]
        if any(PDA_ADDRESS_PROBE in event.text for event in events):
            raise PdaSleepWakeFailure(
                "PDA address probing began before the STATUS-retry command was sent"
            )
        retry_count = sum(PDA_ADDRESS_STATUS in event.text for event in events)
        if retry_count == 0:
            raise PdaSleepWakeFailure(
                "No repeated PDA STATUS packet was observed before the "
                "STATUS-retry command"
            )
        return PdaStatusRetryWindow(entered, retry_count)

    async def wait_for_probe_window(self) -> PdaProbeWindow:
        entered = await self._wait_for_sleep(
            phase="devices.sleep.probing",
            measurement_name="pda.sleep.probe.command_ready",
        )
        self._progress(
            "[ WAIT ] PDA probe phase: waiting for a probe to address 0x60 "
            f"(timeout {self._config.sleep_timeout_seconds:g}s)"
        )
        try:
            probe = await self._events.wait_for(
                PDA_ADDRESS_PROBE,
                after=entered.sequence,
                timeout_seconds=self._config.sleep_timeout_seconds,
            )
        except TimeoutError as error:
            raise PdaSleepWakeFailure(
                "Panel did not begin probing PDA address 0x60 after sleep"
            ) from error
        probe_delay = (probe.offset_ns - entered.offset_ns) / 1_000_000_000
        remaining = max(
            0.0,
            self._config.probe_command_min_delay_seconds - probe_delay,
        )
        if remaining:
            self._progress(
                f"[ WAIT ] Probe observed early; delaying {remaining:.3f}s "
                "so the command is at least "
                f"{self._config.probe_command_min_delay_seconds:g}s after "
                "sleep began"
            )
            await asyncio.sleep(remaining)
        return PdaProbeWindow(entered, probe, probe_delay)

    async def _wait_for_sleep(
        self,
        *,
        phase: str,
        measurement_name: str,
    ) -> LineEvent:
        cursor = self._events.cursor
        started = self._timeline.offset_ns()
        event = await self._events.wait_for(
            PDA_SLEEPING,
            after=cursor,
            timeout_seconds=self._config.sleep_timeout_seconds,
        )
        self._record_measurement(
            name=measurement_name,
            category="state_wait",
            phase=phase,
            target="pda_sleep",
            requested_value=True,
            start_offset_ns=started,
            api_ack_offset_ns=None,
            log_completion_offset_ns=event.offset_ns,
            state_observed_offset_ns=None,
        )
        return event

    def _record_cycle_measurements(
        self,
        *,
        entered: LineEvent,
        wake_active: LineEvent,
        wake_finished: LineEvent,
        returned: LineEvent,
    ) -> None:
        measurements = (
            (
                "pda.sleep.duration",
                "pda_sleep",
                entered.offset_ns,
                wake_active.offset_ns,
                None,
            ),
            (
                "pda.after_wake.status_refresh",
                "pda_status",
                wake_active.offset_ns,
                wake_finished.offset_ns,
                wake_active.offset_ns,
            ),
            (
                "pda.after_wake.return_to_sleep",
                "pda_sleep",
                wake_finished.offset_ns,
                returned.offset_ns,
                None,
            ),
            (
                "pda.wake.duration",
                "pda_awake",
                wake_active.offset_ns,
                returned.offset_ns,
                None,
            ),
            (
                "pda.sleep_wake.cycle",
                "pda_sleep_wake_cycle",
                entered.offset_ns,
                returned.offset_ns,
                None,
            ),
        )
        for name, target, started, completed, active in measurements:
            self._record_measurement(
                name=name,
                category="sleep_cycle",
                phase="devices.sleeping",
                target=target,
                requested_value=True,
                start_offset_ns=started,
                api_ack_offset_ns=None,
                task_active_offset_ns=active,
                log_completion_offset_ns=completed,
                state_observed_offset_ns=None,
            )
