from __future__ import annotations

import asyncio
import unittest
from typing import Any

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.protocols.pda import (
    PdaEquipmentStatusFailure,
    PdaEquipmentStatusService,
)
from aqualinkd_validator.supervisor import LineEvent, OutputMonitor


class PdaEquipmentStatusServiceTests(unittest.TestCase):
    def test_complete_multi_page_loop_reconciles_heater_and_swg(self) -> None:
        asyncio.run(self._reconcile_complete_loop())

    def test_missing_later_page_and_incorrect_api_state_are_both_reported(
        self,
    ) -> None:
        asyncio.run(self._reject_incomplete_loop())

    async def _reconcile_complete_loop(self) -> None:
        snapshot = self._snapshot(
            filter_status=1,
            aux_status=1,
            include_heater=True,
            include_swg=True,
        )
        events = OutputMonitor()
        progress: list[str] = []
        service = self._service(events, snapshot, progress)
        lines = (
            "PDA Menu Line 1 = AIR         POOL",
            "*** Pass Equiptment msg 'EQUIPMENT STATUS'",
            "Found Status for Filter Pump = 'Filter Pump ON'",
            "Found Status for Pool Heater = 'Pool Heater ENA'",
            "PDA Start new Equipment loop",
            "Pool Hearter is enabled",
            "*** Pass Equiptment msg '  AquaPure 35%'",
            "AquaPure = 35",
            "PDA End Equiptment loop",
            "Start new equipment cycle bitmask 0x0003",
        )
        for offset, line in enumerate(lines, start=1):
            await events.publish(offset, "stdout", line)

        loop = await service.wait_for_complete_loop(after=0)
        result = await service.verify(
            initial_snapshot=snapshot,
            controls=("Filter_Pump", "Pool_Heater"),
            events=loop.events,
            setup_states={"Filter_Pump": {"enabled": True}},
        )

        self.assertEqual(result.report["missing_devices"], [])
        self.assertEqual(result.report["incorrect_api_states"], [])
        self.assertEqual(
            result.report["swg"],
            {
                "present": True,
                "observed": True,
                "percent": 35,
                "api_percent": 35,
            },
        )
        heater = result.report["heater_states"]["Pool_Heater"]
        self.assertTrue(heater["pda_enabled"])
        self.assertFalse(heater["pda_active"])
        self.assertLess(loop.started.sequence, loop.finished.sequence)
        self.assertTrue(
            any("loop completed and reconciled" in message for message in progress)
        )

    async def _reject_incomplete_loop(self) -> None:
        initial = self._snapshot(filter_status=1, aux_status=1)
        reconciled = self._snapshot(filter_status=1, aux_status=0)
        service = self._service(OutputMonitor(), reconciled, [])
        events = (
            self._event(1, "*** Pass Equiptment msg 'EQUIPMENT STATUS'"),
            self._event(2, "Found Status for Filter Pump = 'Filter Pump ON'"),
        )

        with self.assertRaises(PdaEquipmentStatusFailure) as raised:
            await service.verify(
                initial_snapshot=initial,
                controls=("Filter_Pump", "Aux_1"),
                events=events,
            )

        self.assertIn("missing status entries for Aux_1", str(raised.exception))
        self.assertIn(
            "API marked expected-on devices off after status processing: Aux_1",
            str(raised.exception),
        )
        assert raised.exception.result is not None
        self.assertEqual(raised.exception.result.report["missing_devices"], ["Aux_1"])
        self.assertEqual(
            raised.exception.result.report["incorrect_api_states"],
            ["Aux_1"],
        )

    @staticmethod
    def _service(
        events: OutputMonitor,
        snapshot: EquipmentSnapshot,
        progress: list[str],
    ) -> PdaEquipmentStatusService:
        async def stable(
            identifiers: tuple[str, ...],
            phase: str,
            timeout: float,
        ) -> EquipmentSnapshot:
            del identifiers, phase, timeout
            return snapshot

        return PdaEquipmentStatusService(
            events=events,
            wait_for_stable=stable,
            status_timeout_seconds=0.1,
            state_timeout_seconds=0.1,
            progress=progress.append,
        )

    @staticmethod
    def _event(sequence: int, text: str) -> LineEvent:
        return LineEvent(
            sequence=sequence,
            offset_ns=sequence,
            stream="stdout",
            text=text,
        )

    @staticmethod
    def _snapshot(
        *,
        filter_status: int,
        aux_status: int,
        include_heater: bool = False,
        include_swg: bool = False,
    ) -> EquipmentSnapshot:
        devices: dict[str, dict[str, Any]] = {
            "Filter_Pump": {
                "id": "Filter_Pump",
                "name": "Filter Pump",
                "type": "switch",
                "int_status": str(filter_status),
                "state": "on" if filter_status else "off",
                "status": "on" if filter_status else "off",
            },
            "Aux_1": {
                "id": "Aux_1",
                "name": "Cleaner",
                "type": "switch",
                "int_status": str(aux_status),
                "state": "on" if aux_status else "off",
                "status": "on" if aux_status else "off",
            },
        }
        if include_heater:
            devices["Pool_Heater"] = {
                "id": "Pool_Heater",
                "name": "Pool Heater",
                "type": "setpoint_thermo",
                "int_status": "3",
                "state": "off",
                "status": "enabled",
                "spvalue": "36",
            }
        if include_swg:
            devices["SWG"] = {
                "id": "SWG",
                "name": "AquaPure",
                "type": "setpoint_swg",
                "int_status": "1",
                "state": "on",
                "status": "on",
                "spvalue": "35",
            }
        return EquipmentSnapshot(temp_units="f", devices=devices)


if __name__ == "__main__":
    unittest.main()
