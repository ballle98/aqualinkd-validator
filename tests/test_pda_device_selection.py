from __future__ import annotations

import unittest

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.protocols.pda import (
    PdaDeviceSelectionConfig,
    PdaDeviceSelectionFailure,
    PdaDeviceSelector,
)


class PdaDeviceSelectorTests(unittest.TestCase):
    def test_constraints_use_api_name_buttons_and_reported_panel_size(self) -> None:
        selector, skips = self._selector(
            disabled_button_numbers=(4, 5, 8, 9, 12),
        )
        constraints = selector.configure(
            EquipmentSnapshot(
                temp_units="f",
                devices={
                    "Filter_Pump": self._switch("Filter Pump"),
                    "Spa_Mode": self._switch("Spa"),
                    "Aux_1": self._switch("Cleaner"),
                    "Aux_2": self._switch("NONE"),
                    "Aux_3": self._switch("NONE"),
                    "Aux_4": self._switch("Pool Light"),
                    "Aux_5": self._switch("Spa Light"),
                    "Aux_6": self._switch("NONE"),
                    "Aux_7": self._switch("NONE"),
                    "Solar_Heater": self._switch("NONE"),
                },
            ),
            reported_panel_size=6,
            reported_panel_combo=True,
        )

        self.assertEqual(
            {item["identifier"] for item in constraints.excluded},
            {"Aux_2", "Aux_3", "Aux_6", "Aux_7", "Solar_Heater"},
        )
        self.assertEqual(
            selector.sleep_switch(phase="devices.sleep.test"),
            "Aux_5",
        )
        self.assertEqual(
            [name for name, _ in skips],
            [
                "devices.sleep.test.Aux_2",
                "devices.sleep.test.Aux_3",
                "devices.sleep.test.Aux_6",
                "devices.sleep.test.Aux_7",
                "devices.sleep.test.Solar_Heater",
            ],
        )

    def test_sleep_selection_uses_highest_aux_on_small_pool_only_panel(
        self,
    ) -> None:
        selector, _ = self._selector()
        selector.configure(
            EquipmentSnapshot(
                temp_units="f",
                devices={
                    "Aux_1": self._switch("Aux 1"),
                    "Filter_Pump": self._switch("Filter Pump"),
                    "Aux_3": self._switch("Aux 3"),
                    "Aux_2": self._switch("Aux 2"),
                },
            ),
            reported_panel_size=4,
            reported_panel_combo=False,
        )

        self.assertEqual(
            selector.sleep_switch(phase="devices.sleep.test"),
            "Aux_3",
        )

    def test_consecutive_selection_skips_spa_and_unactionable_devices(self) -> None:
        selector, skips = self._selector(disabled_button_numbers=(4,))
        selector.configure(
            EquipmentSnapshot(
                temp_units="f",
                devices={
                    "Filter_Pump": self._switch("Filter Pump"),
                    "Spa_Mode": self._switch("Spa"),
                    "Aux_1": self._switch("Cleaner"),
                    "Aux_2": self._switch("NONE"),
                },
            ),
            reported_panel_size=6,
            reported_panel_combo=True,
        )

        self.assertEqual(
            selector.consecutive_switches(phase="devices.consecutive"),
            ("Filter_Pump", "Aux_1"),
        )
        self.assertEqual(
            [name for name, _ in skips],
            ["devices.consecutive.Spa_Mode", "devices.consecutive.Aux_2"],
        )

    def test_status_selection_defers_hydraulic_controls(self) -> None:
        selector, skips = self._selector()
        selector.configure(
            EquipmentSnapshot(
                temp_units="f",
                devices={
                    "Filter_Pump": self._switch("Filter Pump"),
                    "Spa": self._switch("Spa"),
                    "Solar_Heater": self._switch("Solar Heater"),
                    "Pool_Heater": {
                        "type": "setpoint_thermo",
                        "name": "Pool Heater",
                    },
                },
            ),
            reported_panel_size=6,
            reported_panel_combo=True,
        )

        self.assertEqual(
            selector.status_candidates(phase="devices.status_menu.setup"),
            ("Filter_Pump", "Pool_Heater"),
        )
        self.assertEqual(
            skips,
            [
                (
                    "devices.status_menu.spa_hydraulics",
                    "Left unchanged because the general status test must not "
                    "route water or demand solar heat: Solar_Heater, Spa",
                )
            ],
        )

    def test_requested_non_switch_is_rejected(self) -> None:
        selector, _ = self._selector(requested=("Pool_Heater",))
        selector.configure(
            EquipmentSnapshot(
                temp_units="f",
                devices={
                    "Pool_Heater": {
                        "type": "setpoint_thermo",
                        "name": "Pool Heater",
                    }
                },
            ),
            reported_panel_size=6,
            reported_panel_combo=True,
        )

        with self.assertRaisesRegex(
            PdaDeviceSelectionFailure,
            "Pool_Heater is not a switch device",
        ):
            selector.consecutive_switches(phase="devices.consecutive")

    @staticmethod
    def _selector(
        *,
        requested: tuple[str, ...] = (),
        disabled_button_numbers: tuple[int, ...] = (),
    ) -> tuple[PdaDeviceSelector, list[tuple[str, str]]]:
        skips: list[tuple[str, str]] = []
        return (
            PdaDeviceSelector(
                PdaDeviceSelectionConfig(
                    requested=requested,
                    disabled_button_numbers=disabled_button_numbers,
                ),
                record_skip=lambda name, reason: skips.append((name, reason)),
            ),
            skips,
        )

    @staticmethod
    def _switch(name: str) -> dict[str, object]:
        return {"type": "switch", "name": name, "int_status": 0}
