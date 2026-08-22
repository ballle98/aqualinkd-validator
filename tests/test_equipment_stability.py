from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from aqualinkd_validator.adapters import Timeline
from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.engine import (
    EquipmentStabilityConfig,
    EquipmentStabilityFailure,
    EquipmentStabilityService,
)


class SequenceApi:
    base_url = "http://127.0.0.1:8080"

    def __init__(self, snapshots: list[EquipmentSnapshot]) -> None:
        self._snapshots = snapshots
        self.calls = 0

    async def devices(self) -> EquipmentSnapshot:
        index = min(self.calls, len(self._snapshots) - 1)
        self.calls += 1
        return self._snapshots[index]

    async def status(self) -> dict[str, Any]:
        return {}

    async def set_device(self, identifier: str, enabled: bool) -> None:
        raise AssertionError("stability observation must not mutate equipment")

    async def set_setpoint(self, identifier: str, value: int) -> None:
        raise AssertionError("stability observation must not mutate setpoints")


class EquipmentStabilityServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_through_transition_and_records_stable_state(self) -> None:
        transitioning = self._snapshot("Filter_Pump", 2, "***", "pending")
        settled = self._snapshot("Filter_Pump", 1, "on", "on")
        async with StabilityFixture(
            [transitioning, settled],
            stable_seconds=0.001,
        ) as fixture:
            result = await fixture.service.wait(
                ("Filter_Pump",),
                phase="devices.test",
                timeout_seconds=0.1,
            )

            self.assertTrue(result.devices["Filter_Pump"].enabled)
            self.assertGreaterEqual(fixture.api.calls, 3)
            self.assertEqual(
                [observation["stable"] for observation in fixture.observations],
                [False, False, True],
            )
            self.assertEqual(
                fixture.observations[0]["pending"],
                ["Filter_Pump"],
            )

    async def test_timeout_names_the_device_still_transitioning(self) -> None:
        transitioning = self._snapshot("Filter_Pump", 2, "***", "pending")
        async with StabilityFixture([transitioning]) as fixture:
            with self.assertRaisesRegex(
                EquipmentStabilityFailure,
                r"within 0.005s \(Filter_Pump\)",
            ):
                await fixture.service.wait(
                    ("Filter_Pump",),
                    phase="devices.timeout",
                    timeout_seconds=0.005,
                )

    async def test_heater_enabled_is_distinct_from_active(self) -> None:
        enabled = self._snapshot("Pool_Heater", 3, "off", "enabled")
        async with StabilityFixture([enabled], stable_seconds=0) as fixture:
            await fixture.service.wait(
                ("Pool_Heater",),
                phase="heater.enabled",
                timeout_seconds=0.1,
            )

            details = fixture.observations[-1]["devices"]["Pool_Heater"]
            self.assertTrue(details["enabled"])
            self.assertFalse(details["active"])
            self.assertFalse(details["transitioning"])

    @staticmethod
    def _snapshot(
        identifier: str,
        int_status: int,
        state: str,
        status: str,
    ) -> EquipmentSnapshot:
        return EquipmentSnapshot(
            temp_units="f",
            devices={
                identifier: {
                    "id": identifier,
                    "type": (
                        "setpoint_thermo"
                        if identifier.endswith("Heater")
                        else "switch"
                    ),
                    "int_status": int_status,
                    "state": state,
                    "status": status,
                }
            },
        )


class StabilityFixture:
    def __init__(
        self,
        snapshots: list[EquipmentSnapshot],
        *,
        stable_seconds: float = 0.01,
    ) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.timeline = Timeline(Path(self._temporary.name) / "timeline.jsonl", 0)
        self.api = SequenceApi(snapshots)
        self.observations: list[dict[str, Any]] = []
        self.service = EquipmentStabilityService(
            api=self.api,
            timeline=self.timeline,
            config=EquipmentStabilityConfig(
                stable_seconds=stable_seconds,
                poll_seconds=0.001,
            ),
            record_observation=self.observations.append,
            progress=lambda message: None,
        )

    async def __aenter__(self) -> StabilityFixture:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.timeline.close()
        self._temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
