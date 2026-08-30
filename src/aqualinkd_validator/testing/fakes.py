from __future__ import annotations

import asyncio
import copy
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from ..domain import EquipmentSnapshot
from ..interfaces import (
    AqualinkApi,
    AquaPdaClient,
    ArtifactStore,
    EventTimeline,
    LineEvent,
    MonotonicClock,
    OrderedLogEvents,
    ProcessOutputObserverFactory,
    ProcessRunner,
    RunResult,
    Scenario,
    ScenarioContext,
    SerialTransport,
)


class FakeClock:
    def __init__(self, *, nanoseconds: int = 0) -> None:
        self._nanoseconds = nanoseconds

    def seconds(self) -> float:
        return self._nanoseconds / 1_000_000_000

    def nanoseconds(self) -> int:
        return self._nanoseconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._nanoseconds += round(seconds * 1_000_000_000)


class FakeTimeline:
    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock or FakeClock()
        self.events: list[dict[str, Any]] = []
        self.closed = False

    def offset_ns(self) -> int:
        return self.clock.nanoseconds()

    async def write(self, kind: str, **fields: Any) -> int:
        offset = self.offset_ns()
        self.events.append({"offset_ns": offset, "kind": kind, **fields})
        return offset

    def close(self) -> None:
        self.closed = True


class FakeOrderedLogEvents:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._events: list[LineEvent] = []
        self._sequence = 0

    @property
    def cursor(self) -> int:
        return self._sequence

    def recent_events(self, *, before: int | None = None) -> list[LineEvent]:
        return [
            event
            for event in self._events
            if before is None or event.sequence < before
        ]

    async def publish(self, offset_ns: int, stream: str, text: str) -> LineEvent:
        async with self._condition:
            self._sequence += 1
            event = LineEvent(self._sequence, offset_ns, stream, text)
            self._events.append(event)
            self._condition.notify_all()
            return event

    async def wait_for(
        self,
        predicate: str,
        *,
        after: int = 0,
        timeout_seconds: float,
    ) -> LineEvent:
        return await self.wait_for_match(
            lambda event: predicate in event.text,
            description=predicate,
            after=after,
            timeout_seconds=timeout_seconds,
        )

    async def wait_for_any(
        self,
        predicates: tuple[str, ...],
        *,
        after: int = 0,
        timeout_seconds: float,
    ) -> LineEvent:
        return await self.wait_for_match(
            lambda event: any(value in event.text for value in predicates),
            description=", ".join(predicates),
            after=after,
            timeout_seconds=timeout_seconds,
        )

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
                f"timed out after {timeout_seconds:g}s waiting for {description}"
            ) from error


