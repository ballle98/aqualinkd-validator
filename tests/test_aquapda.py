from __future__ import annotations

import asyncio
import time
import unittest
from typing import Any

from aqualinkd_validator.adapters import OutputMonitor
from aqualinkd_validator.adapters.aquapda import (
    AquaPdaProtocolError,
    AquaPdaWebSocketClient,
    PdaScreen,
)
from aqualinkd_validator.protocols.pda.aquapda import (
    AquaPdaMenuWalkConfig,
    AquaPdaMenuWalker,
    AquaPdaTransportConfig,
    AquaPdaTransportValidator,
    AquaPdaValidationFailure,
)


def packet(command: int, *data: int) -> dict[str, Any]:
    return {
        "type": "simpacket",
        "simtype": "aquapda",
        "dec": [0x10, 0x02, 0x60, command, *data, 0x10, 0x03],
    }


class FakeAquaPdaClient:
    def __init__(
        self,
        events: OutputMonitor,
        *,
        corrupt: bool,
        slow: bool,
    ) -> None:
        self.events = events
        self.corrupt = corrupt
        self.slow = slow
        self.screen = PdaScreen()
        self.packet_count = 0
        self.screen_update_count = 0
        self.closed = False

    async def connect(self) -> None:
        if self.corrupt:
            await self.events.publish(
                1,
                "stderr",
                "RS Serial: Serial read bad Jandy checksum, ignoring",
            )
        if self.slow:
            await self.events.publish(
                2,
                "stdout",
                "RS Serial: Time from recv to send is 0.019 sec",
            )

    async def send_key(self, key: str) -> None:
        if key != "back":
            raise AssertionError(f"unexpected key: {key}")
        self.packet_count += 2

    async def wait_for_packets(
        self,
        count: int,
        *,
        after: int = 0,
        timeout_seconds: float = 10.0,
    ) -> int:
        self.packet_count = max(self.packet_count, after + count)
        return self.packet_count

    async def wait_for_highlight_change(
        self,
        previous: str | None,
        *,
        after: int,
        timeout_seconds: float = 5.0,
    ) -> str:
        raise AssertionError("not used by transport test")

    async def wait_for_screen_change(
        self,
        previous: tuple[str, ...],
        *,
        after: int,
        timeout_seconds: float = 5.0,
    ) -> tuple[str, ...]:
        raise AssertionError("not used by transport test")

    async def wait_for_screen_settle(
        self,
        *,
        after: int,
        timeout_seconds: float = 5.0,
        idle_seconds: float = 0.15,
    ) -> int:
        raise AssertionError("not used by transport test")

    async def close(self) -> None:
        self.closed = True


class FakeAquaPdaMenuClient:
    MENUS = {
        "INIT": [],
        "HOME": ["POOL MODE OFF", "Menu", "Equipment ON/OFF"],
        "MAIN MENU": ["HELP >", "PROGRAM >", "SET TEMP >"],
        "EQUIPMENT ON/OFF": ["FILTER PUMP OFF", "AUX 1 OFF"],
        "HELP": ["ONLY ITEM"],
        "PROGRAM": [],
        "SET TEMP": [],
    }
    TARGETS = {
        ("HOME", "Menu"): "MAIN MENU",
        ("HOME", "Equipment ON/OFF"): "EQUIPMENT ON/OFF",
        ("MAIN MENU", "HELP >"): "HELP",
        ("MAIN MENU", "PROGRAM >"): "PROGRAM",
        ("MAIN MENU", "SET TEMP >"): "SET TEMP",
    }

    def __init__(self) -> None:
        self.screen = PdaScreen()
        self.packet_count = 0
        self.screen_update_count = 0
        self._menu = "INIT"
        self._stack: list[str] = []
        self.closed = False
        self._render()

    async def connect(self) -> None:
        self.packet_count = 6

    async def send_key(self, key: str) -> None:
        if key == "back":
            if self._stack:
                self._menu = self._stack.pop()
            else:
                self._menu = "HOME"
            self._render()
        elif key == "down":
            options = self.MENUS[self._menu]
            assert self.screen.highlighted_line is not None
            current = self.screen.highlighted_line - 2
            self.screen.highlighted_line = 2 + ((current + 1) % len(options))
            self.screen_update_count += 1
        elif key == "select":
            selected = self.screen.highlighted_text
            target = self.TARGETS[(self._menu, selected)]
            self._stack.append(self._menu)
            self._menu = target
            self._render()
        else:
            raise AssertionError(f"unexpected key: {key}")
        self.packet_count += 1

    async def wait_for_packets(
        self,
        count: int,
        *,
        after: int = 0,
        timeout_seconds: float = 10.0,
    ) -> int:
        self.packet_count = max(self.packet_count, after + count)
        return self.packet_count

    async def wait_for_highlight_change(
        self,
        previous: str | None,
        *,
        after: int,
        timeout_seconds: float = 5.0,
    ) -> str:
        current = self.screen.highlighted_text
        if not current or current == previous:
            raise AquaPdaProtocolError(
                "PDA highlight did not change after a navigation key"
            )
        return current

    async def wait_for_screen_change(
        self,
        previous: tuple[str, ...],
        *,
        after: int,
        timeout_seconds: float = 5.0,
    ) -> tuple[str, ...]:
        current = tuple(self.screen.lines)
        if current == previous:
            raise AssertionError("screen did not change")
        return current

    async def wait_for_screen_settle(
        self,
        *,
        after: int,
        timeout_seconds: float = 5.0,
        idle_seconds: float = 0.15,
    ) -> int:
        if self.screen_update_count <= after:
            raise AssertionError("screen did not update")
        return self.screen_update_count

    async def close(self) -> None:
        self.closed = True

    def _render(self) -> None:
        options = self.MENUS[self._menu]
        self.screen.lines = [""] * 10
        self.screen.lines[0] = self._menu
        for index, option in enumerate(options, start=2):
            self.screen.lines[index] = option
        self.screen.highlighted_line = 2 if options else None
        self.screen_update_count += 1


