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
from typing import Any

from aqualinkd_validator.http_api import DeviceSnapshot
from aqualinkd_validator.pda_scenario import (
    DEVICE_ACTIVE,
    DEVICE_FINISHED,
    INIT_ACTIVE,
    INIT_FINISHED,
    PDA_SLEEPING,
    POOL_HEATER,
    POOL_HEATER_SETPOINT_ACTIVE,
    POOL_HEATER_SETPOINT_FINISHED,
    STATUS_MENU_PRESENT,
    PdaLivePanelScenario,
    PdaScenarioConfig,
)
from aqualinkd_validator.supervisor import (
    OutputMonitor,
    ScenarioContext,
    Timeline,
)


class FakeApi:
    def __init__(
        self,
        context: ScenarioContext,
        panel_time_offset_minutes: int = 0,
        base_url: str = "http://127.0.0.1:8080",
    ) -> None:
        self._context = context
        self._panel_time_offset_minutes = panel_time_offset_minutes
        self._base_url = base_url
        self._devices: dict[str, dict[str, Any]] = {
            "Filter_Pump": {
                "id": "Filter_Pump",
                "type": "switch",
                "int_status": "0",
            },
            "Aux_1": {
                "id": "Aux_1",
                "type": "switch",
                "int_status": "0",
            },
            "Air_Temp": {
                "id": "Air_Temp",
                "type": "temperature",
                "value": "75",
            },
            POOL_HEATER: {
                "id": POOL_HEATER,
                "type": "setpoint_thermo",
                "int_status": "3",
                "spvalue": "80",
            },
        }

    @property
    def base_url(self) -> str:
        return self._base_url

    async def devices(self) -> DeviceSnapshot:
        return DeviceSnapshot(
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
        self._devices[identifier]["int_status"] = (
            "3" if identifier == POOL_HEATER and enabled else str(int(enabled))
        )
        await self._publish(DEVICE_ACTIVE)
        await self._publish(DEVICE_FINISHED)

    async def set_setpoint(self, identifier: str, value: int) -> None:
        self._devices[identifier]["spvalue"] = str(value)
        await self._publish(POOL_HEATER_SETPOINT_ACTIVE)
        await self._publish(POOL_HEATER_SETPOINT_FINISHED)

    async def _publish(self, text: str) -> None:
        await self._context.monitor.publish(
            self._context.timeline.offset_ns(),
            "stdout",
            text,
        )


class PdaScenarioTests(unittest.TestCase):
    def test_scenario_rejects_panel_clock_outside_tolerance(self) -> None:
        asyncio.run(self._run_bad_clock_scenario())

    def test_scenario_times_actions_and_restores_original_state(self) -> None:
        asyncio.run(self._run_scenario())

    def test_fast_scenario_does_not_wait_for_panel_states(self) -> None:
        asyncio.run(self._run_fast_scenario())

    async def _run_scenario(self) -> None:
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
                    action_timeout_seconds=0.5,
                    init_timeout_seconds=0.5,
                    sleep_timeout_seconds=0.5,
                    panel_timezone="UTC",
                    panel_time_tolerance_seconds=120.0,
                ),
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
            feeder = asyncio.create_task(self._feed_panel_states(context))
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
            self.assertEqual(
                report["device_selection"],
                {
                    "mode": "all_discovered_switches",
                    "requested": [],
                    "resolved": ["Filter_Pump", "Aux_1"],
                },
            )
            self.assertEqual(
                report["panel"]["init_screen"]["panel_type"],
                "PDA-PS8 Combo",
            )
            self.assertEqual(report["checks"][0]["status"], "passed")
            categories = {
                measurement["category"]
                for measurement in report["measurements"]
            }
            self.assertEqual(
                categories,
                {
                    "device",
                    "heater_setpoint",
                    "initialization",
                    "state_wait",
                },
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

    async def _run_fast_scenario(self) -> None:
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

            def api_factory(base_url: str) -> FakeApi:
                discovered_urls.append(base_url)
                return FakeApi(context, base_url=base_url)

            scenario = PdaLivePanelScenario(
                None,
                PdaScenarioConfig(
                    suite_name="pda-live-fast",
                    include_state_waits=False,
                    action_timeout_seconds=0.5,
                    init_timeout_seconds=0.5,
                    panel_timezone="UTC",
                ),
                api_factory=api_factory,
            )
            await monitor.publish(
                0,
                "stderr",
                "Starting web server on http://0.0.0.0:8080",
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
                report["device_selection"],
                {
                    "mode": "not_applicable",
                    "requested": [],
                    "resolved": [],
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
                measurement["category"]
                for measurement in report["measurements"]
            }
            self.assertNotIn("state_wait", categories)
            phases = {
                measurement["phase"]
                for measurement in report["measurements"]
            }
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
                    init_timeout_seconds=0.5,
                    panel_timezone="UTC",
                    panel_time_tolerance_seconds=120.0,
                ),
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
            self.assertEqual(report["checks"][0]["status"], "failed")
            self.assertIn("Panel time differs", report["error"])

    async def _feed_panel_states(self, context: ScenarioContext) -> None:
        while True:
            await asyncio.sleep(0.005)
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                STATUS_MENU_PRESENT,
            )
            await context.monitor.publish(
                context.timeline.offset_ns(),
                "stdout",
                PDA_SLEEPING,
            )
