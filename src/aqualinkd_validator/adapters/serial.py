from __future__ import annotations

import asyncio
import contextlib
import os
import tty
from pathlib import Path


class PosixSerialTransport:
    """Nonblocking raw-byte transport for a POSIX serial device or PTY."""

    def __init__(self, path: Path, *, poll_seconds: float = 0.001) -> None:
        self._path = path
        self._poll_seconds = poll_seconds
        self._fd: int | None = None

    async def open(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(
            self._path,
            os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK,
        )
        try:
            tty.setraw(fd)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    async def read(self, maximum_bytes: int = 4096) -> bytes:
        if maximum_bytes <= 0:
            raise ValueError("maximum_bytes must be positive")
        fd = self._require_fd()
        while True:
            try:
                return os.read(fd, maximum_bytes)
            except BlockingIOError:
                await asyncio.sleep(self._poll_seconds)

    async def write(self, payload: bytes) -> None:
        fd = self._require_fd()
        view = memoryview(payload)
        while view:
            try:
                written = os.write(fd, view)
                view = view[written:]
            except BlockingIOError:
                await asyncio.sleep(self._poll_seconds)

    async def close(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _require_fd(self) -> int:
        if self._fd is None:
            raise RuntimeError("serial transport is not open")
        return self._fd