class FakeAqualinkApi:
    def __init__(
        self,
        snapshot: EquipmentSnapshot,
        *,
        base_url: str = "http://127.0.0.1:8080",
        status: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self.snapshot = snapshot
        self.status_value = status or {}
        self.device_calls: list[tuple[str, bool]] = []
        self.setpoint_calls: list[tuple[str, int]] = []
        self.http_calls: list[tuple[str, str, str | None]] = []

    async def devices(self) -> EquipmentSnapshot:
        return EquipmentSnapshot(
            temp_units=self.snapshot.temp_units,
            devices={
                identifier: copy.deepcopy(device.raw)
                for identifier, device in self.snapshot.devices.items()
            },
        )

    async def status(self) -> dict[str, Any]:
        return copy.deepcopy(self.status_value)

    async def set_device(self, identifier: str, enabled: bool) -> None:
        self.device_calls.append((identifier, enabled))

    async def set_setpoint(self, identifier: str, value: int) -> None:
        self.setpoint_calls.append((identifier, value))

    async def request(
        self,
        method: str,
        path: str,
        *,
        value: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        del timeout_seconds
        self.http_calls.append((method, path, value))
        return "{}"


class _MemoryText(io.StringIO):
    def __init__(self, save: Callable[[str], None]) -> None:
        super().__init__()
        self._save = save

    def close(self) -> None:
        if not self.closed:
            self._save(self.getvalue())
        super().close()


class _MemoryBytes(io.BytesIO):
    def __init__(self, save: Callable[[bytes], None]) -> None:
        super().__init__()
        self._save = save

    def close(self) -> None:
        if not self.closed:
            self._save(self.getvalue())
        super().close()


class MemoryArtifactStore:
    def __init__(self) -> None:
        self._root = Path("/memory-artifacts")
        self.values: dict[str, str] = {}
        self.binary_values: dict[str, bytes] = {}

    @property
    def root(self) -> Path:
        return self._root

    def open_text(self, name: str) -> TextIO:
        return _MemoryText(lambda value: self.write_text(name, value))

    def open_binary(self, name: str) -> BinaryIO:
        return _MemoryBytes(lambda value: self.binary_values.__setitem__(name, value))

    def write_text(self, name: str, value: str) -> None:
        self.values[name] = value

    def write_json(self, name: str, value: Any) -> None:
        self.write_text(name, json.dumps(value, indent=2, sort_keys=True) + "\n")

    def json(self, name: str) -> Any:
        return json.loads(self.values[name])


class FakeSerialTransport:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[bytes] = asyncio.Queue()
        self.outgoing: list[bytes] = []
        self.is_open = False

    async def open(self) -> None:
        self.is_open = True

    async def read(self, maximum_bytes: int = 4096) -> bytes:
        if not self.is_open:
            raise RuntimeError("serial transport is not open")
        value = await self.incoming.get()
        if len(value) <= maximum_bytes:
            return value
        await self.incoming.put(value[maximum_bytes:])
        return value[:maximum_bytes]

    async def write(self, payload: bytes) -> None:
        if not self.is_open:
            raise RuntimeError("serial transport is not open")
        self.outgoing.append(bytes(payload))

    async def close(self) -> None:
        self.is_open = False


class FakeScreen:
    def __init__(self) -> None:
        self.lines: list[str] = [""] * 10
        self.highlighted_text: str | None = None

    @property
    def title(self) -> str:
        return self.lines[0].strip()


class FakeAquaPdaClient:
    def __init__(self) -> None:
        self.screen = FakeScreen()
        self.packet_count = 0
        self.screen_update_count = 0
        self.keys: list[str] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def send_key(self, key: str) -> None:
        self.keys.append(key)

    async def wait_for_packets(
        self,
        count: int,
        *,
        after: int = 0,
        timeout_seconds: float = 10.0,
    ) -> int:
        del timeout_seconds
        self.packet_count = max(self.packet_count, after + count)
        return self.packet_count

    async def wait_for_highlight_change(
        self,
        previous: str | None,
        *,
        after: int,
        timeout_seconds: float = 5.0,
    ) -> str:
        del previous, after, timeout_seconds
        if self.screen.highlighted_text is None:
            raise TimeoutError("fake screen has no highlight")
        return self.screen.highlighted_text

    async def wait_for_screen_change(
        self,
        previous: tuple[str, ...],
        *,
        after: int,
        timeout_seconds: float = 5.0,
    ) -> tuple[str, ...]:
        del previous, after, timeout_seconds
        return tuple(self.screen.lines)

    async def wait_for_screen_settle(
        self,
        *,
        after: int,
        timeout_seconds: float = 5.0,
        idle_seconds: float = 0.15,
    ) -> int:
        del timeout_seconds, idle_seconds
        return max(after, self.packet_count)

    async def close(self) -> None:
        self.connected = False


class FakeProcessRunner:
    def __init__(self, result: RunResult | None = None) -> None:
        self.result = result or RunResult("passed", "completed", 0, 0)
        self.commands: list[list[str]] = []

    async def run(
        self,
        command: list[str],
        artifact_dir: Path,
        *,
        cwd: Path | None,
        duration_seconds: float | None,
        sample_interval_seconds: float,
        terminate_grace_seconds: float,
        scenario: Scenario | None = None,
        scenario_cleanup_seconds: float = 120.0,
        output_observer_factories: tuple[ProcessOutputObserverFactory, ...] = (),
    ) -> RunResult:
        del (
            artifact_dir,
            cwd,
            duration_seconds,
            sample_interval_seconds,
            terminate_grace_seconds,
            scenario,
            scenario_cleanup_seconds,
            output_observer_factories,
        )
        self.commands.append(list(command))
        return self.result


def fake_scenario_context() -> ScenarioContext:
    return ScenarioContext(
        artifacts=MemoryArtifactStore(),
        monitor=FakeOrderedLogEvents(),
        timeline=FakeTimeline(),
    )


def _typecheck_fake_interfaces(
    api: FakeAqualinkApi,
    aquapda: FakeAquaPdaClient,
    artifacts: MemoryArtifactStore,
    clock: FakeClock,
    events: FakeOrderedLogEvents,
    process: FakeProcessRunner,
    serial: FakeSerialTransport,
    timeline: FakeTimeline,
) -> None:
    """Make strict mypy prove every fake satisfies its public boundary."""

    api_interface: AqualinkApi = api
    aquapda_interface: AquaPdaClient = aquapda
    artifact_interface: ArtifactStore = artifacts
    clock_interface: MonotonicClock = clock
    event_interface: OrderedLogEvents = events
    process_interface: ProcessRunner = process
    serial_interface: SerialTransport = serial
    timeline_interface: EventTimeline = timeline
    del (
        api_interface,
        aquapda_interface,
        artifact_interface,
        clock_interface,
        event_interface,
        process_interface,
        serial_interface,
        timeline_interface,
    )
