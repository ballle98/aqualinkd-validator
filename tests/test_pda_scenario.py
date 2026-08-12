from __future__ import annotations

import asyncio
import copy
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.pda.cases import PdaCaseId
from aqualinkd_validator.pda_scenario import (
    DEVICE_ACTIVE,
    DEVICE_FINISHED,
    INIT_ACTIVE,
    INIT_FINISHED,
    LEGACY_POOL_HEATER_SETPOINT_ACTIVE,
    LEGACY_POOL_HEATER_SETPOINT_FINISHED,
    LEGACY_STATUS_MENU_PRESENT,
    PDA_ADDRESS_PROBE,
    PDA_ADDRESS_STATUS,
    PDA_SLEEPING,
    POOL_HEATER,
    POOL_HEATER_SETPOINT_ACTIVE,
    POOL_HEATER_SETPOINT_FINISHED,
    SPA_HEATER,
    SPA_HEATER_SETPOINT_ACTIVE,
    SPA_HEATER_SETPOINT_FINISHED,
    STATUS_MENU_PRESENT,
    WAKE_INIT_ACTIVE,
    WAKE_INIT_FINISHED,
    PdaLivePanelScenario,
    PdaScenarioConfig,
    ScenarioFailure,
)
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
        scenario = PdaLivePanelScenario(
            None,
            PdaScenarioConfig(
                suite_name="pda-live-long",
                include_state_waits=True,
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
        scenario = PdaLivePanelScenario(None, PdaScenarioConfig())
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

    def test_scenario_rejects_panel_clock_outside_tolerance(self) -> None:
        asyncio.run(self._run_bad_clock_scenario())

    def test_scenario_times_actions_and_restores_original_state(self) -> None:
        asyncio.run(self._run_scenario())

    def test_awake_phase_excludes_sleep_test(self) -> None:
        asyncio.run(self._run_scenario(execution_phase="awake"))

    def test_sleep_phase_excludes_awake_only_tests(self) -> None:
        asyncio.run(self._run_scenario(execution_phase="sleep"))

    def test_devices_configured_as_none_are_skipped(self) -> None:
        asyncio.run(self._run_scenario(disabled_button_numbers=(3,)))

    def test_legacy_status_menu_marker_is_supported(self) -> None:
        asyncio.run(
            self._run_scenario(
                status_menu_marker=LEGACY_STATUS_MENU_PRESENT,
            )
        )

    def test_fast_scenario_does_not_wait_for_panel_states(self) -> None:
        asyncio.run(self._run_fast_scenario(timestamped=True))

    def test_legacy_scenario_without_millisecond_logging(self) -> None:
        asyncio.run(self._run_fast_scenario(timestamped=False))

    def test_single_exception_group_reports_underlying_failure(self) -> None:
        error = ExceptionGroup(
            "task group",
            [ScenarioFailure("legacy startup log was invalid")],
        )
        self.assertEqual(
            PdaLivePanelScenario._format_exception(error),
            "ScenarioFailure: legacy startup log was invalid",
        )

    def test_current_web_server_url_log_is_supported(self) -> None:
        asyncio.run(self._run_web_server_discovery())

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

        self.assertTrue(PdaLivePanelScenario._device_enabled(enabled))
        self.assertFalse(PdaLivePanelScenario._device_active(enabled))
        self.assertTrue(PdaLivePanelScenario._device_enabled(active))
        self.assertTrue(PdaLivePanelScenario._device_active(active))
        self.assertEqual(
            PdaLivePanelScenario._requested_device_state_label(enabled, True),
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
            scenario = PdaLivePanelScenario(
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
            scenario = PdaLivePanelScenario(
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
            scenario = PdaLivePanelScenario(
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
            scenario = PdaLivePanelScenario(
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
            scenario = PdaLivePanelScenario(
                None,
                PdaScenarioConfig(
                    suite_name="continuation-test",
                    case_ids=(
                        PdaCaseId.INITIALIZATION,
                        PdaCaseId.FILTER_AFTER_INIT,
                        PdaCaseId.POOL_HEATER,
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
                if name == "Filter pump after initialization":
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

    async def _run_web_server_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            timeline = Timeline(
                artifact_dir / "timeline.jsonl",
                time.monotonic_ns(),
            )
            monitor = OutputMonitor()
            context = ScenarioContext(
                artifact_dir=artifact_dir,
                monitor=monitor,
                timeline=timeline,
            )
            scenario = PdaLivePanelScenario(
                None,
                PdaScenarioConfig(init_timeout_seconds=0.5),
            )
            await monitor.publish(
                0,
                "stdout",
                "Notice: NetService:Starting web server on http://0.0.0.0:8080",
            )
            try:
                discovered = await scenario._discover_api_base_url(context)
            finally:
                timeline.close()
            self.assertEqual(discovered, "http://127.0.0.1:8080")

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
            scenario = PdaLivePanelScenario(
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

    async def _run_scenario(
        self,
        *,
        status_menu_marker: str = STATUS_MENU_PRESENT,
        execution_phase: Literal["single", "awake", "sleep"] = "single",
        disabled_button_numbers: tuple[int, ...] = (),
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            timeline = Timeline(
                artifact_dir / "timeline.jsonl",
                time.monotonic_ns(),
            )
            monitor = OutputMonitor()
            context = ScenarioContext(
                artifact_dir=artifact_dir,
                monitor=monitor,
                timeline=timeline,
            )
            api = FakeApi(context)
            scenario = PdaLivePanelScenario(
                api,
                PdaScenarioConfig(
                    suite_name="pda-live-long",
                    include_state_waits=True,
                    execution_phase=execution_phase,
                    disabled_button_numbers=disabled_button_numbers,
                    action_timeout_seconds=0.5,
                    status_timeout_seconds=1.0,
                    init_timeout_seconds=1.0,
                    sleep_timeout_seconds=0.5,
                    status_retry_command_delay_seconds=0.01,
                    probe_command_min_delay_seconds=0.04,
                    panel_timezone="UTC",
                    panel_time_tolerance_seconds=120.0,
                ),
            )

            await monitor.publish(
                100_000_000,
                "stdout",
                "AqualinkD: Starting Aqualink Daemon v3.1.1 (Dev2) !",
            )
            await monitor.publish(
                200_000_000,
                "stdout",
                "AqualinkD: panel type = PDA-8 Combo (Pool & Spa)",
            )
            await monitor.publish(
                500_000_000,
                "stdout",
                f"Thread 19,0x1 {INIT_ACTIVE}",
            )
            await monitor.publish(
                600_000_000,
                "stdout",
                "PDA Menu Line 1 = PDA-PS8 Combo",
            )
            await monitor.publish(
                700_000_000,
                "stdout",
                "PDA Menu Line 3 = Firmware Version",
            )
            await monitor.publish(
                800_000_000,
                "stdout",
                "PDA Menu Line 5 = PDA: 7.1.0",
            )
            await monitor.publish(2_000_000_000, "stdout", INIT_FINISHED)
            feeder = asyncio.create_task(
                self._feed_panel_states(
                    context,
                    api=api,
                    status_menu_marker=status_menu_marker,
                )
            )
            try:
                outcome = await scenario.run(context)
            finally:
                feeder.cancel()
                await asyncio.gather(feeder, return_exceptions=True)
                timeline.close()

            self.assertEqual(outcome.status, "passed")
            report = json.loads(
                (artifact_dir / "scenario.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["restoration"]["status"], "passed")
            excluded = []
            if disabled_button_numbers:
                excluded.append(
                    {
                        "button": 3,
                        "identifier": "Aux_1",
                        "name": "Cleaner",
                        "reasons": ["button_03_label is configured as NONE"],
                    }
                )
            excluded.extend(
                [
                    {
                        "button": 4,
                        "identifier": "Aux_2",
                        "name": "NONE",
                        "reasons": ["API device name is NONE"],
                    },
                    {
                        "button": 10,
                        "identifier": "Aux_8",
                        "name": "Waterfall",
                        "reasons": ["Aux_8 is beyond reported panel size 8"],
                    },
                ]
            )
            expected_selection = (
                {
                    "mode": "auto_last_switch",
                    "requested": [],
                    "resolved": ["Filter_Pump" if disabled_button_numbers else "Aux_1"],
                    "configured_none_buttons": list(disabled_button_numbers),
                    "reported_panel_size": 8,
                    "excluded": excluded,
                }
                if execution_phase == "sleep"
                else {
                    "mode": "all_discovered_switches",
                    "requested": [],
                    "resolved": (
                        ["Filter_Pump"]
                        if disabled_button_numbers
                        else ["Filter_Pump", "Aux_1"]
                    ),
                    "configured_none_buttons": list(disabled_button_numbers),
                    "reported_panel_size": 8,
                    "excluded": excluded,
                }
            )
            self.assertEqual(report["device_selection"], expected_selection)
            self.assertEqual(
                report["panel"]["init_screen"]["panel_type"],
                "PDA-PS8 Combo",
            )
            self.assertEqual(report["checks"][0]["status"], "passed")
            categories = {
                measurement["category"] for measurement in report["measurements"]
            }
            expected_categories = {
                "device",
                "initialization",
                "state_wait",
            }
            if execution_phase != "sleep":
                expected_categories.add("heater_setpoint")
            if execution_phase != "awake":
                expected_categories.add("sleep_cycle")
            self.assertEqual(categories, expected_categories)
            measurement_names = {
                measurement["name"] for measurement in report["measurements"]
            }
            if execution_phase == "awake":
                self.assertNotIn("pda.sleep.enter", measurement_names)
            elif execution_phase == "sleep":
                self.assertNotIn("pda.status_menu.present", measurement_names)
                self.assertIn("pda.sleep.duration", measurement_names)
                self.assertIn(
                    "pda.sleep.status_retry.command_ready",
                    measurement_names,
                )
                self.assertIn(
                    "pda.sleep.probe.command_ready",
                    measurement_names,
                )
                self.assertIn("pda.after_wake.status_refresh", measurement_names)
                self.assertIn("pda.after_wake.return_to_sleep", measurement_names)
                self.assertIn("pda.wake.duration", measurement_names)
                self.assertIn("pda.sleep_wake.cycle", measurement_names)
                self.assertFalse(
                    any(
                        name.startswith("devices.after_init")
                        for name in measurement_names
                    )
                )
                sleep_cycle = report["sleep_cycle"]
                self.assertGreater(sleep_cycle["cycle_ms"], 0)
                self.assertAlmostEqual(
                    sleep_cycle["awake_ms"],
                    sleep_cycle["status_refresh_ms"]
                    + sleep_cycle["return_to_sleep_ms"],
                    places=2,
                )
                self.assertAlmostEqual(
                    sleep_cycle["awake_percent"] + sleep_cycle["sleep_percent"],
                    100.0,
                    places=2,
                )
            snapshot = await api.devices()
            self.assertEqual(
                snapshot.devices["Filter_Pump"]["int_status"],
                "0",
            )
            self.assertEqual(snapshot.devices["Aux_1"]["int_status"], "0")
            self.assertEqual(
                snapshot.devices[POOL_HEATER]["int_status"],
                "3",
            )
            self.assertEqual(
                snapshot.devices[POOL_HEATER]["spvalue"],
                "80",
            )
            self.assertEqual(report["suite"], "pda-live-long")
            self.assertEqual(report["execution_phase"], execution_phase)
            if execution_phase != "sleep":
                self.assertEqual(
                    report["equipment_status"]["missing_devices"],
                    [],
                )
                self.assertEqual(
                    report["equipment_status"]["incorrect_api_states"],
                    [],
                )
                self.assertEqual(
                    report["equipment_status"]["swg"],
                    {
                        "present": True,
                        "observed": True,
                        "percent": 35,
                        "api_percent": 35,
                    },
                )
                heater = report["equipment_status"]["heater_states"][POOL_HEATER]
                self.assertTrue(heater["enabled"])
                self.assertFalse(heater["active"])
                self.assertTrue(heater["pda_enabled"])
                self.assertFalse(heater["pda_active"])
                self.assertTrue(heater["pda_status_lines"])
                self.assertEqual(
                    report["equipment_status"]["heater_enabled_mismatches"],
                    [],
                )
                self.assertEqual(
                    report["equipment_status"]["heater_active_mismatches"],
                    [],
                )
                self.assertTrue(
                    report["equipment_status"]["setup_states"][POOL_HEATER]["enabled"]
                )

    async def _run_fast_scenario(self, *, timestamped: bool) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            timeline = Timeline(
                artifact_dir / "timeline.jsonl",
                time.monotonic_ns(),
            )
            monitor = OutputMonitor()
            context = ScenarioContext(
                artifact_dir=artifact_dir,
                monitor=monitor,
                timeline=timeline,
            )
            discovered_urls: list[str] = []
            log_prefix = "16:52:42.520 " if timestamped else ""

            def api_factory(base_url: str) -> FakeApi:
                discovered_urls.append(base_url)
                return FakeApi(
                    context,
                    base_url=base_url,
                    legacy_heater_markers=True,
                    log_prefix=log_prefix,
                )

            scenario = PdaLivePanelScenario(
                None,
                PdaScenarioConfig(
                    suite_name="pda-live-fast",
                    include_state_waits=False,
                    action_timeout_seconds=0.5,
                    init_timeout_seconds=1.0,
                    panel_timezone="UTC",
                ),
                api_factory=api_factory,
            )
            await monitor.publish(
                0,
                "stderr",
                f"{log_prefix}Notice: AqualinkD: Aqualink Daemon v2.3.7 (rev dbfcb39)",
            )
            await monitor.publish(
                100_000_000,
                "stdout",
                f"{log_prefix}Notice: AqualinkD: Panel set to PDA-8 Combo Pool/Spa",
            )
            await monitor.publish(
                200_000_000,
                "stdout",
                f"{log_prefix}Notice: NetService:Starting web server on port 8080",
            )
            await monitor.publish(
                500_000_000,
                "stdout",
                f"{log_prefix}Thread 19,0x1 {INIT_ACTIVE}",
            )
            await monitor.publish(
                600_000_000,
                "stdout",
                f"{log_prefix}Info: PDA Menu Line 1 = PDA-PS6 Combo",
            )
            await monitor.publish(
                700_000_000,
                "stdout",
                f"{log_prefix}PDA Menu Line 3 = Firmware Version",
            )
            await monitor.publish(
                800_000_000,
                "stdout",
                f"{log_prefix}PDA Menu Line 5 = PDA: 7.1.0",
            )
            await monitor.publish(
                2_000_000_000,
                "stdout",
                f"{log_prefix}{INIT_FINISHED}",
            )
            try:
                console = io.StringIO()
                with redirect_stdout(console):
                    outcome = await scenario.run(context)
            finally:
                timeline.close()

            self.assertEqual(outcome.status, "passed")
            progress = console.getvalue()
            self.assertIn(
                "[ RUN  ] PDA initialization, identity, and clock",
                progress,
            )
            self.assertIn(
                "[ACTIVE] Init PDA became active after 0.500s",
                progress,
            )
            self.assertIn(
                "[ DONE ] Init PDA programmer completed in 1.500s",
                progress,
            )
            self.assertIn("[STATE ] Init PDA started", progress)
            self.assertIn("[STATE ] Init PDA complete", progress)
            self.assertIn(
                "[INFO  ] AqualinkD version: v2.3.7 (rev dbfcb39)",
                progress,
            )
            self.assertIn(
                "[INFO  ] Panel reported: PDA-PS6 Combo; firmware PDA: 7.1.0",
                progress,
            )
            self.assertIn("[ WARN ] Configured panel type does not match", progress)
            self.assertRegex(
                progress,
                r"\[ PASS \] PDA initialization, identity, and clock "
                r"completed in \d+\.\d{3}s",
            )
            self.assertIn(
                "[ RUN  ] Filter pump after initialization",
                progress,
            )
            self.assertRegex(
                progress,
                r"\[ PASS \] pda-live-fast completed in \d+\.\d{3}s",
            )
            self.assertEqual(discovered_urls, ["http://127.0.0.1:8080"])
            report = json.loads(
                (artifact_dir / "scenario.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["suite"], "pda-live-fast")
            self.assertEqual(
                report["api_base_url"],
                "http://127.0.0.1:8080",
            )
            self.assertEqual(
                report["api_endpoint_source"],
                "aqualinkd_startup_log",
            )
            self.assertEqual(
                report["aqualinkd"],
                {
                    "configured_panel_type": "PDA-8 Combo Pool/Spa",
                    "source": "aqualinkd_startup_log",
                    "version": "v2.3.7 (rev dbfcb39)",
                },
            )
            panel_type_check = next(
                check for check in report["checks"] if check["name"] == "panel.type"
            )
            self.assertEqual(panel_type_check["status"], "warning")
            self.assertEqual(
                report["device_selection"],
                {
                    "mode": "not_applicable",
                    "requested": [],
                    "resolved": [],
                    "configured_none_buttons": [],
                    "reported_panel_size": 6,
                    "excluded": [
                        {
                            "button": 4,
                            "identifier": "Aux_2",
                            "name": "NONE",
                            "reasons": ["API device name is NONE"],
                        },
                        {
                            "button": 10,
                            "identifier": "Aux_8",
                            "name": "Waterfall",
                            "reasons": ["Aux_8 is beyond reported panel size 6"],
                        },
                    ],
                },
            )
            initialization = next(
                measurement
                for measurement in report["measurements"]
                if measurement["name"] == "pda.init"
            )
            self.assertIsNotNone(initialization["activation_ms"])
            self.assertEqual(initialization["activation_ms"], 500.0)
            self.assertEqual(
                initialization["programmer_duration_ms"],
                1500.0,
            )
            filter_on = next(
                measurement
                for measurement in report["measurements"]
                if measurement["name"].endswith("Filter_Pump.on")
            )
            self.assertEqual(filter_on["status"], "passed")
            self.assertIsNotNone(filter_on["activation_ms"])
            self.assertIsNotNone(filter_on["programmer_duration_ms"])
            self.assertIsNotNone(filter_on["state_convergence_ms"])
            categories = {
                measurement["category"] for measurement in report["measurements"]
            }
            self.assertNotIn("state_wait", categories)
            phases = {measurement["phase"] for measurement in report["measurements"]}
            self.assertNotIn("devices.status_menu", phases)
            self.assertNotIn("devices.sleeping", phases)

    async def _run_bad_clock_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            timeline = Timeline(
                artifact_dir / "timeline.jsonl",
                time.monotonic_ns(),
            )
            monitor = OutputMonitor()
            context = ScenarioContext(
                artifact_dir=artifact_dir,
                monitor=monitor,
                timeline=timeline,
            )
            scenario = PdaLivePanelScenario(
                FakeApi(context, panel_time_offset_minutes=10),
                PdaScenarioConfig(
                    action_timeout_seconds=0.5,
                    init_timeout_seconds=1.0,
                    panel_timezone="UTC",
                    panel_time_tolerance_seconds=120.0,
                ),
            )
            await monitor.publish(
                0,
                "stdout",
                "AqualinkD: Starting Aqualink Daemon v3.1.1 (Dev2) !",
            )
            await monitor.publish(
                0,
                "stdout",
                "AqualinkD: panel type = PDA-8 Combo (Pool & Spa)",
            )
            await monitor.publish(
                1,
                "stdout",
                f"Thread 19,0x1 {INIT_ACTIVE}",
            )
            await monitor.publish(
                2,
                "stdout",
                "PDA Menu Line 1 = PDA-PS8 Combo",
            )
            await monitor.publish(
                3,
                "stdout",
                "PDA Menu Line 3 = Firmware Version",
            )
            await monitor.publish(
                4,
                "stdout",
                "PDA Menu Line 5 = PDA: 7.1.0",
            )
            await monitor.publish(5, "stdout", INIT_FINISHED)
            try:
                outcome = await scenario.run(context)
            finally:
                timeline.close()

            self.assertEqual(outcome.status, "failed")
            report = json.loads(
                (artifact_dir / "scenario.json").read_text(encoding="utf-8")
            )
            clock_check = next(
                check for check in report["checks"] if check["name"] == "panel.time"
            )
            self.assertEqual(clock_check["status"], "failed")
            self.assertIn("Panel time differs", report["error"])

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
            scenario = PdaLivePanelScenario(
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

    async def _feed_panel_states(
        self,
        context: ScenarioContext,
        *,
        api: FakeApi,
        status_menu_marker: str,
    ) -> None:
        while True:
            await asyncio.sleep(0.005)
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                "PDA Menu Line 1 = AIR         POOL",
            )
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                "*** Pass Equiptment msg 'EQUIPMENT STATUS'",
            )
            snapshot = await api.devices()
            for device in snapshot.devices.values():
                if (
                    device.get("type") in {"switch", "setpoint_thermo"}
                    and int(device.get("int_status", 0)) != 0
                ):
                    int_status = int(device.get("int_status", 0))
                    if device.get("type") == "setpoint_thermo":
                        status = "" if int_status == 1 else " ENA"
                    else:
                        status = " ON"
                    await context.monitor.publish(
                        context.timeline.offset_ns(),
                        "stdout",
                        f"Found Status for {device['name']} = "
                        f"'{device['name']}{status}'",
                    )
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                status_menu_marker,
            )
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                "Pool Hearter is enabled",
            )
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                "*** Pass Equiptment msg '  AquaPure 35%'",
            )
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                "AquaPure = 35",
            )
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                "PDA End Equipment loop",
            )
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                "Start new equipment cycle bitmask 0x0003",
            )
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                PDA_SLEEPING,
            )
            await asyncio.sleep(0.005)
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                f"Read Jandy packet {PDA_ADDRESS_STATUS}",
            )
            await asyncio.sleep(0.03)
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                f"Read Jandy packet {PDA_ADDRESS_PROBE}",
            )
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                f"Thread 20,0x2 {WAKE_INIT_ACTIVE}",
            )
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                WAKE_INIT_FINISHED,
            )
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                PDA_SLEEPING,
            )
            await asyncio.sleep(0.02)
