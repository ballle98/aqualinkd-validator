from __future__ import annotations

import asyncio
import re

from ..interfaces import EventTimeline, SerialTransport


class SerialActionFailure(RuntimeError):
    """Raised when bounded serial I/O cannot satisfy a testcase step."""


def parse_hex_bytes(value: str) -> bytes:
    """Parse readable hex bytes separated by whitespace, commas, or pipes."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("serial bytes must not be empty")
    tokens = re.sub(r"[\s,|:-]+", " ", stripped).split()
    parsed = bytearray()
    for token in tokens:
        normalized = token[2:] if token.casefold().startswith("0x") else token
        if (
            not normalized
            or len(normalized) % 2 != 0
            or re.fullmatch(r"[0-9a-fA-F]+", normalized) is None
        ):
            raise ValueError(f"invalid serial byte {token!r} in {value!r}")
        parsed.extend(bytes.fromhex(normalized))
    return bytes(parsed)


class SerialActions:
    """Bounded exact-byte operations over a captured panel transport."""

    def __init__(
        self,
        transport: SerialTransport,
        *,
        timeline: EventTimeline,
    ) -> None:
        self._transport = transport
        self._timeline = timeline
        self._received = bytearray()

    async def open(self) -> None:
        await self._transport.open()

    async def send(self, payload: bytes, *, timeout_seconds: float) -> None:
        if not payload:
            raise ValueError("serial payload must not be empty")
        self._validate_timeout(timeout_seconds)
        await self._timeline.write(
            "serial_send_requested",
            bytes_hex=payload.hex(),
            byte_count=len(payload),
            timeout_seconds=timeout_seconds,
        )
        try:
            await asyncio.wait_for(
                self._transport.write(payload),
                timeout=timeout_seconds,
            )
        except TimeoutError as error:
            raise SerialActionFailure(
                f"serial send timed out after {timeout_seconds:g}s"
            ) from error
        await self._timeline.write(
            "serial_send_completed",
            bytes_hex=payload.hex(),
            byte_count=len(payload),
        )

    async def expect_exact(
        self,
        expected: bytes,
        *,
        timeout_seconds: float,
    ) -> bytes:
        if not expected:
            raise ValueError("expected serial bytes must not be empty")
        self._validate_timeout(timeout_seconds)
        await self._timeline.write(
            "serial_expect_requested",
            bytes_hex=expected.hex(),
            byte_count=len(expected),
            timeout_seconds=timeout_seconds,
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while len(self._received) < len(expected):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SerialActionFailure(
                    self._timeout_message(expected, timeout_seconds)
                )
            try:
                chunk = await asyncio.wait_for(
                    self._transport.read(),
                    timeout=remaining,
                )
            except TimeoutError as error:
                raise SerialActionFailure(
                    self._timeout_message(expected, timeout_seconds)
                ) from error
            if not chunk:
                continue
            self._received.extend(chunk)
            comparable = min(len(self._received), len(expected))
            if self._received[:comparable] != expected[:comparable]:
                observed = bytes(self._received[:comparable])
                raise SerialActionFailure(
                    "serial bytes differed: expected "
                    f"{expected.hex()}, observed {observed.hex()}"
                )

        observed = bytes(self._received[: len(expected)])
        del self._received[: len(expected)]
        await self._timeline.write(
            "serial_expect_matched",
            bytes_hex=observed.hex(),
            byte_count=len(observed),
        )
        return observed

    async def close(self) -> None:
        await self._transport.close()

    def _timeout_message(self, expected: bytes, timeout_seconds: float) -> str:
        return (
            f"serial expectation timed out after {timeout_seconds:g}s: "
            f"expected {expected.hex()}, observed {bytes(self._received).hex()}"
        )

    @staticmethod
    def _validate_timeout(timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("serial timeout must be positive")
