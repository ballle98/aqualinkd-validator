from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.adapters import (
    FileArtifactStore,
    IsolatedAqualinkdRuntime,
    PanelFixture,
    PosixPtyPair,
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

    def test_owned_pty_pair_exposes_panel_side_and_slave_path(self) -> None:
        async def exercise() -> None:
            pair = PosixPtyPair.create()
            slave = PosixSerialTransport(pair.slave_path)
            try:
                await pair.panel.open()
                await slave.open()
                await pair.panel.write(b"panel")
                self.assertEqual(await slave.read(), b"panel")
                await slave.write(b"aqualinkd")
                self.assertEqual(await pair.panel.read(), b"aqualinkd")
            finally:
                await slave.close()
                await pair.close()

        asyncio.run(exercise())

    def test_isolated_runtime_generates_private_config_and_artifact(self) -> None:
        async def exercise() -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                web = root / "web"
                web.mkdir()
                artifacts = FileArtifactStore(root / "artifacts")
                runtime = IsolatedAqualinkdRuntime.create(
                    web_directory=web,
                    fixture=PanelFixture(
                        panel_type="PDA-6 Combo",
                        device_id="0x60",
                        overrides=(("pda_sleep_mode", "yes"),),
                    ),
                    artifacts=artifacts,
                )
                config_path = runtime.config_path
                try:
                    contents = config_path.read_text(encoding="utf-8")
                    self.assertIn(f"serial_port = {runtime.pty.slave_path}", contents)
                    self.assertIn(f"listen_address = {runtime.api_base_url}", contents)
                    self.assertIn("enable_scheduler = no", contents)
                    self.assertIn("pda_sleep_mode = yes", contents)
                    self.assertNotIn("mqtt_address", contents)
                    self.assertEqual(
                        (root / "artifacts/effective-aqualinkd.conf").read_text(
                            encoding="utf-8"
                        ),
                        contents,
                    )
                    port = int(runtime.api_base_url.rsplit(":", 1)[1])
                    conflict = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        with self.assertRaises(OSError):
                            conflict.bind(("127.0.0.1", port))
                        runtime.release_http_port()
                        conflict.bind(("127.0.0.1", port))
                    finally:
                        conflict.close()
                finally:
                    await runtime.close()
                self.assertFalse(config_path.exists())

        asyncio.run(exercise())

    def test_isolated_runtime_rejects_owned_config_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            web = root / "web"
            web.mkdir()
            with self.assertRaisesRegex(ValueError, "runtime-owned key serial_port"):
                IsolatedAqualinkdRuntime.create(
                    web_directory=web,
                    fixture=PanelFixture(
                        panel_type="RS-4 Combo",
                        device_id="0x0a",
                        overrides=(("serial_port", "/dev/ttyUSB0"),),
                    ),
                    artifacts=FileArtifactStore(root / "artifacts"),
                )
