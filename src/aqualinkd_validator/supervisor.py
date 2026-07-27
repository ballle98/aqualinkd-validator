from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO


@dataclass(frozen=True)
class RunResult:
    status: str
    reason: str
    child_returncode: int | None
    duration_ns: int


class Timeline:
    def __init__(self, path: Path, start_ns: int) -> None:
        self._handle = path.open("w", encoding="utf-8")
        self._start_ns = start_ns
        self._lock = asyncio.Lock()

    async def write(self, kind: str, **fields: Any) -> None:
        event = {
            "offset_ns": time.monotonic_ns() - self._start_ns,
            "kind": kind,
            **fields,
        }
        async with self._lock:
            self._handle.write(json.dumps(event, separators=(",", ":")) + "\n")
            self._handle.flush()

    def close(self) -> None:
        self._handle.close()


async def supervise(
    command: list[str],
    artifact_dir: Path,
    *,
    cwd: Path | None,
    duration_seconds: float | None,
    sample_interval_seconds: float,
    terminate_grace_seconds: float,
) -> RunResult:
    start_ns = time.monotonic_ns()
    timeline = Timeline(artifact_dir / "timeline.jsonl", start_ns)
    stdout_handle = (artifact_dir / "stdout.log").open("w", encoding="utf-8")
    stderr_handle = (artifact_dir / "stderr.log").open("w", encoding="utf-8")
    metrics_handle = (artifact_dir / "metrics.jsonl").open("w", encoding="utf-8")
    process: asyncio.subprocess.Process | None = None
    readers: list[asyncio.Task[None]] = []
    sampler: asyncio.Task[None] | None = None
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
                _read_stream(process.stdout, stdout_handle, "stdout", timeline)
            ),
            asyncio.create_task(
                _read_stream(process.stderr, stderr_handle, "stderr", timeline)
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

        if duration_seconds is None:
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
        if process is not None:
            await _stop_process(process, timeline, terminate_grace_seconds)
            returncode = process.returncode
        raise
    finally:
        if process is not None and process.returncode is None:
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
) -> None:
    while line := await stream.readline():
        text = line.decode("utf-8", errors="replace")
        destination.write(text)
        destination.flush()
        await timeline.write("process_output", stream=name, text=text.rstrip("\n"))


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
        "voluntary_context_switches": int(
            status.get("voluntary_ctxt_switches", 0)
        ),
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
