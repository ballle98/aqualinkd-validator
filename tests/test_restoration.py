from __future__ import annotations

import asyncio
import unittest

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.engine import RestorationSession


def snapshot(
    states: dict[str, tuple[bool, int | None]],
) -> EquipmentSnapshot:
    return EquipmentSnapshot(
        temp_units="f",
        devices={
            identifier: {
                "id": identifier,
                "type": "setpoint_thermo" if setpoint is not None else "switch",
                "int_status": "3" if enabled and setpoint is not None else int(enabled),
                "state": "on" if enabled else "off",
                "status": "enabled" if enabled and setpoint is not None else (
                    "on" if enabled else "off"
                ),
                **({"spvalue": str(setpoint)} if setpoint is not None else {}),
            }
            for identifier, (enabled, setpoint) in states.items()
        },
    )


class RestorationSessionTests(unittest.TestCase):
    def test_disabled_dependencies_restore_heater_first_and_pump_last(self) -> None:
        asyncio.run(self._restore_disabled_dependencies())

    def test_enabled_dependencies_restore_pump_before_mode_and_heater(self) -> None:
        asyncio.run(self._restore_enabled_dependencies())

    def test_setpoint_is_restored_before_device_state(self) -> None:
        asyncio.run(self._restore_setpoint_before_state())

    def test_failure_retains_pending_mutations_for_retry(self) -> None:
        asyncio.run(self._retain_failed_mutation())

    def test_cancellation_propagates_and_retains_pending_mutations(self) -> None:
        asyncio.run(self._retain_cancelled_mutation())

    def test_duplicate_request_state_is_owned_by_session(self) -> None:
        session = RestorationSession()
        self.assertIsNone(session.requested_state("Spa_Mode"))
        session.mark_requested_state("Spa_Mode", False)
        self.assertFalse(session.requested_state("Spa_Mode"))
        session.forget_requested_state("Spa_Mode")
        self.assertIsNone(session.requested_state("Spa_Mode"))

    async def _restore_disabled_dependencies(self) -> None:
        initial = snapshot(
            {
                "Filter_Pump": (False, None),
                "Spa_Mode": (False, None),
                "Aux_1": (False, None),
                "Pool_Heater": (False, None),
            }
        )
        session = RestorationSession()
        session.capture_initial(initial)
        for identifier in initial.devices:
            session.touch_device(identifier)
        restored: list[str] = []

        async def restore_device(identifier: str, enabled: bool) -> None:
            self.assertFalse(enabled)
            restored.append(identifier)

        result = await session.restore(
            read_snapshot=self._unexpected_snapshot_read,
            restore_setpoint=self._unexpected_setpoint_restore,
            restore_device=restore_device,
        )

        self.assertTrue(result.passed)
        self.assertEqual(
            restored,
            ["Pool_Heater", "Aux_1", "Spa_Mode", "Filter_Pump"],
        )
        self.assertFalse(session.has_pending_mutations)

    async def _restore_enabled_dependencies(self) -> None:
        initial = snapshot(
            {
                "Pool_Heater": (True, None),
                "Aux_1": (True, None),
                "Spa_Mode": (True, None),
                "Filter_Pump": (True, None),
            }
        )
        session = RestorationSession()
        session.capture_initial(initial)
        for identifier in initial.devices:
            session.touch_device(identifier)
        restored: list[str] = []

        async def restore_device(identifier: str, enabled: bool) -> None:
            self.assertTrue(enabled)
            restored.append(identifier)

        result = await session.restore(
            read_snapshot=self._unexpected_snapshot_read,
            restore_setpoint=self._unexpected_setpoint_restore,
            restore_device=restore_device,
        )

        self.assertTrue(result.passed)
        self.assertEqual(
            restored,
            ["Filter_Pump", "Spa_Mode", "Aux_1", "Pool_Heater"],
        )

    async def _restore_setpoint_before_state(self) -> None:
        initial = snapshot({"Pool_Heater": (False, 80)})
        current = snapshot({"Pool_Heater": (True, 85)})
        session = RestorationSession()
        session.capture_initial(initial)
        session.touch_setpoint("Pool_Heater")
        session.touch_device("Pool_Heater")
        actions: list[tuple[str, int | bool]] = []

        async def read_snapshot() -> EquipmentSnapshot:
            return current

        async def restore_setpoint(identifier: str, value: int) -> None:
            actions.append((identifier, value))

        async def restore_device(identifier: str, enabled: bool) -> None:
            actions.append((identifier, enabled))

        result = await session.restore(
            read_snapshot=read_snapshot,
            restore_setpoint=restore_setpoint,
            restore_device=restore_device,
        )

        self.assertTrue(result.passed)
        self.assertEqual(actions, [("Pool_Heater", 80), ("Pool_Heater", False)])

    async def _retain_failed_mutation(self) -> None:
        initial = snapshot({"Spa_Mode": (False, None)})
        session = RestorationSession()
        session.capture_initial(initial)
        session.touch_device("Spa_Mode")

        async def fail_restore(identifier: str, enabled: bool) -> None:
            raise TimeoutError(f"{identifier} did not become {enabled}")

        result = await session.restore(
            read_snapshot=self._unexpected_snapshot_read,
            restore_setpoint=self._unexpected_setpoint_restore,
            restore_device=fail_restore,
        )

        self.assertFalse(result.passed)
        self.assertIn("Spa_Mode state", result.errors[0])
        self.assertTrue(session.has_pending_mutations)

    async def _retain_cancelled_mutation(self) -> None:
        initial = snapshot({"Filter_Pump": (False, None)})
        session = RestorationSession()
        session.capture_initial(initial)
        session.touch_device("Filter_Pump")

        async def cancel_restore(identifier: str, enabled: bool) -> None:
            del identifier, enabled
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await session.restore(
                read_snapshot=self._unexpected_snapshot_read,
                restore_setpoint=self._unexpected_setpoint_restore,
                restore_device=cancel_restore,
            )
        self.assertTrue(session.has_pending_mutations)

    async def _unexpected_snapshot_read(self) -> EquipmentSnapshot:
        raise AssertionError("setpoint snapshot should not be read")

    async def _unexpected_setpoint_restore(
        self,
        identifier: str,
        value: int,
    ) -> None:
        raise AssertionError(f"unexpected setpoint restore: {identifier}={value}")


if __name__ == "__main__":
    unittest.main()
