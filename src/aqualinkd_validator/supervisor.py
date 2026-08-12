from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO


@dataclass(frozen=True)
class RunResult:
    status: str
    reason: str
    child_returncode: int | None
    duration_ns: int


@dataclass(frozen=True)
class ScenarioOutcome:
    status: str
    reason: str


@dataclass(frozen=True)
class LineEvent:
    sequence: int
    offset_ns: int
    stream: str
    text: str


class OutputMonitor:
    def __init__(self, history_size: int = 4096) -> None:
        self._condition = asyncio.Condition()
        self._events: deque[LineEvent] = deque(maxlen=history_size)
        self._sequence = 0

    @property
    def cursor(self) -> int:
        return self._sequence

    def recent_events(self, *, before: int | None = None) -> list[LineEvent]:
        return [
            event for event in self._events if before is None or event.sequence < before
        ]

    async def publish(self, offset_ns: int, stream: str, text: str) -> None:
        async with self._condition:
            self._sequence += 1
            self._events.append(
                LineEvent(
                    sequence=self._sequence,
                    offset_ns=offset_ns,
                    stream=stream,
                    text=text,
                )
            )
            self._condition.notify_all()

    async def wait_for(
        self,
        predicate: str,
        *,
        after: int = 0,
        timeout_seconds: float,
    ) -> LineEvent:
        async def wait() -> LineEvent:
            cursor = after
            while True:
                async with self._condition:
                    for event in self._events:
                        if event.sequence > cursor and predicate in event.text:
                            return event
                    cursor = max(cursor, self._sequence)
                    await self._condition.wait()

        try:
            return await asyncio.wait_for(wait(), timeout_seconds)
        except TimeoutError as error:
            raise TimeoutError(
                f"timed out after {timeout_seconds:g}s waiting for log "
                f"marker: {predicate}"
            ) from error

    async def wait_for_any(
        self,
        predicates: tuple[str, ...],
        *,
        after: int = 0,
        timeout_seconds: float,
    ) -> LineEvent:
        if not predicates:
            raise ValueError("at least one log marker is required")

        async def wait() -> LineEvent:
            cursor = after
            while True:
                async with self._condition:
                    for event in self._events:
                        if event.sequence > cursor and any(
                            predicate in event.text for predicate in predicates
                        ):
                            return event
                    cursor = max(cursor, self._sequence)
                    await self._condition.wait()

        try:
            return await asyncio.wait_for(wait(), timeout_seconds)
        except TimeoutError as error:
            raise TimeoutError(
                f"timed out after {timeout_seconds:g}s waiting for any log "
                f"marker: {', '.join(predicates)}"
            ) from error

    async def wait_for_match(
        self,
        predicate: Callable[[LineEvent], bool],
        *,
        description: str,
        after: int = 0,
        timeout_seconds: float,
    ) -> LineEvent:
        async def wait() -> LineEvent:
            cursor = after
            while True:
                async with self._condition:
                    for event in self._events:
                        if event.sequence > cursor and predicate(event):
                            return event
                    cursor = max(cursor, self._sequence)
                    await self._condition.wait()

        try:
            return await asyncio.wait_for(wait(), timeout_seconds)
        except TimeoutError as error:
            raise TimeoutError(
                f"timed out after {timeout_seconds:g}s waiting for log "
                f"match: {description}"
            ) from error


class Timeline:
    def __init__(self, path: Path, start_ns: int) -> None:
        self._handle = path.open("w", encoding="utf-8")
        self._start_ns = start_ns
        self._lock = asyncio.Lock()

    @property
    def start_ns(self) -> int:
        return self._start_ns

    def offset_ns(self) -> int:
        return time.monotonic_ns() - self._start_ns

    async def write(self, kind: str, **fields: Any) -> int:
        offset_ns = self.offset_ns()
        event = {
            "offset_ns": offset_ns,
            "kind": kind,
            **fields,
        }
        async with self._lock:
            self._handle.write(json.dumps(event, separators=(",", ":")) + "\n")
            self._handle.flush()
        return offset_ns

    def close(self) -> None:
        self._handle.close()


@dataclass(frozen=True)
class ScenarioContext:
    artifact_dir: Path
    monitor: OutputMonitor
    timeline: Timeline


