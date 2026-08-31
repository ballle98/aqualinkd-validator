from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from aqualinkd_validator.adapters import OutputMonitor
from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.engine import ProgrammerMarkers, RestorationSession
from aqualinkd_validator.protocols.pda.keywords import (
    PdaKeywordFailure,
    PdaKeywordMarkers,
    PdaTestcaseKeywords,
)
from aqualinkd_validator.testcases import (
    AssertDeviceStep,
    AssertLogStep,
    AssertNoLogStep,
    ExerciseHeaterStep,
    ReturnPdaHomeStep,
    SetPowerCenterModeStep,
    SetSetpointStep,
    WaitForStep,
    WaitHttpJsonStep,
    load_testcase,
)
from aqualinkd_validator.testcases import (
    TestcaseExecutor as DeclarativeExecutor,
)


class FakeActions:
    def __init__(self) -> None:
        self.device_calls: list[tuple[str, bool, float, float, float]] = []
        self.setpoint_calls: list[tuple[str, int, float, float, float]] = []
        self.state_waits: list[tuple[str, bool, float]] = []

    async def set_device(
        self,
        identifier: str,
        enabled: bool,
        *,
        phase: str,
        markers: ProgrammerMarkers,
        activation_timeout_seconds: float | None = None,
        completion_timeout_seconds: float | None = None,
        convergence_timeout_seconds: float | None = None,
    ) -> None:
        del phase, markers
        assert activation_timeout_seconds is not None
        assert completion_timeout_seconds is not None
        assert convergence_timeout_seconds is not None
        self.device_calls.append(
            (
                identifier,
                enabled,
                activation_timeout_seconds,
                completion_timeout_seconds,
                convergence_timeout_seconds,
            )
        )

    async def set_setpoint(
        self,
        identifier: str,
        value: int,
        *,
        phase: str,
        category: str,
        markers: ProgrammerMarkers,
        activation_timeout_seconds: float | None = None,
        completion_timeout_seconds: float | None = None,
        convergence_timeout_seconds: float | None = None,
    ) -> None:
        del phase, category, markers
        assert activation_timeout_seconds is not None
        assert completion_timeout_seconds is not None
        assert convergence_timeout_seconds is not None
        self.setpoint_calls.append(
            (
                identifier,
                value,
                activation_timeout_seconds,
                completion_timeout_seconds,
                convergence_timeout_seconds,
            )
        )

    async def wait_for_device_state(
        self,
        identifier: str,
        enabled: bool,
        *,
        timeout_seconds: float,
    ) -> int:
        self.state_waits.append((identifier, enabled, timeout_seconds))
        return 1


