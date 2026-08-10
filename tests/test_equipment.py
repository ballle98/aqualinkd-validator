from __future__ import annotations

import unittest

from aqualinkd_validator.domain import (
    DeviceState,
    EquipmentSnapshot,
    EquipmentStateError,
)


class DeviceStateTests(unittest.TestCase):
    def test_heater_enablement_is_distinct_from_active_heating(self) -> None:
        enabled = DeviceState(
            {
                "id": "Pool_Heater",
                "name": "Pool Heater",
                "type": "setpoint_thermo",
                "int_status": "3",
                "state": "off",
                "status": "enabled",
                "spvalue": "80",
            }
        )
        active = DeviceState({**enabled.raw, "int_status": "1", "state": "on"})

        self.assertTrue(enabled.enabled)
        self.assertFalse(enabled.active)
        self.assertFalse(enabled.transitioning)
        self.assertEqual(enabled.setpoint, 80)
        self.assertEqual(enabled.requested_state_label(True), "enabled")
        self.assertTrue(active.enabled)
        self.assertTrue(active.active)

    def test_flashing_and_delayed_states_are_transitions(self) -> None:
        for int_status, status in (("2", "flash"), ("4", "pending")):
            with self.subTest(int_status=int_status):
                state = DeviceState(
                    {
                        "id": "Filter_Pump",
                        "type": "switch",
                        "int_status": int_status,
                        "state": "off",
                        "status": status,
                    }
                )
                self.assertTrue(state.enabled)
                self.assertFalse(state.active)
                self.assertTrue(state.transitioning)

    def test_invalid_status_has_device_context(self) -> None:
        state = DeviceState({"id": "Aux_1", "int_status": "not-a-number"})

        with self.assertRaisesRegex(EquipmentStateError, "Aux_1"):
            _ = state.int_status

    def test_snapshot_normalizes_raw_devices(self) -> None:
        snapshot = EquipmentSnapshot(
            temp_units="f",
            devices={
                "Filter_Pump": {
                    "id": "Filter_Pump",
                    "type": "switch",
                    "int_status": "0",
                }
            },
        )

        self.assertIsInstance(snapshot.devices["Filter_Pump"], DeviceState)
        self.assertFalse(snapshot.devices["Filter_Pump"].enabled)


if __name__ == "__main__":
    unittest.main()