class Scenario(Protocol):
    async def run(self, context: ScenarioContext) -> ScenarioOutcome: ...


async def supervise(
    command: list[str],
    artifact_dir: Path,
    *,
    cwd: Path | None,
    duration_seconds: float | None,
    sample_interval_seconds: float,
    terminate_grace_seconds: float,
    scenario: Scenario | None = None,
    scenario_cleanup_seconds: float = 120.0,
) -> RunResult:
    start_ns = time.monotonic_ns()
    timeline = Timeline(artifact_dir / "timeline.jsonl", start_ns)
    monitor = OutputMonitor()
    stdout_handle = (artifact_dir / "stdout.log").open("w", encoding="utf-8")
    stderr_handle = (artifact_dir / "stderr.log").open("w", encoding="utf-8")
    metrics_handle = (artifact_dir / "metrics.jsonl").open("w", encoding="utf-8")
    process: asyncio.subprocess.Process | None = None
    readers: list[asyncio.Task[None]] = []
    sampler: asyncio.Task[None] | None = None
    scenario_task: asyncio.Task[ScenarioOutcome] | None = None
    reason = "startup_failed"
    status = "failed"
    returncode: int | None = None

    try:
        await timeline.write("process_starting", command=command)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        await timeline.write("process_started", pid=process.pid)
        assert process.stdout is not None
        assert process.stderr is not None
        readers = [
            asyncio.create_task(
                _read_stream(
                    process.stdout,
                    stdout_handle,
                    "stdout",
                    timeline,
                    monitor,
                )
            ),
            asyncio.create_task(
                _read_stream(
                    process.stderr,
                    stderr_handle,
                    "stderr",
                    timeline,
                    monitor,
                )
            ),
        ]
        sampler = asyncio.create_task(
            _sample_process(
                process.pid,
                metrics_handle,
                start_ns,
                sample_interval_seconds,
            )
        )

        if scenario is not None:
            scenario_task = asyncio.create_task(
                scenario.run(
                    ScenarioContext(
                        artifact_dir=artifact_dir,
                        monitor=monitor,
                        timeline=timeline,
                    )
                )
            )
            process_wait = asyncio.create_task(process.wait())
            done, _ = await asyncio.wait(
                {process_wait, scenario_task},
                timeout=duration_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                reason = "scenario_timeout"
                status = "failed"
                await timeline.write(
                    "scenario_timeout",
                    duration_seconds=duration_seconds,
                )
                await _cancel_task(scenario_task, scenario_cleanup_seconds)
                await _stop_process(process, timeline, terminate_grace_seconds)
                returncode = process.returncode
            elif process_wait in done:
                returncode = process_wait.result()
                reason = "child_exit_during_scenario"
                status = "failed"
                await _cancel_task(scenario_task, terminate_grace_seconds)
            else:
                try:
                    outcome = scenario_task.result()
                except Exception as error:
                    reason = "scenario_error"
                    status = "failed"
                    await timeline.write(
                        "scenario_error",
                        error=f"{type(error).__name__}: {error}",
                    )
                else:
                    reason = outcome.reason
                    status = outcome.status
                await _stop_process(process, timeline, terminate_grace_seconds)
                returncode = process.returncode
            if not process_wait.done():
                process_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await process_wait
        elif duration_seconds is None:
            returncode = await process.wait()
            status = "passed" if returncode == 0 else "failed"
            reason = "child_exit"
        else:
            try:
                returncode = await asyncio.wait_for(
                    process.wait(), timeout=duration_seconds
                )
                reason = "child_exit_before_duration"
                status = "failed"
            except TimeoutError:
                reason = "duration_elapsed"
                status = "passed"
                await timeline.write(
                    "duration_elapsed", duration_seconds=duration_seconds
                )
                await _stop_process(process, timeline, terminate_grace_seconds)
                returncode = process.returncode
    except asyncio.CancelledError:
        reason = "interrupted"
        status = "failed"
        if scenario_task is not None and not scenario_task.done():
            await _cancel_task(scenario_task, scenario_cleanup_seconds)
        if process is not None:
            await _stop_process(process, timeline, terminate_grace_seconds)
            returncode = process.returncode
        raise
    finally:
        if process is not None and process.returncode is None:
            if scenario_task is not None and not scenario_task.done():
                await _cancel_task(scenario_task, scenario_cleanup_seconds)
            await _stop_process(process, timeline, terminate_grace_seconds)
            returncode = process.returncode
        if sampler is not None:
            sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
        await timeline.write(
            "process_finished",
            status=status,
            reason=reason,
            returncode=returncode,
        )
        timeline.close()
        stdout_handle.close()
        stderr_handle.close()
        metrics_handle.close()

    return RunResult(
        status=status,
        reason=reason,
        child_returncode=returncode,
        duration_ns=time.monotonic_ns() - start_ns,
    )


async def _read_stream(
    stream: asyncio.StreamReader,
    destination: TextIO,
    name: str,
    timeline: Timeline,
    monitor: OutputMonitor,
) -> None:
    while line := await stream.readline():
        text = line.decode("utf-8", errors="replace")
        destination.write(text)
        destination.flush()
        stripped = text.rstrip("\n")
        offset_ns = await timeline.write(
            "process_output",
            stream=name,
            text=stripped,
        )
        await monitor.publish(offset_ns, name, stripped)
        _echo_process_update(name, stripped)


def _echo_process_update(stream: str, text: str) -> None:
    state_markers = (
        (
            "Waiting for Control Panel probe",
            "[STATE ] Waiting on control-panel probe",
        ),
        (
            "Got probe on ",
            "[STATE ] Control-panel probe received",
        ),
        (
            "Starting programming thread 'Init PDA'",
            "[STATE ] Init PDA task created; waiting to become active",
        ),
    )
    for marker, message in state_markers:
        if marker in text:
            print(message, flush=True)
            break

    severity_match = re.match(
        r"^(?:\d{2}:\d{2}:\d{2}(?:\.\d{3})?\s+)?"
        r"(warning|error|critical|fatal):",
        text,
        re.IGNORECASE,
    )
    severity = severity_match.group(1).lower() if severity_match else None
    if severity is not None:
        print(f"[AQUALINKD {severity.upper()}] {text}", flush=True)
    elif stream == "stderr":
        print(f"[AQUALINKD STDERR] {text}", flush=True)


async def _cancel_task(
    task: asyncio.Task[Any],
    cleanup_timeout_seconds: float,
) -> None:
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(task, cleanup_timeout_seconds)


async def _sample_process(
    pid: int,
    destination: TextIO,
    start_ns: int,
    interval_seconds: float,
) -> None:
    while True:
        sample = _read_proc_sample(pid)
        if sample is None:
            return
        sample["offset_ns"] = time.monotonic_ns() - start_ns
        destination.write(json.dumps(sample, separators=(",", ":")) + "\n")
        destination.flush()
        await asyncio.sleep(interval_seconds)


def _read_proc_sample(pid: int) -> dict[str, int] | None:
    proc = Path("/proc") / str(pid)
    try:
        stat_line = (proc / "stat").read_text(encoding="utf-8")
        closing_parenthesis = stat_line.rfind(")")
        if closing_parenthesis < 0:
            return None
        # Drop pid and the parenthesized command, which may contain spaces.
        stat_fields = stat_line[closing_parenthesis + 2 :].split()
        status = _read_key_values(proc / "status")
        io = _read_key_values(proc / "io")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None

    page_size = os.sysconf("SC_PAGE_SIZE")
    return {
        "cpu_system_ticks": int(stat_fields[12]),
        "cpu_user_ticks": int(stat_fields[11]),
        "read_bytes": int(io.get("read_bytes", 0)),
        "rss_bytes": int(stat_fields[21]) * page_size,
        "threads": int(stat_fields[17]),
        "voluntary_context_switches": int(status.get("voluntary_ctxt_switches", 0)),
        "nonvoluntary_context_switches": int(
            status.get("nonvoluntary_ctxt_switches", 0)
        ),
        "write_bytes": int(io.get("write_bytes", 0)),
    }


def _read_key_values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parts = value.strip().split()
        if parts:
            result[key] = parts[0]
    return result


async def _stop_process(
    process: asyncio.subprocess.Process,
    timeline: Timeline,
    grace_seconds: float,
) -> None:
    if process.returncode is not None:
        return
    await timeline.write("process_signal", signal="SIGTERM")
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        pass
    await timeline.write("process_signal", signal="SIGKILL")
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()
