from __future__ import annotations

import unittest
from typing import Any

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.engine import RestorationSession
from aqualinkd_validator.protocols.pda import PdaProgrammerObserver
from aqualinkd_validator.protocols.pda.equipment_control import (
    PdaEquipmentControlConfig,
    PdaEquipmentController,
)
from aqualinkd_validator.testing import (
    FakeAqualinkApi,
    FakeOrderedLogEvents,
    FakeTimeline,
)


class PdaEquipmentControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_binds_stability_observation_to_run_dependencies(self) -> None:
        snapshot = EquipmentSnapshot(
            temp_units="F",
            devices={
                "Filter_Pump": {
                    "id": "Filter_Pump",
                    "type": "switch",
                    "int_status": 0,
                    "state": "off",
                    "status": "off",
                }
            },
        )
        timeline = FakeTimeline()
        observations: list[dict[str, Any]] = []
        controller = PdaEquipmentController(
            api=FakeAqualinkApi(snapshot),
            events=FakeOrderedLogEvents(),
            timeline=timeline,
            programmer=PdaProgrammerObserver(),
            restoration=RestorationSession(),
            config=PdaEquipmentControlConfig(
                activation_timeout_seconds=1,
                action_timeout_seconds=1,
                state_timeout_seconds=1,
                restoration_timeout_seconds=1,
                poll_seconds=0.001,
                stable_seconds=0,
            ),
            record_measurement=lambda value: None,
            record_observation=observations.append,
            record_skip=lambda name, reason: None,
            progress=lambda message: None,
        )

        result = await controller.wait_for_stable(
            ("Filter_Pump",),
            phase="test.stable",
            timeout_seconds=1,
        )

        self.assertFalse(result.devices["Filter_Pump"].enabled)
        self.assertTrue(observations[-1]["stable"])
        self.assertEqual(observations[-1]["phase"], "test.stable")


if __name__ == "__main__":
    unittest.main()
