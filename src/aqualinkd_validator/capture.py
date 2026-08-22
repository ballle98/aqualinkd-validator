from __future__ import annotations

import asyncio
import struct
import time
from typing import BinaryIO, Literal

from .interfaces import ArtifactStore, EventTimeline, SerialTransport

SerialDirection = Literal["panel_to_aqualinkd", "aqualinkd_to_panel"]

_SECTION_HEADER_BLOCK = 0x0A0D0D0A
_INTERFACE_DESCRIPTION_BLOCK = 0x00000001
_ENHANCED_PACKET_BLOCK = 0x00000006
_BYTE_ORDER_MAGIC = 0x1A2B3C4D
_LINKTYPE_USER0 = 147
_PSEUDO_HEADER = struct.Struct("<4sBBBBI")
_PSEUDO_MAGIC = b"AQV1"
_PSEUDO_VERSION = 1
_CAPTURE_POINT_PTY_MASTER = 1
_FLAG_COMPLETE = 0x01
_FLAG_FRAMING_EXACT = 0x02
_FLAG_DIRECTION_EXACT = 0x04
_FLAG_TIMING_EXACT = 0x08
_DIRECTION_VALUE = {
    "panel_to_aqualinkd": 1,
    "aqualinkd_to_panel": 2,
}


class JandyFrameBuffer:
    """Extract complete DLE/STX ... DLE/ETX frames from serial chunks."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, payload: bytes) -> tuple[bytes, ...]:
        self._buffer.extend(payload)
        frames: list[bytes] = []
        while True:
            start = self._buffer.find(b"\x10\x02")
            if start < 0:
                if self._buffer.endswith(b"\x10"):
                    self._buffer[:] = b"\x10"
                else:
                    self._buffer.clear()
                break
            if start:
                del self._buffer[:start]
            end = self._frame_end()
            if end is None:
                break
            frames.append(bytes(self._buffer[:end]))
            del self._buffer[:end]
        return tuple(frames)

    def flush(self) -> bytes:
        remaining = bytes(self._buffer)
        self._buffer.clear()
        return remaining

    def _frame_end(self) -> int | None:
        index = 2
        while index + 1 < len(self._buffer):
            if self._buffer[index] != 0x10:
                index += 1
                continue
            following = self._buffer[index + 1]
            if following == 0x10:
                index += 2
                continue
            if following == 0x03:
                return index + 2
            index += 1
        return None


class PcapngSerialWriter:
    """Write nanosecond PCAPNG packets using the AQV1 private pseudo-header."""

    def __init__(self, handle: BinaryIO, *, wall_start_ns: int) -> None:
        self._handle = handle
        self._wall_start_ns = wall_start_ns
        self._write_headers()

    def write_frame(
        self,
        payload: bytes,
        *,
        direction: SerialDirection,
        offset_ns: int,
        complete: bool = True,
    ) -> None:
        flags = _FLAG_FRAMING_EXACT | _FLAG_DIRECTION_EXACT | _FLAG_TIMING_EXACT
        if complete:
            flags |= _FLAG_COMPLETE
        pseudo_header = _PSEUDO_HEADER.pack(
            _PSEUDO_MAGIC,
            _PSEUDO_VERSION,
            _DIRECTION_VALUE[direction],
            _CAPTURE_POINT_PTY_MASTER,
            flags,
            len(payload),
        )
        packet = pseudo_header + payload
        timestamp_ns = self._wall_start_ns + offset_ns
        packet_padding = b"\0" * ((-len(packet)) % 4)
        body = struct.pack(
            "<IIIII",
            0,
            timestamp_ns >> 32,
            timestamp_ns & 0xFFFFFFFF,
            len(packet),
            len(packet),
        )
        self._write_block(_ENHANCED_PACKET_BLOCK, body + packet + packet_padding)
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def _write_headers(self) -> None:
        self._write_block(
            _SECTION_HEADER_BLOCK,
            struct.pack("<IHHq", _BYTE_ORDER_MAGIC, 1, 0, -1),
        )
        options = self._option(2, b"aqualinkd-validator PTY")
        options += self._option(
            3,
            b"AQV1 pseudo-header followed by unmodified RS485 frame bytes",
        )
        options += self._option(9, b"\x09")
        options += struct.pack("<HH", 0, 0)
        self._write_block(
            _INTERFACE_DESCRIPTION_BLOCK,
            struct.pack("<HHI", _LINKTYPE_USER0, 0, 0xFFFFFFFF) + options,
        )
        self._handle.flush()

    def _write_block(self, block_type: int, body: bytes) -> None:
        total_length = 12 + len(body)
        self._handle.write(struct.pack("<II", block_type, total_length))
        self._handle.write(body)
        self._handle.write(struct.pack("<I", total_length))

    @staticmethod
    def _option(code: int, value: bytes) -> bytes:
        return (
            struct.pack("<HH", code, len(value))
            + value
            + b"\0" * ((-len(value)) % 4)
        )


class CapturedSerialTransport:
    """Record exact PTY traffic to the shared timeline and serial PCAPNG."""

    def __init__(
        self,
        transport: SerialTransport,
        *,
        timeline: EventTimeline,
        artifacts: ArtifactStore,
        wall_start_ns: int | None = None,
    ) -> None:
        self._transport = transport
        self._timeline = timeline
        self._writer = PcapngSerialWriter(
            artifacts.open_binary("serial.pcapng"),
            wall_start_ns=(
                wall_start_ns
                if wall_start_ns is not None
                else time.time_ns() - timeline.offset_ns()
            ),
        )
        self._frames = {
            "panel_to_aqualinkd": JandyFrameBuffer(),
            "aqualinkd_to_panel": JandyFrameBuffer(),
        }
        self._lock = asyncio.Lock()
        self._closed = False
        artifacts.write_json("serial-capture.json", self.manifest())

    async def open(self) -> None:
        await self._transport.open()

    async def read(self, maximum_bytes: int = 4096) -> bytes:
        payload = await self._transport.read(maximum_bytes)
        await self._record("aqualinkd_to_panel", payload)
        return payload

    async def write(self, payload: bytes) -> None:
        await self._transport.write(payload)
        await self._record("panel_to_aqualinkd", payload)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            async with self._lock:
                for direction in (
                    "panel_to_aqualinkd",
                    "aqualinkd_to_panel",
                ):
                    remainder = self._frames[direction].flush()
                    if remainder:
                        offset = await self._timeline.write(
                            "serial_frame",
                            direction=direction,
                            capture_point="pty_master",
                            framing="incomplete",
                            bytes_hex=remainder.hex(),
                            byte_count=len(remainder),
                        )
                        self._writer.write_frame(
                            remainder,
                            direction=direction,
                            offset_ns=offset,
                            complete=False,
                        )
        finally:
            try:
                self._writer.close()
            finally:
                await self._transport.close()

    async def _record(self, direction: SerialDirection, payload: bytes) -> None:
        if not payload:
            return
        async with self._lock:
            await self._timeline.write(
                "serial_bytes",
                direction=direction,
                capture_point="pty_master",
                timing="exact_monotonic",
                bytes_hex=payload.hex(),
                byte_count=len(payload),
            )
            for frame in self._frames[direction].feed(payload):
                offset = await self._timeline.write(
                    "serial_frame",
                    direction=direction,
                    capture_point="pty_master",
                    framing="exact",
                    bytes_hex=frame.hex(),
                    byte_count=len(frame),
                )
                self._writer.write_frame(
                    frame,
                    direction=direction,
                    offset_ns=offset,
                )

    @staticmethod
    def manifest() -> dict[str, object]:
        return {
            "schema_version": 1,
            "format": "pcapng",
            "file": "serial.pcapng",
            "link_type": "LINKTYPE_USER0 (147)",
            "pseudo_header": {
                "name": "AQV1",
                "version": 1,
                "length_bytes": _PSEUDO_HEADER.size,
                "byte_order": "little-endian",
                "fields": [
                    "magic[4]",
                    "version:u8",
                    "direction:u8",
                    "capture_point:u8",
                    "status_flags:u8",
                    "frame_length:u32",
                ],
            },
            "capture_source": "validator_pty_master",
            "payload": "unmodified_rs485_frame_after_pseudo_header",
            "fidelity": {
                "bytes": "exact",
                "direction": "exact",
                "framing": "exact_for_complete_dle_frames",
                "timing": "exact_monotonic_at_frame_completion",
            },
            "replay_clock": "timeline.offset_ns",
            "pcapng_clock": "wall_start_plus_monotonic_offset_ns",
        }
