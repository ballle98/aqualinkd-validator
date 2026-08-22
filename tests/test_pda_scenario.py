from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.pda_scenario import (
    DEVICE_ACTIVE,
    DEVICE_FINISHED,
    INIT_ACTIVE,
    INIT_FINISHED,
    LEGACY_POOL_HEATER_SETPOINT_ACTIVE,
    LEGACY_POOL_HEATER_SETPOINT_FINISHED,
    POOL_HEATER,
    POOL_HEATER_SETPOINT_ACTIVE,
    POOL_HEATER_SETPOINT_FINISHED,
    SPA_HEATER,
    SPA_HEATER_SETPOINT_ACTIVE,
    SPA_HEATER_SETPOINT_FINISHED,
    PdaScenarioConfig,
    PdaScenarioRuntime,
    ScenarioFailure,
)
from aqualinkd_validator.run_targets import RuntimeCaseId
from aqualinkd_validator.supervisor import (
    OutputMonitor,
    ScenarioContext,
    Timeline,
)
from aqualinkd_validator.testcases import load_testcase, load_testcase_suite


class FakeApi:
    def __init__(
        self,
        context: ScenarioContext,
        panel_time_offset_minutes: int = 0,
        base_url: str = "http://127.0.0.1:8080",
        legacy_heater_markers: bool = False,
        log_prefix: str = "",
    ) -> None:
        self._context = context
        self._panel_time_offset_minutes = panel_time_offset_minutes
        self._base_url = base_url
        self._legacy_heater_markers = legacy_heater_markers
        self._log_prefix = log_prefix
        self._devices: dict[str, dict[str, Any]] = {
            "Filter_Pump": {
                "id": "Filter_Pump",
                "name": "Filter Pump",
                "type": "switch",
                "int_status": "0",
                "state": "off",
                "status": "off",
            },
            "Aux_1": {
                "id": "Aux_1",
                "name": "Cleaner",
                "type": "switch",
                "int_status": "0",
                "state": "off",
                "status": "off",
            },
            "Aux_2": {
                "id": "Aux_2",
                "name": "NONE",
                "type": "switch",
                "int_status": "0",
                "state": "off",
                "status": "off",
            },
            "Aux_8": {
                "id": "Aux_8",
                "name": "Waterfall",
                "type": "switch",
                "int_status": "0",
                "state": "off",
                "status": "off",
            },
            "Air_Temp": {
                "id": "Air_Temp",
                "type": "temperature",
                "value": "75",
            },
            POOL_HEATER: {
                "id": POOL_HEATER,
                "name": "Pool Heater",
                "type": "setpoint_thermo",
                "int_status": "3",
                "state": "off",
                "status": "enabled",
                "spvalue": "80",
                "value": "82",
            },
            "SWG": {
                "id": "SWG",
                "name": "Salt Water Generator",
                "type": "setpoint_swg",
                "int_status": "1",
                "spvalue": "35",
            },
        }

    @property
    def base_url(self) -> str:
        return self._base_url

    async def devices(self) -> EquipmentSnapshot:
        return EquipmentSnapshot(
            temp_units="f",
            devices=copy.deepcopy(self._devices),
        )

    async def status(self) -> dict[str, Any]:
        panel_now = datetime.now(UTC) + timedelta(
            minutes=self._panel_time_offset_minutes
        )
        return {
            "panel_type_full": "PD-8 Combo",
            "panel_type": "PD-8",
            "version": "PDA-PS8 Combo PDA: 7.1.0",
            "date": panel_now.strftime("%a").upper(),
            "time": panel_now.strftime("%I:%M%p"),
        }

    async def set_device(self, identifier: str, enabled: bool) -> None:
        heater_enabled = identifier == POOL_HEATER and enabled
        self._devices[identifier].update(
            int_status="3" if heater_enabled else str(int(enabled)),
            state="on" if enabled and not heater_enabled else "off",
            status=("enabled" if heater_enabled else "on" if enabled else "off"),
        )
        await self._publish(DEVICE_ACTIVE)
        await self._publish(DEVICE_FINISHED)

    async def set_setpoint(self, identifier: str, value: int) -> None:
        self._devices[identifier]["spvalue"] = str(value)
        await self._publish(
            LEGACY_POOL_HEATER_SETPOINT_ACTIVE
            if self._legacy_heater_markers
            else POOL_HEATER_SETPOINT_ACTIVE
        )
        await self._publish(
            LEGACY_POOL_HEATER_SETPOINT_FINISHED
            if self._legacy_heater_markers
            else POOL_HEATER_SETPOINT_FINISHED
        )

    async def _publish(self, text: str) -> None:
        await self._context.monitor.publish(
            self._context.timeline.offset_ns(),
            "stdout",
            f"{self._log_prefix}{text}",
        )


