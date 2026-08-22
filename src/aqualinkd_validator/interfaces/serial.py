from __future__ import annotations

from typing import Protocol


class SerialTransport(Protocol):
    """Bidirectional byte transport for a live or emulated RS485 endpoint."""

    async def open(self) -> None: ...

    async def read(self, maximum_bytes: int = 4096) -> bytes: ...

    async def write(self, payload: bytes) -> None: ...

    async def close(self) -> None: ...
