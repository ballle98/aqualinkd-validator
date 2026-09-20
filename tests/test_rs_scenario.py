from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from aqualinkd_validator.adapters import FileArtifactStore, OutputMonitor, Timeline
from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.interfaces import ScenarioContext
from aqualinkd_validator.rs_scenario import RsScenarioRuntime


class FakeRsApi:
    def __init__(self, context: ScenarioContext) -> None:
        self._context = context
        self._devices: dict[str, dict[str, Any]] = {
            identifier: {
                "id": identifier,
                "name": name,
                "type": "switch",
                "int_status": "0",
                "state": "off",
                "status": "off",
            }
            for identifier, name in (
                ("Filter_Pump", "Filter Pump"),
                ("Aux_1", "Aux 1"),
                ("Aux_2", "Aux 2"),
                ("Aux_B1", "Aux B1"),
            )
        }
        self.calls: list[tuple[str, bool]] = []

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:8080"

    async def devices(self) -> EquipmentSnapshot:
        return EquipmentSnapshot(temp_units="f", devices=copy.deepcopy(self._devices))

    async def status(self) -> dict[str, Any]:
        return {}

    async def set_device(self, identifier: str, enabled: bool) -> None:
        self.calls.append((identifier, enabled))
        self._devices[identifier].update(
            int_status=str(int(enabled)),
            state="on" if enabled else "off",
            status="on" if enabled else "off",
        )
        await self._context.monitor.publish(
            self._context.timeline.offset_ns(),
            "stdout",
            "AqualinkD: Send Jandy packet To 0x00 of type Ack w/ Command",
        )
        await self._context.monitor.publish(
            self._context.timeline.offset_ns(),
            "stdout",
            "RS Serial: Read Jandy packet To 0x0b of type Status",
        )

    async def set_setpoint(self, identifier: str, value: int) -> None:
        raise AssertionError((identifier, value))


class RsScenarioRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_basic_suite_toggles_and_restores_rs_devices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor = OutputMonitor()
            timeline = Timeline(root / "timeline.jsonl", time.monotonic_ns())
            context = ScenarioContext(
                artifacts=FileArtifactStore(root),
                monitor=monitor,
                timeline=timeline,
            )
            await monitor.publish(1, "stdout", "Starting Aqualink Daemon v2.4.1 !")
            await monitor.publish(
                2,
                "stdout",
                "Panel set to RS-16 Combo (Pool & Spa)",
            )
            await monitor.publish(
                3,
                "stdout",
                "Starting communication with Control Panel",
            )
            await monitor.publish(
                4,
                "stdout",
                "Thread 1 (Get Freeze protect) finished",
            )
            api = FakeRsApi(context)
            runtime = RsScenarioRuntime(
                suite_name="rs-fast",
                api_base_url_override="http://127.0.0.1:8080",
                api_factory=lambda endpoint: api,
                init_timeout_seconds=1,
                state_timeout_seconds=1,
            )

            outcome = await runtime.run(context)
            timeline.close()

            self.assertEqual(outcome.status, "passed")
            self.assertEqual(
                api.calls,
                [
                    ("Filter_Pump", True),
                    ("Filter_Pump", False),
                    ("Aux_1", True),
                    ("Aux_1", False),
                ],
            )
            report = json.loads((root / "scenario.json").read_text())
            self.assertTrue(report["safe_to_continue"])
            self.assertEqual(
                [case["status"] for case in report["cases"]],
                ["passed", "passed", "passed"],
            )
            self.assertEqual(report["restoration"]["status"], "passed")
            self.assertEqual(
                report["restoration"]["actions"],
                [
                    {"target": "Filter_Pump", "property": "state", "value": False},
                    {"target": "Aux_1", "property": "state", "value": False},
                ],
            )

    async def test_full_suite_exercises_every_discovered_auxiliary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor = OutputMonitor()
            timeline = Timeline(root / "timeline.jsonl", time.monotonic_ns())
            context = ScenarioContext(
                artifacts=FileArtifactStore(root),
                monitor=monitor,
                timeline=timeline,
            )
            for sequence, text in enumerate(
                (
                    "Starting Aqualink Daemon v2.4.1 !",
                    "Panel set to RS-8 Combo (Pool & Spa)",
                    "Starting communication with Control Panel",
                    "Thread 1 (Get Freeze protect) finished",
                ),
                start=1,
            ):
                await monitor.publish(sequence, "stdout", text)
            api = FakeRsApi(context)
            runtime = RsScenarioRuntime(
                suite_name="rs-full",
                case_ids=("rs-initialization", "rs-filter-pump", "rs-auxiliaries"),
                api_base_url_override="http://127.0.0.1:8080",
                api_factory=lambda endpoint: api,
                init_timeout_seconds=1,
                state_timeout_seconds=1,
            )

            outcome = await runtime.run(context)
            timeline.close()

            self.assertEqual(outcome.status, "passed")
            self.assertEqual(
                api.calls,
                [
                    ("Filter_Pump", True),
                    ("Filter_Pump", False),
                    ("Aux_1", True),
                    ("Aux_1", False),
                    ("Aux_2", True),
                    ("Aux_2", False),
                    ("Aux_B1", True),
                    ("Aux_B1", False),
                ],
            )
            report = json.loads((root / "scenario.json").read_text())
            self.assertEqual(
                report["full_suite_devices"], ["Aux_1", "Aux_2", "Aux_B1"]
            )


if __name__ == "__main__":
    unittest.main()