class CleanupApi:
    def __init__(
        self,
        context: ScenarioContext,
        devices: dict[str, dict[str, Any]],
        *,
        pending_polls: dict[str, int] | None = None,
    ) -> None:
        self._context = context
        self._devices = copy.deepcopy(devices)
        self._pending_polls = dict(pending_polls or {})
        self.set_device_calls: list[tuple[str, bool]] = []

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:8080"

    async def devices(self) -> EquipmentSnapshot:
        for identifier, remaining in tuple(self._pending_polls.items()):
            if self._devices[identifier].get("int_status") != "2":
                continue
            if remaining <= 0:
                self._devices[identifier].update(
                    int_status="0",
                    state="off",
                    status="off",
                )
                del self._pending_polls[identifier]
            else:
                self._pending_polls[identifier] = remaining - 1
        return EquipmentSnapshot(
            temp_units="f",
            devices=copy.deepcopy(self._devices),
        )

    async def status(self) -> dict[str, Any]:
        return {}

    async def set_device(self, identifier: str, enabled: bool) -> None:
        self.set_device_calls.append((identifier, enabled))
        if enabled:
            self._devices[identifier].update(
                int_status="1",
                state="on",
                status="on",
            )
        elif self._pending_polls.get(identifier, 0) > 0:
            self._devices[identifier].update(
                int_status="2",
                state="off",
                status="flash",
            )
        else:
            self._devices[identifier].update(
                int_status="0",
                state="off",
                status="off",
            )
        await self._context.monitor.publish(
            self._context.timeline.offset_ns(),
            "stdout",
            DEVICE_ACTIVE,
        )
        await self._context.monitor.publish(
            self._context.timeline.offset_ns(),
            "stdout",
            DEVICE_FINISHED,
        )

    async def set_setpoint(self, identifier: str, value: int) -> None:
        self._devices[identifier]["spvalue"] = str(value)


class SpaApi(CleanupApi):
    async def set_device(self, identifier: str, enabled: bool) -> None:
        self.set_device_calls.append((identifier, enabled))
        self._devices[identifier].update(
            int_status="1" if enabled else "0",
            state="on" if enabled else "off",
            status="on" if enabled else "off",
        )
        await self._context.monitor.publish(
            self._context.timeline.offset_ns(), "stdout", DEVICE_ACTIVE
        )
        await self._context.monitor.publish(
            self._context.timeline.offset_ns(), "stdout", DEVICE_FINISHED
        )

    async def set_setpoint(self, identifier: str, value: int) -> None:
        self._devices[identifier]["spvalue"] = str(value)
        await self._context.monitor.publish(
            self._context.timeline.offset_ns(),
            "stdout",
            SPA_HEATER_SETPOINT_ACTIVE,
        )
        await self._context.monitor.publish(
            self._context.timeline.offset_ns(),
            "stdout",
            SPA_HEATER_SETPOINT_FINISHED,
        )


