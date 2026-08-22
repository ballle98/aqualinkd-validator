from __future__ import annotations

import copy
import unittest
from typing import Any

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.protocols.pda.equipment_setup import (
    POOL_HEATER,
    SPA_HEATER,
    PdaEquipmentSetupConfig,
    PdaEquipmentSetupFailure,
    PdaEquipmentStatusSetup,
)
from aqualinkd_validator.supervisor import OutputMonitor


class SetupApi:
    base_url = "http://127.0.0.1:8080"

    def __init__(self, *, activate_pool_heater: bool = False) -> None:
        self.activate_pool_heater = activate_pool_heater
        self.devices_by_id: dict[str, dict[str, Any]] = {
            "Filter_Pump": self._device("Filter_Pump", "switch", 0),
            "Spa": self._device("Spa", "switch", 0),
            POOL_HEATER: self._device(
                POOL_HEATER,
                "setpoint_thermo",
                0,
                spvalue=80,
                value=82,
            ),
            SPA_HEATER: self._device(
                SPA_HEATER,
                "setpoint_thermo",
                0,
                spvalue=100,
                value=-999,
            ),
        }

    async def devices(self) -> EquipmentSnapshot:
        return EquipmentSnapshot(
            temp_units="f",
            devices=copy.deepcopy(self.devices_by_id),
        )

    async def status(self) -> dict[str, Any]:
        return {}

    async def set_device(self, identifier: str, enabled: bool) -> None:
        raise AssertionError("test fixture uses injected actions")

    async def set_setpoint(self, identifier: str, value: int) -> None:
        raise AssertionError("test fixture uses injected actions")

    @staticmethod
    def _device(
        identifier: str,
        kind: str,
        int_status: int,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "id": identifier,
            "name": identifier.replace("_", " "),
            "type": kind,
            "int_status": int_status,
            "state": "on" if int_status == 1 else "off",
            "status": "on" if int_status == 1 else "off",
            **extra,
        }


class PdaEquipmentStatusSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_lowers_heaters_and_enables_controls_without_active_heat(
        self,
    ) -> None:
        async with SetupFixture() as fixture:
            initial = await fixture.api.devices()
            result = await fixture.service.prepare(
                initial,
                ("Filter_Pump", POOL_HEATER, SPA_HEATER),
            )

            self.assertEqual(
                result.controls,
                ("Filter_Pump", POOL_HEATER, SPA_HEATER),
            )
            self.assertEqual(
                fixture.setpoint_calls,
                [
                    (POOL_HEATER, 36, "devices.status_menu.safe_setpoint"),
                    (SPA_HEATER, 36, "devices.status_menu.safe_setpoint"),
                ],
            )
            self.assertTrue(result.states[POOL_HEATER]["enabled"])
            self.assertFalse(result.states[POOL_HEATER]["active"])
            self.assertEqual(
                fixture.stable_phases,
                [
                    "devices.status_menu.precondition",
                    "devices.status_menu.setup_complete",
                ],
            )

    async def test_active_heater_at_start_is_skipped(self) -> None:
        async with SetupFixture() as fixture:
            fixture.api.devices_by_id[POOL_HEATER].update(
                int_status=1,
                state="on",
                status="on",
            )
            initial = await fixture.api.devices()
            result = await fixture.service.prepare(initial, (POOL_HEATER,))

            self.assertEqual(result.controls, ())
            self.assertEqual(fixture.device_calls, [])
            self.assertEqual(
                fixture.skips,
                [
                    (
                        "devices.status_menu.setup.Pool_Heater",
                        "Heater was already actively heating at test start",
                    )
                ],
            )

    async def test_unexpected_active_heat_is_disabled_and_fails(self) -> None:
        async with SetupFixture(activate_pool_heater=True) as fixture:
            initial = await fixture.api.devices()
            with self.assertRaisesRegex(
                PdaEquipmentSetupFailure,
                "unexpectedly activated: Pool_Heater",
            ):
                await fixture.service.prepare(
                    initial,
                    ("Filter_Pump", POOL_HEATER),
                )

            self.assertIn(
                (
                    POOL_HEATER,
                    False,
                    "devices.status_menu.emergency_heat_disable",
                    0.2,
                ),
                fixture.device_calls,
            )


class SetupFixture:
    def __init__(self, *, activate_pool_heater: bool = False) -> None:
        self.api = SetupApi(activate_pool_heater=activate_pool_heater)
        self.events = OutputMonitor()
        self.device_calls: list[tuple[str, bool, str, float]] = []
        self.setpoint_calls: list[tuple[str, int, str]] = []
        self.stable_phases: list[str] = []
        self.skips: list[tuple[str, str]] = []
        self.service = PdaEquipmentStatusSetup(
            api=self.api,
            events=self.events,
            config=PdaEquipmentSetupConfig(
                status_timeout_seconds=0.1,
                restoration_timeout_seconds=0.2,
                poll_seconds=0.001,
            ),
            set_device=self._set_device,
            set_setpoint=self._set_setpoint,
            wait_for_stable=self._wait_for_stable,
            record_skip=lambda name, reason: self.skips.append((name, reason)),
            progress=lambda message: None,
        )

    async def __aenter__(self) -> SetupFixture:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def _set_device(
        self,
        identifier: str,
        enabled: bool,
        phase: str,
        timeout: float,
    ) -> None:
        self.device_calls.append((identifier, enabled, phase, timeout))
        device = self.api.devices_by_id[identifier]
        active = (
            identifier == POOL_HEATER
            and enabled
            and self.api.activate_pool_heater
        )
        int_status = 1 if active else (3 if enabled else 0)
        device.update(
            int_status=int_status,
            state="on" if active else "off",
            status="on" if active else ("enabled" if enabled else "off"),
        )
        if identifier == "Filter_Pump" and enabled:
            await self.events.publish(1, "stdout", "PDA Menu Line 1 = AIR")

    async def _set_setpoint(
        self,
        identifier: str,
        value: int,
        phase: str,
    ) -> None:
        self.setpoint_calls.append((identifier, value, phase))
        self.api.devices_by_id[identifier]["spvalue"] = value

    async def _wait_for_stable(
        self,
        identifiers: Any,
        phase: str,
        timeout: float,
    ) -> EquipmentSnapshot:
        del identifiers, timeout
        self.stable_phases.append(phase)
        return await self.api.devices()
