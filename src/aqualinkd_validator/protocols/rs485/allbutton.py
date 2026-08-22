from __future__ import annotations

import asyncio
import contextlib

from ...capture import JandyFrameBuffer
from ...interfaces import EventTimeline, SerialTransport

_DLE = 0x10
_STX = 0x02
_ETX = 0x03
_MASTER = 0x00
_CMD_PROBE = 0x00
_CMD_ACK = 0x01
_CMD_STATUS = 0x02
_KEY_FILTER_PUMP = 0x02


class PanelDriverFailure(RuntimeError):
    """Raised when a stateful panel driver loses its protocol exchange."""


class AllButtonPanelDriver:
    """Minimal stateful AllButton panel for probe, STATUS, and key commands."""

    def __init__(
        self,
        transport: SerialTransport,
        *,
        device_id: str,
        timeline: EventTimeline,
        status_interval_seconds: float = 0.1,
        response_timeout_seconds: float = 2.0,
    ) -> None:
        parsed_device_id = int(device_id, 0)
        if not 0x08 <= parsed_device_id <= 0x0B:
            raise ValueError(
                "allbutton panel driver requires device_id from 0x08 to 0x0b"
            )
        self._transport = transport
        self._device_id = parsed_device_id
        self._timeline = timeline
        self._status_interval_seconds = status_interval_seconds
        self._response_timeout_seconds = response_timeout_seconds
        self._frames = JandyFrameBuffer()
        self._status = bytearray(5)
        self._commands: asyncio.Queue[int] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("allbutton panel driver is already started")
        await self._transport.open()
        self._task = asyncio.create_task(self._run())
        await self._wait_for_ready()

    async def expect_command(self, command: int, *, timeout_seconds: float) -> None:
        task = self._require_task()
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise PanelDriverFailure(
                    f"allbutton command 0x{command:02x} not received within "
                    f"{timeout_seconds:g}s"
                )
            command_task = asyncio.create_task(self._commands.get())
            done, _ = await asyncio.wait(
                {command_task, task},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                command_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await command_task
                self._raise_task_failure(task)
            if command_task not in done:
                command_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await command_task
                continue
            observed = command_task.result()
            if observed == command:
                await self._timeline.write(
                    "panel_command_expected",
                    protocol="allbutton",
                    command=f"0x{command:02x}",
                )
                return
            raise PanelDriverFailure(
                f"expected allbutton command 0x{command:02x}, "
                f"received 0x{observed:02x}"
            )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        await self._send(self._frame(self._device_id, _CMD_PROBE))
        await self._exchange(
            self._frame(self._device_id, _CMD_STATUS, bytes(self._status))
        )
        self._ready.set()
        await self._timeline.write(
            "panel_driver_ready",
            protocol="allbutton",
            device_id=f"0x{self._device_id:02x}",
        )
        while True:
            ack = await self._exchange(
                self._frame(self._device_id, _CMD_STATUS, bytes(self._status))
            )
            command = ack[5]
            if command:
                await self._commands.put(command)
                await self._timeline.write(
                    "panel_command_received",
                    protocol="allbutton",
                    command=f"0x{command:02x}",
                )
                if command == _KEY_FILTER_PUMP:
                    self._status[0] ^= 0x01
            await asyncio.sleep(self._status_interval_seconds)

    async def _exchange(self, outgoing: bytes) -> bytes:
        await self._send(outgoing)
        incoming = await self._read_frame()
        if len(incoming) < 9 or incoming[2] != _MASTER or incoming[3] != _CMD_ACK:
            raise PanelDriverFailure(
                "expected AqualinkD ACK, received " + incoming.hex(" ")
            )
        if not self._valid_checksum(incoming):
            raise PanelDriverFailure(
                "AqualinkD ACK has an invalid checksum: " + incoming.hex(" ")
            )
        return incoming

    async def _send(self, outgoing: bytes) -> None:
        try:
            await asyncio.wait_for(
                self._transport.write(outgoing),
                self._response_timeout_seconds,
            )
        except TimeoutError as error:
            raise PanelDriverFailure(
                "timed out sending panel packet after "
                f"{self._response_timeout_seconds:g}s"
            ) from error

    async def _read_frame(self) -> bytes:
        async def read() -> bytes:
            while True:
                frames = self._frames.feed(await self._transport.read())
                if frames:
                    return frames[0]

        try:
            return await asyncio.wait_for(read(), self._response_timeout_seconds)
        except TimeoutError as error:
            raise PanelDriverFailure(
                "timed out waiting for AqualinkD ACK after "
                f"{self._response_timeout_seconds:g}s"
            ) from error

    async def _wait_for_ready(self) -> None:
        task = self._require_task()
        ready_task = asyncio.create_task(self._ready.wait())
        done, _ = await asyncio.wait(
            {ready_task, task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            ready_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ready_task
            self._raise_task_failure(task)
        await ready_task

    def _require_task(self) -> asyncio.Task[None]:
        if self._task is None:
            raise RuntimeError("allbutton panel driver is not started")
        return self._task

    @staticmethod
    def _raise_task_failure(task: asyncio.Task[None]) -> None:
        error = task.exception()
        if error is None:
            raise PanelDriverFailure("allbutton panel driver stopped unexpectedly")
        raise PanelDriverFailure(f"allbutton panel driver failed: {error}") from error

    @staticmethod
    def _frame(destination: int, command: int, data: bytes = b"") -> bytes:
        packet = bytes((_DLE, _STX, destination, command)) + data
        return packet + bytes((sum(packet) & 0xFF, _DLE, _ETX))

    @staticmethod
    def _valid_checksum(frame: bytes) -> bool:
        return len(frame) >= 7 and sum(frame[:-3]) & 0xFF == frame[-3]