class PdaScenarioTests(unittest.TestCase):
    def test_spa_heating_uses_site_fill_and_restores_state(self) -> None:
        asyncio.run(self._run_spa_heating())

    def test_declarative_filter_test_uses_scenario_lifecycle(self) -> None:
        asyncio.run(self._run_declarative_filter_test())

    def test_declarative_filter_discovers_api_before_creating_actions(self) -> None:
        asyncio.run(self._run_declarative_filter_test(discover_api=True))

    def test_declarative_suite_reuses_one_initialized_runtime(self) -> None:
        asyncio.run(self._run_declarative_filter_test(as_suite=True))

    def test_switch_selection_uses_api_name_and_reported_panel_size(self) -> None:
        scenario = PdaScenarioRuntime(
            None,
            PdaScenarioConfig(
                suite_name="pda-live-long",
                disabled_button_numbers=(4, 5, 8, 9, 12),
            ),
        )
        scenario._reported_panel_size = 6
        scenario._reported_panel_combo = True
        scenario._initial_snapshot = EquipmentSnapshot(
            temp_units="f",
            devices={
                "Filter_Pump": {"type": "switch", "name": "Filter Pump"},
                "Spa_Mode": {"type": "switch", "name": "Spa"},
                "Aux_1": {"type": "switch", "name": "Cleaner"},
                "Aux_2": {"type": "switch", "name": "NONE"},
                "Aux_3": {"type": "switch", "name": "NONE"},
                "Aux_4": {"type": "switch", "name": "Pool Light"},
                "Aux_5": {"type": "switch", "name": "Spa Light"},
                "Aux_6": {"type": "switch", "name": "NONE"},
                "Aux_7": {"type": "switch", "name": "NONE"},
                "Solar_Heater": {"type": "switch", "name": "NONE"},
            },
        )

        scenario._record_device_constraints()

        safe = [
            identifier
            for identifier in scenario._initial_snapshot.devices
            if identifier not in scenario._excluded_device_ids
        ]
        self.assertEqual(
            safe,
            ["Filter_Pump", "Spa_Mode", "Aux_1", "Aux_4", "Aux_5"],
        )
        self.assertEqual(
            scenario._excluded_device_ids,
            {"Aux_2", "Aux_3", "Aux_6", "Aux_7", "Solar_Heater"},
        )
        self.assertEqual(
            scenario._sleep_test_device(phase="devices.sleep.test"),
            "Aux_5",
        )

    def test_sleep_selection_uses_highest_aux_on_small_pool_only_panel(
        self,
    ) -> None:
        scenario = PdaScenarioRuntime(None, PdaScenarioConfig())
        scenario._reported_panel_size = 4
        scenario._reported_panel_combo = False
        scenario._initial_snapshot = EquipmentSnapshot(
            temp_units="f",
            devices={
                "Aux_1": {"type": "switch", "name": "Aux 1"},
                "Filter_Pump": {
                    "type": "switch",
                    "name": "Filter Pump",
                },
                "Aux_3": {"type": "switch", "name": "Aux 3"},
                "Aux_2": {"type": "switch", "name": "Aux 2"},
            },
        )

        scenario._record_device_constraints()

        self.assertEqual(
            scenario._sleep_test_device(phase="devices.sleep.test"),
            "Aux_3",
        )

    def test_single_exception_group_reports_underlying_failure(self) -> None:
        error = ExceptionGroup(
            "task group",
            [ScenarioFailure("legacy startup log was invalid")],
        )
        self.assertEqual(
            PdaScenarioRuntime._format_exception(error),
            "ScenarioFailure: legacy startup log was invalid",
        )

    def test_failed_case_restores_then_continues(self) -> None:
        asyncio.run(self._run_failed_case_continuation())

    def test_cleanup_orders_dependencies_and_waits_for_delays(self) -> None:
        asyncio.run(self._run_dependency_aware_cleanup())

    def test_cleanup_does_not_toggle_pending_off_transition(self) -> None:
        asyncio.run(self._run_pending_cleanup())

    def test_cleanup_retry_waits_without_resending_toggle(self) -> None:
        asyncio.run(self._run_cleanup_retry())

    def test_flashing_device_does_not_satisfy_requested_on_state(self) -> None:
        asyncio.run(self._run_flashing_device_state_wait())

    def test_heater_enabled_state_is_distinct_from_active_heating(self) -> None:
        enabled = {
            "id": POOL_HEATER,
            "type": "setpoint_thermo",
            "int_status": "3",
            "state": "off",
            "status": "enabled",
        }
        active = {**enabled, "int_status": "1", "state": "on", "status": "on"}

        self.assertTrue(PdaScenarioRuntime._device_enabled(enabled))
        self.assertFalse(PdaScenarioRuntime._device_active(enabled))
        self.assertTrue(PdaScenarioRuntime._device_enabled(active))
        self.assertTrue(PdaScenarioRuntime._device_active(active))
        self.assertEqual(
            PdaScenarioRuntime._requested_device_state_label(enabled, True),
            "enabled",
        )

    async def _run_flashing_device_state_wait(self) -> None:
        devices = {
            "Filter_Pump": {
                "id": "Filter_Pump",
                "type": "switch",
                "int_status": "2",
                "state": "off",
                "status": "flash",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            timeline = Timeline(
                artifact_dir / "timeline.jsonl",
                time.monotonic_ns(),
            )
            context = ScenarioContext(
                artifact_dir=artifact_dir,
                monitor=OutputMonitor(),
                timeline=timeline,
            )
            api = CleanupApi(
                context,
                devices,
                pending_polls={"Filter_Pump": 1},
            )
            scenario = PdaScenarioRuntime(
                api,
                PdaScenarioConfig(state_timeout_seconds=0.4),
            )
            try:
                with self.assertRaisesRegex(
                    ScenarioFailure,
                    "did not become on",
                ):
                    await scenario._wait_for_device_state(
                        context,
                        "Filter_Pump",
                        True,
                        timeout_seconds=0.4,
                    )
            finally:
                timeline.close()

    async def _run_dependency_aware_cleanup(self) -> None:
        identifiers = (POOL_HEATER, "Aux_1", "Spa_Mode", "Filter_Pump")
        original = {
            identifier: {
                "id": identifier,
                "int_status": "0",
                "state": "off",
                "status": "off",
            }
            for identifier in identifiers
        }
        current = copy.deepcopy(original)
        for identifier in identifiers:
            current[identifier].update(
                int_status="3" if identifier == POOL_HEATER else "1",
                state="on",
                status="enabled" if identifier == POOL_HEATER else "on",
            )

        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            timeline = Timeline(
                artifact_dir / "timeline.jsonl",
                time.monotonic_ns(),
            )
            context = ScenarioContext(
                artifact_dir=artifact_dir,
                monitor=OutputMonitor(),
                timeline=timeline,
            )
            api = CleanupApi(
                context,
                current,
                pending_polls={"Spa_Mode": 2, "Filter_Pump": 3},
            )
            scenario = PdaScenarioRuntime(
                api,
                PdaScenarioConfig(restoration_timeout_seconds=2.0),
            )
            scenario._initial_snapshot = EquipmentSnapshot(
                temp_units="f",
                devices=original,
            )
            scenario._restoration.capture_initial(scenario._initial_snapshot)
            for identifier in identifiers:
                scenario._restoration.touch_device(identifier)
            try:
                errors = await scenario._restore_original_state(context)
            finally:
                timeline.close()

            self.assertEqual(errors, [])
            self.assertEqual(
                api.set_device_calls,
                [
                    (POOL_HEATER, False),
                    ("Aux_1", False),
                    ("Spa_Mode", False),
                    ("Filter_Pump", False),
                ],
            )

    async def _run_pending_cleanup(self) -> None:
        original = {
            "Filter_Pump": {
                "id": "Filter_Pump",
                "int_status": "0",
                "state": "off",
                "status": "off",
            }
        }
        pending = copy.deepcopy(original)
        pending["Filter_Pump"].update(int_status="2", status="flash")

        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            timeline = Timeline(
                artifact_dir / "timeline.jsonl",
                time.monotonic_ns(),
            )
            context = ScenarioContext(
                artifact_dir=artifact_dir,
                monitor=OutputMonitor(),
                timeline=timeline,
            )
            api = CleanupApi(
                context,
                pending,
                pending_polls={"Filter_Pump": 2},
            )
            scenario = PdaScenarioRuntime(
                api,
                PdaScenarioConfig(restoration_timeout_seconds=2.0),
            )
            scenario._initial_snapshot = EquipmentSnapshot(
                temp_units="f",
                devices=original,
            )
            scenario._restoration.capture_initial(scenario._initial_snapshot)
            scenario._restoration.touch_device("Filter_Pump")
            try:
                errors = await scenario._restore_original_state(context)
            finally:
                timeline.close()

            self.assertEqual(errors, [])
            self.assertEqual(api.set_device_calls, [])

    async def _run_cleanup_retry(self) -> None:
        original = {
            "Spa_Mode": {
                "id": "Spa_Mode",
                "int_status": "0",
                "state": "off",
                "status": "off",
            }
        }
        current = copy.deepcopy(original)
        current["Spa_Mode"].update(
            int_status="1",
            state="on",
            status="on",
        )

        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            timeline = Timeline(
                artifact_dir / "timeline.jsonl",
                time.monotonic_ns(),
            )
            context = ScenarioContext(
                artifact_dir=artifact_dir,
                monitor=OutputMonitor(),
                timeline=timeline,
            )
            api = CleanupApi(
                context,
                current,
                pending_polls={"Spa_Mode": 10},
            )
            scenario = PdaScenarioRuntime(
                api,
                PdaScenarioConfig(restoration_timeout_seconds=1.0),
            )
            scenario._initial_snapshot = EquipmentSnapshot(
                temp_units="f",
                devices=original,
            )
            scenario._restoration.capture_initial(scenario._initial_snapshot)
            scenario._restoration.touch_device("Spa_Mode")
            first_errors = await scenario._restore_original_state(context)
            api._pending_polls["Spa_Mode"] = 1
            try:
                second_errors = await scenario._restore_original_state(context)
            finally:
                timeline.close()

            self.assertTrue(first_errors)
            self.assertEqual(second_errors, [])
            self.assertEqual(api.set_device_calls, [("Spa_Mode", False)])

    async def _run_failed_case_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            timeline = Timeline(
                artifact_dir / "timeline.jsonl",
                time.monotonic_ns(),
            )
            context = ScenarioContext(
                artifact_dir=artifact_dir,
                monitor=OutputMonitor(),
                timeline=timeline,
            )
            scenario = PdaScenarioRuntime(
                None,
                PdaScenarioConfig(
                    suite_name="continuation-test",
                    case_ids=(
                        RuntimeCaseId.INITIALIZATION,
                        RuntimeCaseId.AQUAPDA_TRANSPORT,
                        RuntimeCaseId.AQUAPDA_MENU_WALK,
                    ),
                ),
            )
            observed: list[str] = []

            async def run_test(name: str, operation: Any) -> None:
                del operation
                observed.append(name)
                if name == "PDA initialization, identity, and clock":
                    scenario._initial_snapshot = EquipmentSnapshot(
                        temp_units="f",
                        devices={},
                    )
                if name == "AquaPDA WebSocket transport integrity":
                    raise ScenarioFailure("injected assertion failure")

            async def restore_case(context: Any, case: Any) -> list[str]:
                del context, case
                return []

            scenario._run_test = run_test  # type: ignore[method-assign]
            scenario._restore_after_case = (  # type: ignore[method-assign]
                restore_case
            )
            try:
                outcome = await scenario.run(context)
            finally:
                timeline.close()

            report = json.loads(
                (artifact_dir / "scenario.json").read_text(encoding="utf-8")
            )
            self.assertEqual(outcome.status, "failed")
            self.assertEqual(outcome.reason, "case_failures")
            self.assertTrue(report["safe_to_continue"])
            self.assertEqual(
                [case["status"] for case in report["cases"]],
                ["passed", "failed", "passed"],
            )
            self.assertEqual(len(observed), 3)

    async def _run_declarative_filter_test(
        self,
        *,
        discover_api: bool = False,
        as_suite: bool = False,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            timeline = Timeline(
                artifact_dir / "timeline.jsonl",
                time.monotonic_ns(),
            )
            monitor = OutputMonitor()
            context = ScenarioContext(artifact_dir, monitor, timeline)
            api = FakeApi(context)
            testcase = load_testcase(
                Path(__file__).parents[1]
                / "testcases"
                / "pda"
                / "filter-after-init.yaml"
            )
            suite = load_testcase_suite(
                Path(__file__).parents[1]
                / "testcases"
                / "suites"
                / "pda-live-fast.yaml"
            )
            scenario = PdaScenarioRuntime(
                None if discover_api else api,
                PdaScenarioConfig(
                    suite_name=suite.identifier if as_suite else testcase.identifier,
                    init_timeout_seconds=1,
                    panel_timezone="UTC",
                ),
                api_factory=lambda base_url: api,
                testcase=None if as_suite else testcase,
                testcases=(
                    tuple(member.testcase for member in suite.members[:2])
                    if as_suite
                    else ()
                ),
            )
            startup_lines = [
                (100_000_000, "AqualinkD: Starting Aqualink Daemon v3.1.1 !"),
                (200_000_000, "AqualinkD: panel type = PDA-8 Combo (Pool & Spa)"),
                (500_000_000, INIT_ACTIVE),
                (600_000_000, "PDA Menu Line 1 = PDA-PS8 Combo"),
                (700_000_000, "PDA Menu Line 3 = Firmware Version"),
                (800_000_000, "PDA Menu Line 5 = PDA: 7.1.0"),
                (2_000_000_000, INIT_FINISHED),
            ]
            if discover_api:
                startup_lines.insert(
                    0,
                    (
                        50_000_000,
                        "NetService:Starting web server on http://0.0.0.0:8080",
                    ),
                )
            for offset, line in startup_lines:
                await monitor.publish(offset, "stdout", line)
            try:
                outcome = await scenario.run(context)
            finally:
                timeline.close()

            report = json.loads(
                (artifact_dir / "scenario.json").read_text(encoding="utf-8")
            )
            self.assertEqual(outcome.status, "passed")
            if as_suite:
                self.assertEqual(
                    report["testcases"],
                    ["pda.initialization", "pda.filter-after-init"],
                )
                self.assertEqual(len(report["testcase_executions"]), 2)
                self.assertEqual(len(report["cases"]), 2)
            else:
                self.assertEqual(report["testcase"], "pda.filter-after-init")
            self.assertEqual(
                report["api_endpoint_source"],
                "aqualinkd_startup_log" if discover_api else "injected",
            )
            self.assertEqual(report["restoration"]["status"], "passed")
            self.assertEqual(
                [step["keyword"] for step in report["testcase_execution"]["steps"]],
                [
                    "wait_for",
                    "set_device",
                    "assert_device",
                    "restore_original_state",
                ],
            )
            self.assertFalse(
                (await api.devices()).devices["Filter_Pump"].enabled
            )

    async def _run_spa_heating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = ScenarioContext(
                artifact_dir=Path(directory),
                monitor=OutputMonitor(),
                timeline=Timeline(
                    Path(directory) / "timeline.jsonl",
                    time.monotonic_ns(),
                ),
            )
            api = SpaApi(
                context,
                {
                    "Filter_Pump": {
                        "id": "Filter_Pump",
                        "type": "switch",
                        "int_status": "0",
                        "state": "off",
                        "status": "off",
                    },
                    "Spa": {
                        "id": "Spa",
                        "type": "switch",
                        "int_status": "0",
                        "state": "off",
                        "status": "off",
                    },
                    SPA_HEATER: {
                        "id": SPA_HEATER,
                        "type": "setpoint_thermo",
                        "int_status": "0",
                        "state": "off",
                        "status": "off",
                        "spvalue": "78",
                        "value": "78",
                    },
                },
            )
            scenario = PdaScenarioRuntime(
                api,
                PdaScenarioConfig(
                    spa_fill_seconds=0.001,
                    status_timeout_seconds=0.2,
                    restoration_timeout_seconds=0.2,
                ),
            )
            initial = await api.devices()
            scenario._initial_snapshot = initial
            scenario._restoration.capture_initial(initial)
            try:
                await scenario._test_spa_heating(context)
                errors = await scenario._restore_original_state(context)
            finally:
                context.timeline.close()

            self.assertEqual(errors, [])
            restored = await api.devices()
            self.assertFalse(restored.devices["Filter_Pump"].enabled)
            self.assertFalse(restored.devices["Spa"].enabled)
            self.assertFalse(restored.devices[SPA_HEATER].enabled)
            self.assertEqual(restored.devices[SPA_HEATER].setpoint, 78)
            measurement_names = {
                measurement["name"] for measurement in scenario._report["measurements"]
            }
            self.assertIn("spa.pool_mode_fill", measurement_names)
            self.assertIn("spa.heater.active", measurement_names)
