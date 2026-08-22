from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .adapters import (
    AqualinkHttpApi,
    FileArtifactStore,
    IsolatedAqualinkdRuntime,
    LocalProcessRunner,
    PanelFixture,
)
from .capture import CapturedSerialTransport
from .engine.http_actions import HttpActions
from .engine.serial_actions import SerialActions
from .interfaces import (
    HttpTransport,
    ProcessRunner,
    RunResult,
    ScenarioContext,
    ScenarioOutcome,
    SerialTransport,
)
from .protocols.rs485 import AllButtonPanelDriver
from .testcases import (
    ExpectPanelCommandStep,
    ExpectSerialStep,
    HttpRequestStep,
    SerialSendStep,
    TestcaseDefinition,
    TestcaseExecutor,
    UnsupportedTestcaseKeywords,
)


class PanelFreeKeywords(UnsupportedTestcaseKeywords):
    """YAML keywords available to an emulated RS485 panel."""

    def __init__(
        self,
        serial: SerialActions,
        http: HttpActions,
        panel_driver: AllButtonPanelDriver | None,
    ) -> None:
        self._serial = serial
        self._http = http
        self._panel_driver = panel_driver

    async def serial_send(self, step: SerialSendStep) -> None:
        await self._serial.send(step.payload, timeout_seconds=step.timeout_seconds)

    async def expect_serial(self, step: ExpectSerialStep) -> None:
        await self._serial.expect_exact(
            step.payload,
            timeout_seconds=step.timeout_seconds,
        )

    async def http_request(self, step: HttpRequestStep) -> None:
        await self._http.request(
            step.method,
            step.path,
            value=step.value,
            timeout_seconds=step.timeout_seconds,
        )

    async def expect_panel_command(self, step: ExpectPanelCommandStep) -> None:
        if self._panel_driver is None:
            self._unsupported(step.keyword)
        assert self._panel_driver is not None
        await self._panel_driver.expect_command(
            step.command,
            timeout_seconds=step.timeout_seconds,
        )


class PanelFreeScenario:
    """Execute one RS485 YAML testcase against the isolated PTY endpoint."""

    def __init__(
        self,
        *,
        testcase: TestcaseDefinition,
        transport: SerialTransport,
        api: HttpTransport,
        http_ready_timeout_seconds: float = 10.0,
        http_poll_seconds: float = 0.05,
    ) -> None:
        if testcase.requirements.protocol != "rs485":
            raise ValueError("panel-free scenarios require an rs485 testcase")
        self._testcase = testcase
        self._transport = transport
        self._api = api
        self._http_ready_timeout_seconds = http_ready_timeout_seconds
        self._http_poll_seconds = http_poll_seconds
        self._serial: SerialActions | None = None

    async def run(self, context: ScenarioContext) -> ScenarioOutcome:
        started = time.monotonic()
        report: dict[str, Any] = {
            "testcase": self._testcase.identifier,
            "api_base_url": self._api.base_url,
            "status": "failed",
            "reason": "testcase_failed",
        }
        captured = CapturedSerialTransport(
            self._transport,
            timeline=context.timeline,
            artifacts=context.artifacts,
        )
        serial = SerialActions(captured, timeline=context.timeline)
        self._serial = serial
        http = HttpActions(
            self._api,
            timeline=context.timeline,
            artifacts=context.artifacts,
        )
        fixture = self._testcase.fixture
        assert fixture is not None
        panel_driver = (
            AllButtonPanelDriver(
                captured,
                device_id=fixture.device_id,
                timeline=context.timeline,
            )
            if fixture.driver == "allbutton"
            else None
        )
        print(f"\n=== Starting {self._testcase.identifier} ===", flush=True)
        print(
            "[ WAIT ] AqualinkD HTTP API readiness "
            f"(timeout {self._http_ready_timeout_seconds:g}s)",
            flush=True,
        )
        try:
            if panel_driver is None:
                await serial.open()
            await http.wait_ready(
                timeout_seconds=self._http_ready_timeout_seconds,
                poll_seconds=self._http_poll_seconds,
            )
            await context.timeline.write(
                "http_ready",
                api_base_url=self._api.base_url,
            )
            print("[STATE ] AqualinkD HTTP API ready", flush=True)
            if panel_driver is not None:
                print("[ WAIT ] AllButton panel probe/STATUS/ACK", flush=True)
                await panel_driver.start()
                print("[STATE ] AllButton panel driver active", flush=True)
            execution = await TestcaseExecutor(
                PanelFreeKeywords(serial, http, panel_driver)
            ).execute(self._testcase)
            report.update(
                status="passed",
                reason="scenario_completed",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                steps=[asdict(step) for step in execution.steps],
            )
            outcome = ScenarioOutcome("passed", "scenario_completed")
        except asyncio.CancelledError:
            report.update(status="failed", reason="scenario_cancelled")
            raise
        except BaseException as error:
            report.update(
                status="failed",
                reason="testcase_failed",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                error=f"{type(error).__name__}: {error}",
            )
            outcome = ScenarioOutcome("failed", "testcase_failed")
        finally:
            http.close()
            if panel_driver is not None:
                await panel_driver.stop()
            context.artifacts.write_json("scenario.json", report)
        state = "PASS" if outcome.status == "passed" else "FAIL"
        print(
            f"[{state:^6}] {self._testcase.identifier} completed in "
            f"{time.monotonic() - started:.3f}s",
            flush=True,
        )
        return outcome

    async def close(self) -> None:
        """Close capture and PTY after the supervised child has stopped."""
        serial = self._serial
        self._serial = None
        if serial is not None:
            await serial.close()