class PdaTestcaseKeywordsTests(unittest.TestCase):
    def test_example_executes_through_typed_pda_adapter(self) -> None:
        asyncio.run(self._execute_example())

    def test_original_setpoint_and_log_assertions(self) -> None:
        asyncio.run(self._setpoint_and_logs())

    def test_equipment_action_before_initialization_is_rejected(self) -> None:
        asyncio.run(self._reject_uninitialized_action())

    def test_heater_policy_owns_bounds_and_round_trip(self) -> None:
        asyncio.run(self._exercise_heater())

    def test_specialized_operations_stay_behind_typed_keywords(self) -> None:
        asyncio.run(self._specialized_operations())

    def test_spa_heating_stays_behind_typed_keyword(self) -> None:
        asyncio.run(self._spa_heating())

    def test_sleep_operations_stay_behind_typed_keywords(self) -> None:
        asyncio.run(self._sleep_operations())

    def test_home_navigation_stays_behind_typed_keyword(self) -> None:
        asyncio.run(self._home_navigation())

    def test_power_center_mode_and_status_polling_use_runtime_callbacks(self) -> None:
        asyncio.run(self._power_center_mode_and_status_polling())

    async def _execute_example(self) -> None:
        fixture = KeywordFixture()
        testcase = load_testcase(
            Path(__file__).parents[1] / "testcases" / "pda" / "filter-after-init.yaml"
        )

        result = await DeclarativeExecutor(fixture.keywords).execute(testcase)

        self.assertEqual(result.identifier, "pda.filter-after-init")
        self.assertEqual(fixture.initialize_count, 1)
        self.assertEqual(
            fixture.actions.device_calls,
            [("Filter_Pump", True, 130, 90, 10)],
        )
        self.assertEqual(
            fixture.actions.state_waits,
            [("Filter_Pump", True, 10)],
        )
        self.assertEqual(fixture.restore_timeouts, [300])

    async def _setpoint_and_logs(self) -> None:
        fixture = KeywordFixture(initialized=True)
        await fixture.keywords.set_setpoint(
            SetSetpointStep("Pool_Heater", "original", 20, 30, 40)
        )
        self.assertEqual(
            fixture.actions.setpoint_calls,
            [("Pool_Heater", 80, 20, 30, 40)],
        )

        await fixture.monitor.publish(1, "stdout", "PDA init complete")
        await fixture.keywords.assert_log(AssertLogStep("init complete", 0.1))
        await fixture.keywords.assert_no_log(AssertNoLogStep(None, "error", 0.001))
        await fixture.monitor.publish(2, "stderr", "Error: simulated failure")
        with self.assertRaisesRegex(PdaKeywordFailure, "simulated failure"):
            await fixture.keywords.assert_no_log(AssertNoLogStep(None, "error", 0.1))

    async def _reject_uninitialized_action(self) -> None:
        fixture = KeywordFixture()
        with self.assertRaisesRegex(
            PdaKeywordFailure,
            "pda.initialized must be awaited",
        ):
            await fixture.keywords.assert_device(
                # The exact assertion does not matter; initialization must win.
                fixture.assert_filter_on
            )
        await fixture.keywords.wait_for(WaitForStep("pda.initialized", 1))

    async def _exercise_heater(self) -> None:
        fixture = KeywordFixture(initialized=True)
        await fixture.keywords.exercise_heater(
            ExerciseHeaterStep("Pool_Heater", True, 20, 30, 40)
        )
        self.assertEqual(
            fixture.actions.setpoint_calls,
            [
                ("Pool_Heater", 36, 20, 30, 40),
                ("Pool_Heater", 37, 20, 30, 40),
                ("Pool_Heater", 36, 20, 30, 40),
            ],
        )
        self.assertEqual(
            fixture.actions.device_calls,
            [
                ("Pool_Heater", False, 20, 30, 40),
                ("Pool_Heater", True, 20, 30, 40),
                ("Pool_Heater", False, 20, 30, 40),
            ],
        )

    async def _specialized_operations(self) -> None:
        fixture = KeywordFixture(initialized=True)
        root = Path(__file__).parents[1] / "testcases" / "pda"
        await DeclarativeExecutor(fixture.keywords).execute(
            load_testcase(root / "equipment-status.yaml")
        )
        await DeclarativeExecutor(fixture.keywords).execute(
            load_testcase(root / "consecutive-devices.yaml")
        )
        self.assertEqual(fixture.status_verifications, 1)
        self.assertEqual(fixture.device_exercises, 1)

    async def _sleep_operations(self) -> None:
        fixture = KeywordFixture(initialized=True)
        root = Path(__file__).parents[1] / "testcases" / "pda"
        for name in (
            "sleep-cycle.yaml",
            "status-retry-command.yaml",
            "probe-transition-command.yaml",
        ):
            await DeclarativeExecutor(fixture.keywords).execute(
                load_testcase(root / name)
            )
        self.assertEqual(fixture.sleep_observations, 1)
        self.assertEqual(fixture.status_retry_exercises, 1)
        self.assertEqual(fixture.probe_transition_exercises, 1)
        self.assertEqual(fixture.restore_timeouts, [420, 420])

    async def _spa_heating(self) -> None:
        fixture = KeywordFixture(initialized=True)
        testcase = load_testcase(
            Path(__file__).parents[1]
            / "testcases"
            / "pda"
            / "spa-heating.yaml"
        )
        await DeclarativeExecutor(fixture.keywords).execute(testcase)
        self.assertEqual(fixture.spa_heating_exercises, 1)
        self.assertEqual(fixture.restore_timeouts, [600])

    async def _home_navigation(self) -> None:
        fixture = KeywordFixture(initialized=True)
        await fixture.keywords.return_pda_home(ReturnPdaHomeStep(30))
        self.assertEqual(fixture.home_timeouts, [30])

    async def _power_center_mode_and_status_polling(self) -> None:
        fixture = KeywordFixture(initialized=True, statuses=["Ready", "Service Mode"])
        await fixture.keywords.set_power_center_mode(
            SetPowerCenterModeStep("service", 2)
        )
        await fixture.keywords.wait_http_json(
            WaitHttpJsonStep("/api/status", "/status", "Service Mode", 1, 0.001, 1)
        )
        self.assertEqual(fixture.power_center_modes, ["service"])


