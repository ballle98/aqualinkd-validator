from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

SerialDirection = Literal["panel_to_aqualinkd", "aqualinkd_to_panel"]
SerialProtocol = Literal["jandy", "pentair"]

_PACKET_MARKER = re.compile(
    r"\b(?P<operation>Read|Write)\s+"
    r"(?P<protocol>Jandy|Pentair)\s+packet\b",
    re.IGNORECASE,
)
_PACKET_LINE = re.compile(
    r"\b(?P<operation>Read|Write)\s+"
    r"(?P<protocol>Jandy|Pentair)\s+packet\s+"
    r"(?P<bad>BAD\s+PACKET\s+)?"
    r"To\s+0x(?P<destination>[0-9a-fA-F]{2})\s+"
    r"of\s+type\s+(?P<packet_type>.*?)\s*"
    r"\|\s*HEX:\s*(?P<hex_payload>.*)\s*$",
    re.IGNORECASE,
)
_HEX_BYTE = re.compile(r"0x([0-9a-fA-F]{2})")


@dataclass(frozen=True)
class LogicalSerialPacket:
    """One packet reported by AqualinkD's serial debug logger."""

    direction: SerialDirection
    protocol: SerialProtocol
    destination: int
    packet_type: str
    payload: bytes
    bad_packet: bool
    raw_line: str


@dataclass(frozen=True)
class PacketLogParseResult:
    """A packet-log candidate, including evidence when it cannot be parsed."""

    raw_line: str
    packet: LogicalSerialPacket | None
    error: str | None

    @property
    def valid(self) -> bool:
        return self.packet is not None


def parse_packet_log_line(line: str) -> PacketLogParseResult | None:
    """Parse one AqualinkD packet line, or return ``None`` for unrelated logs."""

    marker = _PACKET_MARKER.search(line)
    if marker is None:
        return None

    match = _PACKET_LINE.search(line)
    if match is None:
        return PacketLogParseResult(
            raw_line=line,
            packet=None,
            error="packet log line does not match the expected structure",
        )

    tokens = [token.strip() for token in match.group("hex_payload").split("|")]
    tokens = [token for token in tokens if token]
    if not tokens:
        return PacketLogParseResult(
            raw_line=line,
            packet=None,
            error="packet log line has no hexadecimal payload",
        )

    malformed = [token for token in tokens if _HEX_BYTE.fullmatch(token) is None]
    if malformed:
        return PacketLogParseResult(
            raw_line=line,
            packet=None,
            error=f"invalid hexadecimal byte token: {malformed[0]}",
        )

    operation = match.group("operation").lower()
    protocol = match.group("protocol").lower()
    packet = LogicalSerialPacket(
        direction=(
            "panel_to_aqualinkd"
            if operation == "read"
            else "aqualinkd_to_panel"
        ),
        protocol="pentair" if protocol == "pentair" else "jandy",
        destination=int(match.group("destination"), 16),
        packet_type=match.group("packet_type").strip(),
        payload=bytes(int(token[2:], 16) for token in tokens),
        bad_packet=match.group("bad") is not None,
        raw_line=line,
    )
    return PacketLogParseResult(raw_line=line, packet=packet, error=None)