async def run_panel_free_testcase(
    *,
    testcase: TestcaseDefinition,
    aqualinkd: Path,
    web_directory: Path,
    artifact_dir: Path,
    process_runner: ProcessRunner | None = None,
    duration_seconds: float | None = 60.0,
    http_ready_timeout_seconds: float = 10.0,
    sample_interval_seconds: float = 1.0,
    terminate_grace_seconds: float = 10.0,
) -> RunResult:
    """Compose generated config, PTY capture, HTTP, and process supervision."""

    fixture = testcase.fixture
    if fixture is None:
        raise ValueError("panel-free testcase is missing its panel fixture")
    binary = aqualinkd.expanduser().resolve(strict=True)
    if not binary.is_file() or not binary.stat().st_mode & 0o111:
        raise ValueError(f"AqualinkD binary is not executable: {binary}")
    artifacts = FileArtifactStore(artifact_dir)
    runtime = IsolatedAqualinkdRuntime.create(
        web_directory=web_directory,
        fixture=PanelFixture(
            panel_type=fixture.panel_type,
            device_id=fixture.device_id,
            rssa_device_id=fixture.rssa_device_id,
            extended_device_id=fixture.extended_device_id,
            overrides=fixture.overrides,
        ),
        artifacts=artifacts,
    )
    command = [str(binary), "-d", "-c", str(runtime.config_path)]
    artifacts.write_json(
        "manifest.yaml",
        {
            "schema_version": 1,
            "mode": "rs485-panel-emulator",
            "testcase": testcase.identifier,
            "command": command,
            "api_base_url": runtime.api_base_url,
            "serial": {
                "capture_point": "pty_master",
                "slave_path": str(runtime.pty.slave_path),
            },
            "fixture": asdict(fixture),
        },
    )
    scenario = PanelFreeScenario(
        testcase=testcase,
        transport=runtime.pty.panel,
        api=AqualinkHttpApi(runtime.api_base_url),
        http_ready_timeout_seconds=http_ready_timeout_seconds,
    )
    runtime.release_http_port()
    try:
        result = await (process_runner or LocalProcessRunner()).run(
            command,
            artifact_dir,
            cwd=binary.parent,
            duration_seconds=duration_seconds,
            sample_interval_seconds=sample_interval_seconds,
            terminate_grace_seconds=terminate_grace_seconds,
            scenario=scenario,
        )
        return result
    finally:
        try:
            await scenario.close()
        finally:
            await runtime.close()