class KeywordFixture:
    def __init__(
        self,
        *,
        initialized: bool = False,
        statuses: list[str] | None = None,
    ) -> None:
        self.monitor = OutputMonitor()
        self.actions = FakeActions()
        self.restoration = RestorationSession()
        self.initialize_count = 0
        self.restore_timeouts: list[float] = []
        self.skips: list[tuple[str, str]] = []
        self.status_verifications = 0
        self.device_exercises = 0
        self.sleep_observations = 0
        self.status_retry_exercises = 0
        self.probe_transition_exercises = 0
        self.spa_heating_exercises = 0
        self.home_timeouts: list[float] = []
        self.power_center_modes: list[str] = []
        self.statuses = list(statuses or ["Ready"])
        self.assert_filter_on = AssertDeviceStep("Filter_Pump", "on", 10)
        if initialized:
            self.restoration.capture_initial(snapshot())
        self.keywords = PdaTestcaseKeywords(
            events=self.monitor,
            actions=lambda: self.actions,
            restoration=self.restoration,
            markers=PdaKeywordMarkers(
                device=ProgrammerMarkers("device", "active", "finished"),
                setpoints={
                    "Pool_Heater": ProgrammerMarkers(
                        "setpoint",
                        "setpoint active",
                        "setpoint finished",
                    )
                },
            ),
            initialize=self.initialize,
            return_home=self.return_home,
            select_power_center_mode=self.select_power_center_mode,
            read_status=self.read_status,
            wait_for_stable=self.wait_for_stable,
            restore=self.restore,
            verify_status=self.verify_status,
            exercise_devices=self.exercise_devices,
            exercise_spa_heating=self.exercise_spa_heating,
            observe_sleep=self.observe_sleep,
            exercise_status_retry=self.exercise_status_retry,
            exercise_probe_transition=self.exercise_probe_transition,
            record_skip=lambda name, reason: self.skips.append((name, reason)),
            phase_prefix="yaml.pda.filter",
        )

    async def initialize(self) -> None:
        self.initialize_count += 1
        self.restoration.capture_initial(snapshot())

    async def wait_for_stable(
        self,
        identifiers: tuple[str, ...],
        timeout_seconds: float,
    ) -> EquipmentSnapshot:
        del identifiers, timeout_seconds
        return snapshot()

    async def return_home(self, timeout_seconds: float) -> None:
        self.home_timeouts.append(timeout_seconds)

    async def select_power_center_mode(self, mode: str) -> None:
        self.power_center_modes.append(mode)

    async def read_status(self) -> dict[str, object]:
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"status": status}

    async def restore(self, timeout_seconds: float) -> None:
        self.restore_timeouts.append(timeout_seconds)

    async def verify_status(self) -> None:
        self.status_verifications += 1

    async def exercise_devices(self) -> None:
        self.device_exercises += 1

    async def exercise_spa_heating(self) -> None:
        self.spa_heating_exercises += 1

    async def observe_sleep(self) -> None:
        self.sleep_observations += 1

    async def exercise_status_retry(self) -> None:
        self.status_retry_exercises += 1

    async def exercise_probe_transition(self) -> None:
        self.probe_transition_exercises += 1


def snapshot() -> EquipmentSnapshot:
    return EquipmentSnapshot(
        temp_units="f",
        devices={
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
                "value": "82",
            },
        },
    )


if __name__ == "__main__":
    unittest.main()
