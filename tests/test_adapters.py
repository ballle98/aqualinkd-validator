from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.adapters import (
    FileArtifactStore,
    PosixSerialTransport,
    SystemMonotonicClock,
)


class AdapterTests(unittest.TestCase):
    def test_file_artifacts_write_json_and_reject_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileArtifactStore(Path(directory))
            store.write_json("nested/result.json", {"passed": True})
            self.assertEqual(
                json.loads(
                    (Path(directory) / "nested/result.json").read_text(
                        encoding="utf-8"
                    )
                ),
                {"passed": True},
            )
            with self.assertRaisesRegex(ValueError, "beneath the run root"):
                store.write_text("../escape", "unsafe")

    def test_system_clock_is_monotonic_and_sleeps(self) -> None:
        async def exercise() -> None:
            clock = SystemMonotonicClock()
            before = clock.nanoseconds()
            await clock.sleep(0)
            self.assertGreaterEqual(clock.nanoseconds(), before)
            self.assertGreater(clock.seconds(), 0)

        asyncio.run(exercise())

    def test_posix_serial_transport_round_trip_over_pty(self) -> None:
        async def exercise() -> None:
            master_fd, slave_fd = os.openpty()
            slave_path = Path(os.ttyname(slave_fd))
            os.close(slave_fd)
            transport = PosixSerialTransport(slave_path)
            try:
                await transport.open()
                await transport.write(b"validator")
                self.assertEqual(os.read(master_fd, 9), b"validator")
                os.write(master_fd, b"panel")
                self.assertEqual(await transport.read(), b"panel")
            finally:
                await transport.close()
                os.close(master_fd)

        asyncio.run(exercise())