class PdaScreenTests(unittest.TestCase):
    def test_reconstructs_clear_lines_highlight_and_shift(self) -> None:
        screen = PdaScreen()
        self.assertTrue(screen.apply(packet(0x09)))
        self.assertTrue(screen.apply(packet(0x04, 0, *b"MAIN MENU\x00")))
        self.assertTrue(screen.apply(packet(0x08, 0)))
        self.assertEqual(screen.lines[1], "MAIN MENU")
        self.assertEqual(screen.highlighted_line, 1)
        self.assertEqual(screen.highlighted_text, "MAIN MENU")

        screen.lines[2:5] = ["one", "two", "three"]
        self.assertTrue(screen.apply(packet(0x0F, 2, 3, 255)))
        self.assertEqual(screen.lines[2:4], ["two", "three"])


class AquaPdaTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_screen_settle_ignores_non_display_packets(self) -> None:
        client = AquaPdaWebSocketClient("http://127.0.0.1:8080")
        client.screen_update_count = 1

        async def notify_for_unrelated_packets() -> None:
            for _ in range(10):
                await asyncio.sleep(0.005)
                async with client._condition:
                    client.packet_count += 1
                    client._condition.notify_all()

        notifier = asyncio.create_task(notify_for_unrelated_packets())
        started = time.monotonic()
        settled = await client.wait_for_screen_settle(
            after=0,
            timeout_seconds=0.2,
            idle_seconds=0.02,
        )
        elapsed = time.monotonic() - started
        await notifier

        self.assertEqual(settled, 1)
        self.assertLess(elapsed, 0.05)

    async def test_transport_passes_without_corruption(self) -> None:
        await self._run_transport(corrupt=False, slow=False)

    async def test_transport_fails_on_bad_checksum(self) -> None:
        with self.assertRaisesRegex(AquaPdaValidationFailure, "BAD PACKET traffic"):
            await self._run_transport(corrupt=True, slow=False)

    async def test_transport_fails_on_slow_ack_path(self) -> None:
        with self.assertRaisesRegex(
            AquaPdaValidationFailure,
            "10ms transport budget",
        ):
            await self._run_transport(corrupt=False, slow=True)

    async def _run_transport(self, *, corrupt: bool, slow: bool) -> None:
        events = OutputMonitor()
        client = FakeAquaPdaClient(events, corrupt=corrupt, slow=slow)
        result = await AquaPdaTransportValidator(
            client=client,
            events=events,
            config=AquaPdaTransportConfig(
                packet_count=3,
                timeout_seconds=0.1,
            ),
            progress=lambda message: None,
        ).validate()
        self.assertEqual(result.report["packets_observed"], 5)
        self.assertTrue(client.closed)

    async def test_menu_walk_visits_each_structural_submenu(self) -> None:
        client = FakeAquaPdaMenuClient()
        result = await AquaPdaMenuWalker(
            client=client,
            config=AquaPdaMenuWalkConfig(timeout_seconds=0.1),
            progress=lambda message: None,
        ).walk()
        self.assertEqual(result.report["screens_visited"], 6)
        self.assertEqual(
            [screen["path"] for screen in result.report["screens"]],
            [
                ["HOME"],
                ["HOME", "Menu"],
                ["HOME", "Menu", "HELP"],
                ["HOME", "Menu", "PROGRAM"],
                ["HOME", "Menu", "SET TEMP"],
                ["HOME", "Equipment ON/OFF"],
            ],
        )
        self.assertTrue(client.closed)
