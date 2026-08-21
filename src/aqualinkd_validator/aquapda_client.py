from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit


class AquaPdaProtocolError(RuntimeError):
    """Raised when the AquaPDA WebSocket protocol is not usable."""


class AquaPdaClient(Protocol):
    screen: PdaScreen
    packet_count: int
    screen_update_count: int

    async def connect(self) -> None: ...

    async def send_key(self, key: str) -> None: ...

    async def wait_for_packets(
        self,
        count: int,
        *,
        after: int = 0,
        timeout_seconds: float = 10.0,
    ) -> int: ...

    async def wait_for_highlight_change(
        self,
        previous: str | None,
        *,
        after: int,
        timeout_seconds: float = 5.0,
    ) -> str: ...

    async def wait_for_screen_change(
        self,
        previous: tuple[str, ...],
        *,
        after: int,
        timeout_seconds: float = 5.0,
    ) -> tuple[str, ...]: ...

    async def wait_for_screen_settle(
        self,
        *,
        after: int,
        timeout_seconds: float = 5.0,
        idle_seconds: float = 0.15,
    ) -> int: ...

    async def close(self) -> None: ...


class JsonWebSocket:
    """Small RFC 6455 client for AqualinkD's unencrypted local WebSocket."""

    def __init__(self, base_url: str, timeout_seconds: float = 5.0) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname is None:
            raise ValueError("AquaPDA validation requires an http AqualinkD base URL")
        self._host = parsed.hostname
        self._port = parsed.port or 80
        self._path = parsed.path or "/"
        self._timeout_seconds = timeout_seconds
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            self._timeout_seconds,
        )
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host = self._host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if self._port != 80:
            host = f"{host}:{self._port}"
        request = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(request.encode("ascii"))
        await asyncio.wait_for(writer.drain(), self._timeout_seconds)
        response = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), self._timeout_seconds
        )
        lines = response.decode("iso-8859-1").split("\r\n")
        if not lines or " 101 " not in lines[0]:
            writer.close()
            raise AquaPdaProtocolError(
                f"WebSocket upgrade failed: {lines[0] if lines else response!r}"
            )
        headers = {
            name.strip().lower(): value.strip()
            for line in lines[1:]
            if ":" in line
            for name, value in (line.split(":", 1),)
        }
        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            writer.close()
            raise AquaPdaProtocolError(
                "WebSocket server returned an invalid accept key"
            )
        self._reader = reader
        self._writer = writer

    async def send_json(self, payload: dict[str, Any]) -> None:
        await self._send_frame(
            0x1,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )

    async def receive_json(self) -> dict[str, Any]:
        while True:
            opcode, payload = await self._read_frame()
            if opcode == 0x1:
                value = json.loads(payload.decode("utf-8"))
                if not isinstance(value, dict):
                    raise AquaPdaProtocolError(
                        "WebSocket JSON message is not an object"
                    )
                return value
            if opcode == 0x8:
                raise AquaPdaProtocolError("AqualinkD closed the WebSocket")
            if opcode == 0x9:
                await self._send_frame(0xA, payload)

    async def close(self) -> None:
        writer = self._writer
        if writer is None:
            return
        with contextlib.suppress(OSError, AquaPdaProtocolError):
            await self._send_frame(0x8, b"\x03\xe8")
        writer.close()
        with contextlib.suppress(OSError, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), self._timeout_seconds)
        self._reader = None
        self._writer = None

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._writer is None:
            raise AquaPdaProtocolError("WebSocket is not connected")
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((0x80 | opcode, 0xFE)) + struct.pack("!H", length)
        else:
            header = bytes((0x80 | opcode, 0xFF)) + struct.pack("!Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._writer.write(header + mask + masked)
        await asyncio.wait_for(self._writer.drain(), self._timeout_seconds)

    async def _read_frame(self) -> tuple[int, bytes]:
        if self._reader is None:
            raise AquaPdaProtocolError("WebSocket is not connected")
        header = await self._reader.readexactly(2)
        final = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", await self._reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self._reader.readexactly(8))[0]
        mask = await self._reader.readexactly(4) if masked else b""
        payload = await self._reader.readexactly(length)
        if masked:
            payload = bytes(
                byte ^ mask[index % 4] for index, byte in enumerate(payload)
            )
        if not final or opcode == 0x0:
            raise AquaPdaProtocolError(
                "fragmented WebSocket messages are not supported"
            )
        return opcode, payload


@dataclass
class PdaScreen:
    lines: list[str] = field(default_factory=lambda: [""] * 10)
    highlighted_line: int | None = None

    @property
    def title(self) -> str:
        return self.lines[0].strip()

    @property
    def highlighted_text(self) -> str | None:
        if self.highlighted_line is None:
            return None
        return self.lines[self.highlighted_line].strip()

    def apply(self, packet: dict[str, Any]) -> bool:
        if packet.get("type") != "simpacket":
            return False
        if packet.get("simtype") != "aquapda":
            raise AquaPdaProtocolError(
                f"expected aquapda packet, received {packet.get('simtype')!r}"
            )
        raw = packet.get("dec")
        if not isinstance(raw, list) or len(raw) < 4:
            raise AquaPdaProtocolError("simpacket has no usable dec array")
        data = [int(value) for value in raw]
        command = data[3]
        changed = False
        if command == 0x09:
            self.lines = [""] * 10
            self.highlighted_line = None
            changed = True
        elif command == 0x04 and len(data) >= 6:
            line = self._line_number(data[4])
            if line is not None:
                self.lines[line] = self._packet_text(data)
                changed = True
        elif command == 0x08 and len(data) >= 5:
            self.highlighted_line = self._line_number(data[4])
            changed = True
        elif command == 0x10:
            changed = True
        elif command == 0x0F and len(data) >= 7:
            self._shift_lines(data[4], data[5], data[6])
            changed = True
        return changed

    @staticmethod
    def _line_number(value: int) -> int | None:
        if value == 0:
            return 1
        if value == 64:
            return 0
        if value == 130:
            return 2
        return value if 0 <= value < 10 else None

    @staticmethod
    def _packet_text(data: list[int]) -> str:
        chars: list[str] = []
        for value in data[5:21]:
            if value == 0:
                break
            if 31 <= value <= 127:
                chars.append(chr(value))
            elif value == 223:
                chars.append("°")
        return "".join(chars)

    def _shift_lines(self, first: int, last: int, direction: int) -> None:
        first = max(0, first)
        last = min(8, last)
        if direction == 255:
            for index in range(first, last + 1):
                self.lines[index] = self.lines[index + 1]
        elif direction == 1:
            for index in range(last, first - 1, -1):
                self.lines[index] = self.lines[index - 1]


class AquaPdaWebSocketClient:
    KEYS = {
        "page_down": 0x01,
        "back": 0x02,
        "page_up": 0x03,
        "select": 0x04,
        "down": 0x05,
        "up": 0x06,
    }

    def __init__(self, base_url: str, timeout_seconds: float = 5.0) -> None:
        self._socket = JsonWebSocket(base_url, timeout_seconds)
        self.screen = PdaScreen()
        self.packet_count = 0
        self.screen_update_count = 0
        self._condition = asyncio.Condition()
        self._reader_task: asyncio.Task[None] | None = None
        self._reader_error: BaseException | None = None

    async def connect(self) -> None:
        await self._socket.connect()
        self._reader_task = asyncio.create_task(self._read_messages())
        await self._socket.send_json({"uri": "simulator/aquapda"})

    async def send_key(self, key: str) -> None:
        try:
            value = self.KEYS[key]
        except KeyError as error:
            raise ValueError(f"unknown AquaPDA key: {key}") from error
        await self._socket.send_json({"uri": "simcmd", "value": value})

    async def wait_for_packets(
        self,
        count: int,
        *,
        after: int = 0,
        timeout_seconds: float = 10.0,
    ) -> int:
        async def wait() -> int:
            async with self._condition:
                while self.packet_count - after < count:
                    if self._reader_error is not None:
                        raise AquaPdaProtocolError(
                            f"AquaPDA receive loop failed: {self._reader_error}"
                        ) from self._reader_error
                    await self._condition.wait()
                return self.packet_count

        try:
            return await asyncio.wait_for(wait(), timeout_seconds)
        except TimeoutError as error:
            raise AquaPdaProtocolError(
                f"received only {self.packet_count - after} AquaPDA packet(s); "
                f"expected {count} within {timeout_seconds:g}s"
            ) from error

    async def wait_for_highlight_change(
        self,
        previous: str | None,
        *,
        after: int,
        timeout_seconds: float = 5.0,
    ) -> str:
        async def wait() -> str:
            async with self._condition:
                while True:
                    current = self.screen.highlighted_text
                    if self.packet_count > after and current and current != previous:
                        return current
                    if self._reader_error is not None:
                        raise AquaPdaProtocolError(
                            f"AquaPDA receive loop failed: {self._reader_error}"
                        ) from self._reader_error
                    await self._condition.wait()

        try:
            return await asyncio.wait_for(wait(), timeout_seconds)
        except TimeoutError as error:
            raise AquaPdaProtocolError(
                "PDA highlight did not change after a navigation key"
            ) from error

    async def wait_for_screen_change(
        self,
        previous: tuple[str, ...],
        *,
        after: int,
        timeout_seconds: float = 5.0,
    ) -> tuple[str, ...]:
        async def wait() -> tuple[str, ...]:
            async with self._condition:
                while True:
                    current = tuple(self.screen.lines)
                    if self.packet_count > after and current != previous:
                        return current
                    if self._reader_error is not None:
                        raise AquaPdaProtocolError(
                            f"AquaPDA receive loop failed: {self._reader_error}"
                        ) from self._reader_error
                    await self._condition.wait()

        try:
            return await asyncio.wait_for(wait(), timeout_seconds)
        except TimeoutError as error:
            raise AquaPdaProtocolError(
                "PDA screen did not change after a navigation key"
            ) from error

    async def wait_for_screen_settle(
        self,
        *,
        after: int,
        timeout_seconds: float = 5.0,
        idle_seconds: float = 0.15,
    ) -> int:
        deadline = time.monotonic() + timeout_seconds
        settled_count = self.screen_update_count
        quiet_since: float | None = None
        async with self._condition:
            while True:
                if self._reader_error is not None:
                    raise AquaPdaProtocolError(
                        f"AquaPDA receive loop failed: {self._reader_error}"
                    ) from self._reader_error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AquaPdaProtocolError(
                        "PDA screen did not settle after a navigation key"
                    )
                if self.screen_update_count > after:
                    if self.screen_update_count != settled_count:
                        settled_count = self.screen_update_count
                        quiet_since = time.monotonic()
                    elif quiet_since is None:
                        quiet_since = time.monotonic()
                    quiet_remaining = idle_seconds - (time.monotonic() - quiet_since)
                    if quiet_remaining <= 0:
                        return self.screen_update_count
                    try:
                        await asyncio.wait_for(
                            self._condition.wait(),
                            min(quiet_remaining, remaining),
                        )
                    except TimeoutError:
                        if time.monotonic() - quiet_since >= idle_seconds:
                            return self.screen_update_count
                else:
                    try:
                        await asyncio.wait_for(self._condition.wait(), remaining)
                    except TimeoutError as error:
                        raise AquaPdaProtocolError(
                            "PDA screen did not update after a navigation key"
                        ) from error

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        await self._socket.close()

    async def _read_messages(self) -> None:
        try:
            while True:
                message = await self._socket.receive_json()
                if message.get("type") == "simpacket":
                    screen_changed = self.screen.apply(message)
                    async with self._condition:
                        self.packet_count += 1
                        if screen_changed:
                            self.screen_update_count += 1
                        self._condition.notify_all()
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            async with self._condition:
                self._reader_error = error
                self._condition.notify_all()
