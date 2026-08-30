from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from typing import Any, TextIO

from ..capture import logical_packet_log_writer
from ..interfaces import ArtifactStore, EventTimeline, LineEvent
from ..protocols.rs485.log_capture import parse_packet_log_line


class LogicalSerialLogCapture:
    """Stream AqualinkD logical packet logs into JSONL and AQV1 PCAPNG."""

    def __init__(
        self,
        *,
        artifacts: ArtifactStore,
        timeline: EventTimeline,
        wall_start_ns: int | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._timeline = timeline
        self._jsonl: TextIO = artifacts.open_text("serial.jsonl")
        self._pcap = logical_packet_log_writer(
            artifacts.open_binary("serial.pcapng"),
            wall_start_ns=(
                wall_start_ns
                if wall_start_ns is not None
                else time.time_ns() - timeline.offset_ns()
            ),
        )
        self._counts: Counter[str] = Counter()
        self._lock = asyncio.Lock()
        self._closed = False

    async def observe(self, event: LineEvent) -> None:
        result = parse_packet_log_line(event.text)
        if result is None:
            return

        async with self._lock:
            if self._closed:
                return
            self._counts["candidates"] += 1
            if result.packet is None:
                self._counts["unparsed"] += 1
                record = {
                    "schema_version": 1,
                    "offset_ns": event.offset_ns,
                    "stream": event.stream,
                    "valid": False,
                    "error": result.error,
                    "raw_line": result.raw_line,
                }
                self._write_jsonl(record)
                await self._timeline.write(
                    "serial_packet_log_unparsed",
                    source_offset_ns=event.offset_ns,
                    stream=event.stream,
                    error=result.error,
                    raw_line=result.raw_line,
                )
                return

            packet = result.packet
            self._counts["packets"] += 1
            self._counts[f"direction:{packet.direction}"] += 1
            self._counts[f"protocol:{packet.protocol}"] += 1
            if packet.bad_packet:
                self._counts["bad_packets"] += 1
            record = {
                "schema_version": 1,
                "offset_ns": event.offset_ns,
                "stream": event.stream,
                "valid": True,
                "direction": packet.direction,
                "protocol": packet.protocol,
                "destination": f"0x{packet.destination:02x}",
                "packet_type": packet.packet_type,
                "bad_packet": packet.bad_packet,
                "data": packet.payload.hex(),
                "byte_count": len(packet.payload),
                "capture_point": "aqualinkd_packet_log",
                "timestamp_semantics": "validator_received_complete_log_line",
                "raw_line": packet.raw_line,
            }
            self._write_jsonl(record)
            self._pcap.write_frame(
                packet.payload,
                direction=packet.direction,
                offset_ns=event.offset_ns,
                complete=True,
                framing_exact=True,
                direction_exact=True,
                timing_exact=False,
            )
            await self._timeline.write(
                "serial_frame",
                source_offset_ns=event.offset_ns,
                direction=packet.direction,
                protocol=packet.protocol,
                capture_point="aqualinkd_packet_log",
                framing="logical_packet_exact",
                timing="complete_log_line_observation",
                bad_packet=packet.bad_packet,
                bytes_hex=packet.payload.hex(),
                byte_count=len(packet.payload),
            )

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._jsonl.close()
            self._pcap.close()
            self._artifacts.write_json("serial-capture.json", self.manifest())

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "format": "pcapng",
            "file": "serial.pcapng",
            "intermediate_file": "serial.jsonl",
            "link_type": "LINKTYPE_USER0 (147)",
            "pseudo_header": "AQV1",
            "capture_source": "aqualinkd_serial_debug_output",
            "capture_point": "aqualinkd_packet_log",
            "clock": "CLOCK_MONOTONIC",
            "timestamp_resolution": "nanoseconds",
            "timestamp_semantics": "validator_received_complete_log_line",
            "direction_fidelity": "exact_from_aqualinkd_read_write_label",
            "framing_fidelity": "exact_logical_packet_boundary",
            "byte_fidelity": "aqualinkd_logged_logical_packet_not_raw_wire",
            "line_buffering_method": "supervisor_async_complete_line_read",
            "aqualinkd_log_level": "DEBUG_SERIAL",
            "aqualinkd_log_filter": "none",
            "dropped_or_unparsed_records": self._counts["unparsed"],
            "counts": {
                "candidates": self._counts["candidates"],
                "packets": self._counts["packets"],
                "bad_packets": self._counts["bad_packets"],
                "unparsed": self._counts["unparsed"],
                "panel_to_aqualinkd": self._counts[
                    "direction:panel_to_aqualinkd"
                ],
                "aqualinkd_to_panel": self._counts[
                    "direction:aqualinkd_to_panel"
                ],
                "jandy": self._counts["protocol:jandy"],
                "pentair": self._counts["protocol:pentair"],
            },
        }

    def _write_jsonl(self, record: dict[str, Any]) -> None:
        self._jsonl.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._jsonl.flush()
