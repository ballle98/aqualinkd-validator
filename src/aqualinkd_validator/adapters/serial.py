from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import tty
from dataclasses import dataclass
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
        self.close_now()

    def close_now(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _require_fd(self) -> int:
        if self._fd is None:
            raise RuntimeError("serial transport is not open")
        return self._fd


class _OwnedFileDescriptorTransport:
    def __init__(self, fd: int, *, poll_seconds: float = 0.001) -> None:
        self._fd: int | None = fd
        self._poll_seconds = poll_seconds

    async def open(self) -> None:
        if self._fd is None:
            raise RuntimeError("PTY panel endpoint is closed")

    async def read(self, maximum_bytes: int = 4096) -> bytes:
        if maximum_bytes <= 0:
            raise ValueError("maximum_bytes must be positive")
        while True:
            try:
                return os.read(self._require_fd(), maximum_bytes)
            except BlockingIOError:
                await asyncio.sleep(self._poll_seconds)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                # A PTY master reports EIO while no process has the slave open.
                await asyncio.sleep(self._poll_seconds)

    async def write(self, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            try:
                written = os.write(self._require_fd(), view)
                view = view[written:]
            except BlockingIOError:
                await asyncio.sleep(self._poll_seconds)

    async def close(self) -> None:
        self.close_now()

    def close_now(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _require_fd(self) -> int:
        if self._fd is None:
            raise RuntimeError("PTY panel endpoint is closed")
        return self._fd


@dataclass(frozen=True)
class PosixPtyPair:
    """Owned PTY whose master is the emulated-panel transport."""

    slave_path: Path
    panel: _OwnedFileDescriptorTransport

    @classmethod
    def create(cls) -> PosixPtyPair:
        master_fd, slave_fd = os.openpty()
        try:
            tty.setraw(master_fd)
            tty.setraw(slave_fd)
            os.set_blocking(master_fd, False)
            slave_path = Path(os.ttyname(slave_fd))
        except BaseException:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        os.close(slave_fd)
        return cls(
            slave_path=slave_path,
            panel=_OwnedFileDescriptorTransport(master_fd),
        )

    async def close(self) -> None:
        await self.panel.close()

    def close_now(self) -> None:
        self.panel.close_now()
