from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from ...interfaces import EventTimeline, LineEvent, OrderedLogEvents


class PdaProgrammerFailure(RuntimeError):
    """Raised when a PDA programmer task cannot be correlated to completion."""


class PdaProgrammerObserver:
    """Correlates queued PDA work with ordered logs and monotonic timing."""

    async def wait_for_active(
        self,
        events: OrderedLogEvents,
        timeline: EventTimeline,
        *,
        task_name: str,
        marker: str | tuple[str, ...],
        after: int,
        requested_offset_ns: int,
        timeout_seconds: float,
        wait_reason: str = "waiting in the programmer queue",
    ) -> LineEvent:
        print(
            f"[ WAIT ] {task_name}: {wait_reason} (timeout {timeout_seconds:g}s)",
            flush=True,
        )
        try:
            active = await self._wait_for_marker(
                events,
                marker,
                after=after,
                timeout_seconds=timeout_seconds,
            )
        except TimeoutError as error:
            raise PdaProgrammerFailure(
                f"{task_name} did not become active within {timeout_seconds:g}s"
            ) from error
        activation_seconds = (active.offset_ns - requested_offset_ns) / 1_000_000_000
        print(
            f"[ACTIVE] {task_name} became active after {activation_seconds:.3f}s",
            flush=True,
        )
        if task_name == "Init PDA":
            print("[STATE ] Init PDA started", flush=True)
        await timeline.write(
            "scenario_programmer_active",
            task=task_name,
            activation_seconds=round(activation_seconds, 6),
        )
        return active

    async def wait_for_completion(
        self,
        events: OrderedLogEvents,
        timeline: EventTimeline,
        *,
        task_name: str,
        marker: str | tuple[str, ...],
        active: LineEvent,
        timeout_seconds: float,
    ) -> LineEvent:
        try:
            completed = await self._wait_for_marker(
                events,
                marker,
                after=active.sequence,
                timeout_seconds=timeout_seconds,
            )
        except TimeoutError as error:
            raise PdaProgrammerFailure(
                f"{task_name} did not complete within {timeout_seconds:g}s "
                "after becoming active"
            ) from error
        programmer_seconds = (completed.offset_ns - active.offset_ns) / 1_000_000_000
        print(
            f"[ DONE ] {task_name} programmer completed in {programmer_seconds:.3f}s",
            flush=True,
        )
        if task_name == "Init PDA":
            print("[STATE ] Init PDA complete", flush=True)
        await timeline.write(
            "scenario_programmer_finished",
            task=task_name,
            programmer_seconds=round(programmer_seconds, 6),
        )
        return completed

    async def wait_for_state_or_error(
        self,
        events: OrderedLogEvents,
        *,
        task_name: str,
        after: int,
        state_wait: Coroutine[Any, Any, int],
        timeout_seconds: float,
    ) -> int:
        state_task: asyncio.Task[int] = asyncio.create_task(state_wait)
        error_task: asyncio.Task[LineEvent] = asyncio.create_task(
            events.wait_for(
                f"PDA Device programmer '{task_name}' didn't find",
                after=after,
                timeout_seconds=timeout_seconds,
            )
        )
        done, _ = await asyncio.wait(
            {state_task, error_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if error_task in done:
            try:
                error_event = error_task.result()
            except TimeoutError:
                return await state_task
            state_task.cancel()
            await asyncio.gather(state_task, return_exceptions=True)
            raise PdaProgrammerFailure(error_event.text.strip())

        error_task.cancel()
        await asyncio.gather(error_task, return_exceptions=True)
        return state_task.result()

    @staticmethod
    async def _wait_for_marker(
        events: OrderedLogEvents,
        marker: str | tuple[str, ...],
        *,
        after: int,
        timeout_seconds: float,
    ) -> LineEvent:
        if isinstance(marker, str):
            return await events.wait_for(
                marker,
                after=after,
                timeout_seconds=timeout_seconds,
            )
        return await events.wait_for_any(
            marker,
            after=after,
            timeout_seconds=timeout_seconds,
        )
