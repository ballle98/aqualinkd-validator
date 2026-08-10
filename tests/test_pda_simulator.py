from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.pda_scenario import (
    PdaLivePanelScenario,
    PdaScenarioConfig,
    ScenarioFailure,
)
from aqualinkd_validator.pda_simulator import (
    AquaPdaSimulator,
    PdaScreen,
    SimulatorProtocolError,
)
from aqualinkd_validator.supervisor import OutputMonitor, ScenarioContext, Timeline


def packet(command: int, *data: int) -> dict[str, Any]:
    return {
        "type": "simpacket",
        "simtype": "aquapda",
        "dec": [0x10, 0x02, 0x60, command, *data, 0x10, 0x03],
    }


class FakeApi:
    base_url = "http://127.0.0.1:8080"

    async def devices(self) -> EquipmentSnapshot:
        return EquipmentSnapshot(temp_units="f", devices={})

    async def status(self) -> dict[str, Any]:
        return {}

    async def set_device(self, identifier: str, enabled: bool) -> None:
        raise AssertionError("transport test must not change equipment")

    async def set_setpoint(self, identifier: str, value: int) -> None:
        raise AssertionError("transport test must not change setpoints")


class FakeSimulator:
    def __init__(
        self,
        context: ScenarioContext,
        *,
        corrupt: bool,
        slow: bool,
    ) -> None:
        self.context = context
        self.corrupt = corrupt
        self.slow = slow
        self.screen = PdaScreen()
        self.packet_count = 0
        self.screen_update_count = 0
        self.closed = False

    async def connect(self) -> None:
        if self.corrupt:
            await self.context.monitor.publish(
                1,
                "stderr",
                "RS Serial: Serial read bad Jandy checksum, ignoring",
            )
        if self.slow:
            await self.context.monitor.publish(
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


class FakeMenuSimulator:
    MENUS = {
        "INIT": [],
        "HOME": ["POOL MODE OFF", "MENU", "EQUIPMENT ON/OFF"],
        "MAIN MENU": ["HELP >", "PROGRAM >", "SET TEMP >"],
        "EQUIPMENT ON/OFF": ["FILTER PUMP OFF", "AUX 1 OFF"],
        "HELP": ["ONLY ITEM"],
        "PROGRAM": [],
        "SET TEMP": [],
    }
    TARGETS = {
        ("HOME", "MENU"): "MAIN MENU",
        ("HOME", "EQUIPMENT ON/OFF"): "EQUIPMENT ON/OFF",
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
            raise SimulatorProtocolError(
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


class SimulatorTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_screen_settle_ignores_non_display_packets(self) -> None:
        simulator = AquaPdaSimulator("http://127.0.0.1:8080")
        simulator.screen_update_count = 1

        async def notify_for_unrelated_packets() -> None:
            for _ in range(10):
                await asyncio.sleep(0.005)
                async with simulator._condition:
                    simulator.packet_count += 1
                    simulator._condition.notify_all()

        notifier = asyncio.create_task(notify_for_unrelated_packets())
        started = time.monotonic()
        settled = await simulator.wait_for_screen_settle(
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
        with self.assertRaisesRegex(ScenarioFailure, "BAD PACKET traffic"):
            await self._run_transport(corrupt=True, slow=False)

    async def test_transport_fails_on_slow_ack_path(self) -> None:
        with self.assertRaisesRegex(ScenarioFailure, "10ms transport budget"):
            await self._run_transport(corrupt=False, slow=True)

    async def _run_transport(self, *, corrupt: bool, slow: bool) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            timeline = Timeline(artifact_dir / "timeline.jsonl", time.monotonic_ns())
            context = ScenarioContext(
                artifact_dir=artifact_dir,
                monitor=OutputMonitor(),
                timeline=timeline,
            )
            simulators: list[FakeSimulator] = []

            def factory(base_url: str) -> FakeSimulator:
                self.assertEqual(base_url, FakeApi.base_url)
                simulator = FakeSimulator(
                    context,
                    corrupt=corrupt,
                    slow=slow,
                )
                simulators.append(simulator)
                return simulator

            scenario = PdaLivePanelScenario(
                FakeApi(),
                PdaScenarioConfig(
                    simulator_packet_count=3,
                    simulator_timeout_seconds=0.1,
                ),
                simulator_factory=factory,
            )
            try:
                await scenario._test_simulator_transport(context)
            finally:
                timeline.close()
            self.assertTrue(simulators[0].closed)

    async def test_menu_walk_visits_each_structural_submenu(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            timeline = Timeline(
                artifact_dir / "timeline.jsonl", time.monotonic_ns()
            )
            context = ScenarioContext(
                artifact_dir=artifact_dir,
                monitor=OutputMonitor(),
                timeline=timeline,
            )
            simulator = FakeMenuSimulator()
            scenario = PdaLivePanelScenario(
                FakeApi(),
                PdaScenarioConfig(simulator_timeout_seconds=0.1),
                simulator_factory=lambda base_url: simulator,
            )
            try:
                await scenario._test_menu_walk(context)
            finally:
                timeline.close()

            report = scenario._report["menu_walk"]
            self.assertEqual(report["screens_visited"], 6)
            self.assertEqual(
                [screen["path"] for screen in report["screens"]],
                [
                    ["HOME"],
                    ["HOME", "MENU"],
                    ["HOME", "MENU", "HELP"],
                    ["HOME", "MENU", "PROGRAM"],
                    ["HOME", "MENU", "SET TEMP"],
                    ["HOME", "EQUIPMENT ON/OFF"],
                ],
            )
            self.assertTrue(simulator.closed)
