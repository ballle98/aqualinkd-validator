from __future__ import annotations

import asyncio
import unittest

from aqualinkd_validator.protocols.rs485 import AllButtonPanelDriver
from aqualinkd_validator.testing import FakeTimeline


class ReactiveAqualinkTransport:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[bytes] = asyncio.Queue()
        self.outgoing: list[bytes] = []
        self.command_enabled = False
        self.command_sent = False
        self.is_open = False

    async def open(self) -> None:
        self.is_open = True

    async def read(self, maximum_bytes: int = 4096) -> bytes:
        del maximum_bytes
        return await self.incoming.get()

    async def write(self, payload: bytes) -> None:
        self.outgoing.append(payload)
        if payload[3] != 0x02:
            return
        command = 0
        if self.command_enabled and not self.command_sent:
            command = 0x02
            self.command_sent = True
        packet = bytes((0x10, 0x02, 0x00, 0x01, 0x00, command))
        await self.incoming.put(
            packet + bytes((sum(packet) & 0xFF, 0x10, 0x03))
        )

    async def close(self) -> None:
        self.is_open = False


class AllButtonPanelDriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_probes_polls_status_and_observes_requested_command(self) -> None:
        transport = ReactiveAqualinkTransport()
        timeline = FakeTimeline()
        driver = AllButtonPanelDriver(
            transport,
            device_id="0x0a",
            timeline=timeline,
            status_interval_seconds=0.001,
        )

        await driver.start()
        transport.command_enabled = True
        await driver.expect_command(0x02, timeout_seconds=1)
        await driver.stop()
        await transport.close()

        self.assertEqual(transport.outgoing[0].hex(), "10020a001c1003")
        status_frames = [frame for frame in transport.outgoing if frame[3] == 0x02]
        self.assertTrue(status_frames)
        self.assertEqual(status_frames[0].hex(), "10020a0200000000001e1003")
        self.assertTrue(
            any(event["kind"] == "panel_command_received" for event in timeline.events)
        )


if __name__ == "__main__":
    unittest.main()
