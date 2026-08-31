from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any

from aqualinkd_validator.adapters import OutputMonitor, Timeline
from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.engine import (
    EquipmentActions,
    EquipmentActionTimeouts,
    ProgrammerMarkers,
    RestorationSession,
)
from aqualinkd_validator.protocols.pda import PdaProgrammerObserver


class FakeApi:
    def __init__(self, monitor: OutputMonitor, timeline: Timeline) -> None:
        self._monitor = monitor
        self._timeline = timeline
        self._devices: dict[str, dict[str, Any]] = {
            "Filter_Pump": {
                "id": "Filter_Pump",
                "type": "switch",
                "int_status": "0",
                "state": "off",
                "status": "off",
            },
            "Pool_Heater": {
                "id": "Pool_Heater",
                "type": "setpoint_thermo",
                "int_status": "3",
                "state": "off",
                "status": "enabled",
                "spvalue": "80",
            },
            "Temperature/Air": {
                "id": "Temperature/Air",
                "type": "value",
                "value": "34.0",
            },
        }
        self.device_calls: list[tuple[str, bool]] = []
        self.setpoint_calls: list[tuple[str, int]] = []

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:8080"

    async def devices(self) -> EquipmentSnapshot:
        return EquipmentSnapshot(
            temp_units="f",
            devices=copy.deepcopy(self._devices),
        )

    async def status(self) -> dict[str, Any]:
        return {}

    async def set_device(self, identifier: str, enabled: bool) -> None:
        self.device_calls.append((identifier, enabled))
        self._devices[identifier].update(
            int_status=str(int(enabled)),
            state="on" if enabled else "off",
            status="on" if enabled else "off",
        )
        await self._publish("device active")
        await self._publish("device finished")

    async def set_setpoint(self, identifier: str, value: int) -> None:
        self.setpoint_calls.append((identifier, value))
        self._devices[identifier]["spvalue"] = str(value)
        await self._publish("setpoint active")
        await self._publish("setpoint finished")

    async def _publish(self, text: str) -> None:
        await self._monitor.publish(
            self._timeline.offset_ns(),
            "stdout",
            text,
        )


class EquipmentActionsTests(unittest.TestCase):
    def test_device_action_correlates_logs_state_and_measurement(self) -> None:
        asyncio.run(self._device_action())

    def test_already_converged_device_is_skipped(self) -> None:
        asyncio.run(self._skip_converged_device())

    def test_setpoint_action_tracks_restoration_and_measurement(self) -> None:
        asyncio.run(self._setpoint_action())

    def test_wait_for_device_value_accepts_numeric_api_value(self) -> None:
        asyncio.run(self._wait_for_device_value())

    async def _device_action(self) -> None:
        async with ActionFixtureContext() as fixture:
            await fixture.actions.set_device(
                "Filter_Pump",
                True,
                phase="devices.fast",
                markers=ProgrammerMarkers(
                    "Switch PDA device on/off",
                    "device active",
                    "device finished",
                ),
            )

            self.assertEqual(fixture.api.device_calls, [("Filter_Pump", True)])
            self.assertTrue(fixture.restoration.has_pending_mutations)
            self.assertEqual(fixture.measurements[0]["status"], "passed")
            self.assertEqual(fixture.measurements[0]["target"], "Filter_Pump")
            self.assertIsNotNone(
                fixture.measurements[0]["state_observed_offset_ns"]
            )

    async def _skip_converged_device(self) -> None:
        async with ActionFixtureContext() as fixture:
            await fixture.actions.set_device(
                "Filter_Pump",
                False,
                phase="devices.fast",
                markers=ProgrammerMarkers(
                    "Switch PDA device on/off",
                    "device active",
                    "device finished",
                ),
            )

            self.assertEqual(fixture.api.device_calls, [])
            self.assertEqual(
                fixture.skips,
                [
                    (
                        "devices.fast.Filter_Pump.off",
                        "Device is already in the requested state",
                    )
                ],
            )

    async def _setpoint_action(self) -> None:
        async with ActionFixtureContext() as fixture:
            await fixture.actions.set_setpoint(
                "Pool_Heater",
                82,
                phase="heater.raise",
                category="heater_setpoint",
                markers=ProgrammerMarkers(
                    "Set PDA Pool Heater",
                    "setpoint active",
                    "setpoint finished",
                ),
            )

            self.assertEqual(fixture.api.setpoint_calls, [("Pool_Heater", 82)])
            self.assertTrue(fixture.restoration.has_pending_mutations)
            self.assertEqual(fixture.measurements[0]["category"], "heater_setpoint")
            self.assertEqual(fixture.measurements[0]["requested_value"], 82)

    async def _wait_for_device_value(self) -> None:
        async with ActionFixtureContext() as fixture:
            observed = await fixture.actions.wait_for_device_value(
                "Temperature/Air",
                34,
                timeout_seconds=0.1,
            )

            self.assertIsInstance(observed, int)


class ActionFixture:
    def __init__(self, directory: str) -> None:
        self.timeline = Timeline(Path(directory) / "timeline.jsonl", 0)
        self.monitor = OutputMonitor()
        self.api = FakeApi(self.monitor, self.timeline)
        self.restoration = RestorationSession()
        self.measurements: list[dict[str, Any]] = []
        self.skips: list[tuple[str, str]] = []
        self.actions = EquipmentActions(
            api=self.api,
            events=self.monitor,
            timeline=self.timeline,
            programmer=PdaProgrammerObserver(),
            restoration=self.restoration,
            timeouts=EquipmentActionTimeouts(0.1, 0.1, 0.1, 0.1),
            wait_for_stable=self._stable_snapshot,
            record_measurement=self.measurements.append,
            record_skip=lambda name, reason: self.skips.append((name, reason)),
            poll_seconds=0.001,
        )

    async def initialize(self) -> None:
        self.restoration.capture_initial(await self.api.devices())

    async def _stable_snapshot(
        self,
        identifier: str,
        phase: str,
        initial: EquipmentSnapshot,
        timeout_seconds: float,
    ) -> EquipmentSnapshot:
        del identifier, phase, timeout_seconds
        return initial


class ActionFixtureContext:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.fixture = ActionFixture(self._temporary.name)

    async def __aenter__(self) -> ActionFixture:
        await self.fixture.initialize()
        return self.fixture

    async def __aexit__(self, *args: object) -> None:
        self.fixture.timeline.close()
        self._temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
