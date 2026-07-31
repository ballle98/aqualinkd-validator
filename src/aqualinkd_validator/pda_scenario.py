from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import normalize_api_base_url
from .http_api import ApiError, AqualinkHttpApi, DeviceSnapshot
from .supervisor import LineEvent, ScenarioContext, ScenarioOutcome

FILTER_PUMP = "Filter_Pump"
POOL_HEATER = "Pool_Heater"

INIT_FINISHED = "(Init PDA) finished"
INIT_ACTIVE = "is active (Init PDA)"
DEVICE_FINISHED = "(Switch PDA device on/off) finished"
DEVICE_ACTIVE = "is active (Switch PDA device on/off)"
POOL_HEATER_SETPOINT_FINISHED = "(Set PDA Pool Heater) finished"
POOL_HEATER_SETPOINT_ACTIVE = "is active (Set PDA Pool Heater)"
STATUS_MENU_PRESENT = "PDA Start new Equiptment loop"
PDA_SLEEPING = "PDA Aqualink daemon in sleep mode"
FIRMWARE_VERSION_SCREEN = "PDA Menu Line 3 = Firmware Version"
WEB_SERVER_STARTED = "Starting web server on "

_PDA_MENU_LINE = re.compile(r"PDA Menu Line (\d+) =\s*(.*?)\s*$")
_WEB_SERVER_URL = re.compile(r"Starting web server on\s+(\S+)")
_TestResult = TypeVar("_TestResult")


class PdaApi(Protocol):
    @property
    def base_url(self) -> str: ...

    async def devices(self) -> DeviceSnapshot: ...

    async def status(self) -> dict[str, Any]: ...

    async def set_device(self, identifier: str, enabled: bool) -> None: ...

    async def set_setpoint(self, identifier: str, value: int) -> None: ...


@dataclass(frozen=True)
class PdaScenarioConfig:
    suite_name: str = "pda-live-fast"
    include_state_waits: bool = False
    activation_timeout_seconds: float = 130.0
    action_timeout_seconds: float = 90.0
    state_timeout_seconds: float = 10.0
    init_timeout_seconds: float = 180.0
    sleep_timeout_seconds: float = 120.0
    test_devices: tuple[str, ...] = ()
    panel_timezone: str = "UTC"
    panel_time_tolerance_seconds: float = 120.0


class ScenarioFailure(RuntimeError):
    """Raised when an expected PDA state transition does not complete."""


class PdaLivePanelScenario:
    def __init__(
        self,
        api: PdaApi | None,
        config: PdaScenarioConfig,
        *,
        api_base_url_override: str | None = None,
        api_factory: Callable[[str], PdaApi] = AqualinkHttpApi,
    ) -> None:
        self._api = api
        self._api_base_url_override = api_base_url_override
        self._api_factory = api_factory
        self._config = config
        endpoint_source = (
            "injected"
            if api is not None
            else (
                "explicit_override"
                if api_base_url_override is not None
                else "aqualinkd_startup_log"
            )
        )
        self._report: dict[str, Any] = {
            "schema_version": 1,
            "suite": config.suite_name,
            "api_base_url": (
                api.base_url if api is not None else api_base_url_override
            ),
            "api_endpoint_source": endpoint_source,
            "status": "running",
            "reason": None,
            "timeouts_seconds": {
                "activation": config.activation_timeout_seconds,
                "action": config.action_timeout_seconds,
                "state": config.state_timeout_seconds,
                "init": config.init_timeout_seconds,
                "sleep": config.sleep_timeout_seconds,
            },
            "checks": [],
            "panel": None,
            "measurements": [],
            "skipped": [],
            "device_selection": {
                "mode": (
                    "not_applicable"
                    if not config.include_state_waits
                    else (
                        "restricted"
                        if config.test_devices
                        else "all_discovered_switches"
                    )
                ),
                "requested": list(config.test_devices),
                "resolved": [],
            },
            "restoration": {
                "attempted": False,
                "status": "not-needed",
                "actions": [],
                "errors": [],
            },
        }
        self._initial_snapshot: DeviceSnapshot | None = None
        self._touched_devices: set[str] = set()
        self._touched_setpoints: set[str] = set()

    async def run(self, context: ScenarioContext) -> ScenarioOutcome:
        suite_started = time.monotonic()
        print(
            f"\n=== Starting {self._config.suite_name} ===",
            flush=True,
        )
        await context.timeline.write(
            "scenario_started",
            suite=self._config.suite_name,
            api_base_url=self._report["api_base_url"],
            api_endpoint_source=self._report["api_endpoint_source"],
        )
        status = "passed"
        reason = "scenario_completed"
        cancelled = False
        try:
            await self._run_test(
                "PDA initialization, identity, and clock",
                lambda: self._initialize(context),
            )
            await self._run_test(
                "Filter pump after initialization",
                lambda: self._toggle_round_trip(
                    context,
                    FILTER_PUMP,
                    phase="devices.after_init",
                ),
            )
            await self._run_test(
                "Pool heater controls",
                lambda: self._test_pool_heater(context),
            )
            if self._config.include_state_waits:
                await self._run_test(
                    "Filter pump with equipment-status menu present",
                    lambda: self._test_with_status_menu(context),
                )
                await self._run_test(
                    "Consecutive device operations",
                    lambda: self._test_consecutive_devices(context),
                )
                await self._run_test(
                    "Filter pump while PDA is sleeping",
                    lambda: self._test_while_sleeping(context),
                )
        except asyncio.CancelledError:
            status = "failed"
            reason = "scenario_cancelled"
            cancelled = True
        except Exception as error:
            status = "failed"
            reason = "scenario_failed"
            self._report["error"] = f"{type(error).__name__}: {error}"

        restoration_started = self._progress_started(
            "Restore original equipment state"
        )
        restoration_errors = await self._restore_original_state(context)
        if restoration_errors:
            self._progress_finished(
                "Restore original equipment state",
                restoration_started,
                passed=False,
                detail="; ".join(restoration_errors),
            )
            status = "failed"
            reason = "restoration_failed"
        else:
            self._progress_finished(
                "Restore original equipment state",
                restoration_started,
                passed=True,
            )

        self._report["status"] = status
        self._report["reason"] = reason
        self._write_report(context)
        await context.timeline.write(
            "scenario_finished",
            suite=self._config.suite_name,
            status=status,
            reason=reason,
        )
        self._progress_finished(
            self._config.suite_name,
            suite_started,
            passed=status == "passed",
            detail=None if status == "passed" else reason,
        )
        if cancelled:
            raise asyncio.CancelledError
        return ScenarioOutcome(status=status, reason=reason)

    async def _initialize(self, context: ScenarioContext) -> None:
        init_screen = await self._prepare_startup(context)
        self._initial_snapshot = await self._wait_for_api()
        await self._record_panel_identity_and_check_time(init_screen)
        self._require_device(self._initial_snapshot, FILTER_PUMP)

    async def _run_test(
        self,
        name: str,
        operation: Callable[[], Awaitable[_TestResult]],
    ) -> _TestResult:
        started = self._progress_started(name)
        try:
            result = await operation()
        except asyncio.CancelledError:
            self._progress_finished(
                name,
                started,
                passed=False,
                detail="cancelled",
            )
            raise
        except Exception as error:
            self._progress_finished(
                name,
                started,
                passed=False,
                detail=f"{type(error).__name__}: {error}",
            )
            raise
        self._progress_finished(name, started, passed=True)
        return result

    @staticmethod
    def _progress_started(name: str) -> float:
        print(f"[ RUN  ] {name}", flush=True)
        return time.monotonic()

    @staticmethod
    def _progress_finished(
        name: str,
        started: float,
        *,
        passed: bool,
        detail: str | None = None,
    ) -> None:
        elapsed = time.monotonic() - started
        status = "PASS" if passed else "FAIL"
        suffix = f" — {detail}" if detail else ""
        print(
            f"[ {status} ] {name} completed in {elapsed:.3f}s{suffix}",
            flush=True,
        )

    async def _wait_for_task_active(
        self,
        context: ScenarioContext,
        *,
        task_name: str,
        marker: str,
        after: int,
        requested_offset_ns: int,
        timeout_seconds: float,
        wait_reason: str = "waiting in the programmer queue",
    ) -> LineEvent:
        print(
            f"[ WAIT ] {task_name}: {wait_reason} "
            f"(timeout {timeout_seconds:g}s)",
            flush=True,
        )
        try:
            active = await context.monitor.wait_for(
                marker,
                after=after,
                timeout_seconds=timeout_seconds,
            )
        except TimeoutError as error:
            raise ScenarioFailure(
                f"{task_name} did not become active within "
                f"{timeout_seconds:g}s"
            ) from error
        activation_seconds = (
            active.offset_ns - requested_offset_ns
        ) / 1_000_000_000
        print(
            f"[ACTIVE] {task_name} became active after "
            f"{activation_seconds:.3f}s",
            flush=True,
        )
        if task_name == "Init PDA":
            print("[STATE ] Init PDA started", flush=True)
        await context.timeline.write(
            "scenario_programmer_active",
            task=task_name,
            activation_seconds=round(activation_seconds, 6),
        )
        return active

    async def _wait_for_task_completion(
        self,
        context: ScenarioContext,
        *,
        task_name: str,
        marker: str,
        active: LineEvent,
        timeout_seconds: float,
    ) -> LineEvent:
        try:
            completed = await context.monitor.wait_for(
                marker,
                after=active.sequence,
                timeout_seconds=timeout_seconds,
            )
        except TimeoutError as error:
            raise ScenarioFailure(
                f"{task_name} did not complete within {timeout_seconds:g}s "
                "after becoming active"
            ) from error
        programmer_seconds = (
            completed.offset_ns - active.offset_ns
        ) / 1_000_000_000
        print(
            f"[ DONE ] {task_name} programmer completed in "
            f"{programmer_seconds:.3f}s",
            flush=True,
        )
        if task_name == "Init PDA":
            print("[STATE ] Init PDA complete", flush=True)
        await context.timeline.write(
            "scenario_programmer_finished",
            task=task_name,
            programmer_seconds=round(programmer_seconds, 6),
        )
        return completed

    async def _prepare_startup(
        self,
        context: ScenarioContext,
    ) -> dict[str, str]:
        if self._api is None and self._api_base_url_override is not None:
            self._configure_api(
                self._api_base_url_override,
                source="explicit_override",
            )

        async with asyncio.TaskGroup() as tasks:
            startup_task = tasks.create_task(self._wait_for_startup(context))
            discovery_task = (
                tasks.create_task(self._discover_api_base_url(context))
                if self._api is None
                else None
            )
        if discovery_task is not None:
            discovered = discovery_task.result()
            self._configure_api(discovered, source="aqualinkd_startup_log")
            await context.timeline.write(
                "api_endpoint_discovered",
                api_base_url=discovered,
                source="aqualinkd_startup_log",
            )
        return startup_task.result()

    async def _discover_api_base_url(
        self,
        context: ScenarioContext,
    ) -> str:
        event = await context.monitor.wait_for(
            WEB_SERVER_STARTED,
            timeout_seconds=self._config.init_timeout_seconds,
        )
        match = _WEB_SERVER_URL.search(event.text)
        if match is None:
            raise ScenarioFailure(
                "AqualinkD web-server startup log did not contain a URL"
            )
        try:
            return normalize_api_base_url(match.group(1))
        except ValueError as error:
            raise ScenarioFailure(
                f"Invalid AqualinkD web-server URL in log: {match.group(1)}"
            ) from error

    def _configure_api(self, base_url: str, *, source: str) -> None:
        self._api = self._api_factory(base_url)
        self._report["api_base_url"] = self._api.base_url
        self._report["api_endpoint_source"] = source

    @property
    def _api_client(self) -> PdaApi:
        if self._api is None:
            raise ScenarioFailure("AqualinkD HTTP API endpoint is not configured")
        return self._api

    async def _wait_for_startup(
        self,
        context: ScenarioContext,
    ) -> dict[str, str]:
        active = await self._wait_for_task_active(
            context,
            task_name="Init PDA",
            marker=INIT_ACTIVE,
            after=0,
            requested_offset_ns=0,
            timeout_seconds=self._config.init_timeout_seconds,
            wait_reason="waiting for the panel probe",
        )
        async with asyncio.TaskGroup() as tasks:
            completion_task = tasks.create_task(
                self._wait_for_task_completion(
                    context,
                    task_name="Init PDA",
                    marker=INIT_FINISHED,
                    active=active,
                    timeout_seconds=self._config.init_timeout_seconds,
                )
            )
            screen_task = tasks.create_task(
                self._capture_init_screen(context, after=active.sequence)
            )
        completion = completion_task.result()
        self._append_measurement(
            name="pda.init",
            category="initialization",
            phase="startup",
            target="PDA_INIT",
            requested_value=None,
            start_offset_ns=0,
            api_ack_offset_ns=None,
            task_active_offset_ns=active.offset_ns,
            log_completion_offset_ns=completion.offset_ns,
            state_observed_offset_ns=None,
        )
        await context.timeline.write(
            "scenario_phase",
            phase="startup",
            state="PDA_INIT completed",
        )
        return screen_task.result()

    async def _capture_init_screen(
        self,
        context: ScenarioContext,
        *,
        after: int,
    ) -> dict[str, str]:
        firmware_marker = await context.monitor.wait_for(
            FIRMWARE_VERSION_SCREEN,
            after=after,
            timeout_seconds=self._config.init_timeout_seconds,
        )
        panel_type = ""
        for event in reversed(
            context.monitor.recent_events(before=firmware_marker.sequence)
        ):
            parsed = self._parse_menu_line(event.text)
            if parsed is not None and parsed[0] == 1:
                panel_type = parsed[1]
                break
        firmware_event = await context.monitor.wait_for(
            "PDA Menu Line 5 =",
            after=firmware_marker.sequence,
            timeout_seconds=self._config.init_timeout_seconds,
        )
        firmware = self._parse_menu_line(firmware_event.text)
        if not panel_type:
            raise ScenarioFailure(
                "PDA firmware-version screen did not contain a panel type "
                "on line 1"
            )
        if firmware is None or firmware[0] != 5 or not firmware[1]:
            raise ScenarioFailure(
                "PDA firmware-version screen did not contain firmware "
                "information on line 5"
            )
        return {
            "panel_type": panel_type,
            "firmware": firmware[1],
            "source": "pda_firmware_version_screen",
        }

    async def _record_panel_identity_and_check_time(
        self,
        init_screen: dict[str, str],
    ) -> None:
        status = await self._api_client.status()
        initial_identity = self._api_identity(status)
        self._report["panel"] = {
            "init_screen": init_screen,
            "api_status_after_init": initial_identity,
        }

        try:
            timezone = ZoneInfo(self._config.panel_timezone)
        except ZoneInfoNotFoundError as error:
            raise ScenarioFailure(
                f"Unknown panel timezone: {self._config.panel_timezone}"
            ) from error
        deadline = (
            asyncio.get_running_loop().time()
            + self._config.init_timeout_seconds
        )
        wait_started = time.monotonic()
        announced_wait = False
        while True:
            panel_time, now, difference = self._panel_time_difference(
                status,
                timezone,
            )
            if difference <= self._config.panel_time_tolerance_seconds:
                passed = True
                break
            if not announced_wait:
                print(
                    "[ WAIT ] Panel clock: waiting for initialization-time "
                    f"synchronization (timeout "
                    f"{self._config.init_timeout_seconds:g}s)",
                    flush=True,
                )
                announced_wait = True
            if asyncio.get_running_loop().time() >= deadline:
                passed = False
                break
            await asyncio.sleep(0.25)
            status = await self._api_client.status()

        waited_seconds = time.monotonic() - wait_started
        final_identity = self._api_identity(status)
        if final_identity != initial_identity:
            self._report["panel"]["api_status_after_clock_sync"] = (
                final_identity
            )
        self._report["checks"].append(
            {
                "name": "panel.time",
                "status": "passed" if passed else "failed",
                "panel_time": panel_time.strip(),
                "system_time": now.isoformat(),
                "timezone": self._config.panel_timezone,
                "difference_seconds": difference,
                "waited_seconds": round(waited_seconds, 3),
                "tolerance_seconds": (
                    self._config.panel_time_tolerance_seconds
                ),
            }
        )
        if not passed:
            raise ScenarioFailure(
                f"Panel time differs from {self._config.panel_timezone} "
                f"system time by {difference}s; tolerance is "
                f"{self._config.panel_time_tolerance_seconds:g}s"
            )

    @staticmethod
    def _api_identity(status: dict[str, Any]) -> dict[str, Any]:
        return {
            key: status.get(key)
            for key in (
                "panel_type_full",
                "panel_type",
                "version",
                "date",
                "time",
            )
        }

    @staticmethod
    def _panel_time_difference(
        status: dict[str, Any],
        timezone: ZoneInfo,
    ) -> tuple[str, datetime, int]:
        panel_time = status.get("time")
        if not isinstance(panel_time, str):
            raise ScenarioFailure("/api/status did not contain panel time")
        try:
            parsed = datetime.strptime(
                panel_time.strip().upper(),
                "%I:%M%p",
            )
        except ValueError as error:
            raise ScenarioFailure(
                f"Could not parse panel time {panel_time!r}"
            ) from error

        now = datetime.now(timezone)
        panel_seconds = parsed.hour * 3600 + parsed.minute * 60
        host_seconds = now.hour * 3600 + now.minute * 60 + now.second
        difference = abs(panel_seconds - host_seconds)
        difference = min(difference, 24 * 3600 - difference)
        return panel_time.strip(), now, difference

    async def _wait_for_api(self) -> DeviceSnapshot:
        deadline = asyncio.get_running_loop().time() + (
            self._config.action_timeout_seconds
        )
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                return await self._api_client.devices()
            except ApiError as error:
                last_error = error
                await asyncio.sleep(0.25)
        raise ScenarioFailure(
            "AqualinkD HTTP API did not become ready after PDA_INIT"
            + (f": {last_error}" if last_error is not None else "")
        )

    @staticmethod
    def _parse_menu_line(text: str) -> tuple[int, str] | None:
        match = _PDA_MENU_LINE.search(text)
        if match is None:
            return None
        return int(match.group(1)), match.group(2).strip()

    async def _toggle_round_trip(
        self,
        context: ScenarioContext,
        identifier: str,
        *,
        phase: str,
    ) -> None:
        initial = self._initial_device_enabled(identifier)
        await self._set_device(
            context,
            identifier,
            not initial,
            phase=phase,
        )
        await self._set_device(
            context,
            identifier,
            initial,
            phase=phase,
        )

    async def _test_with_status_menu(self, context: ScenarioContext) -> None:
        initial = self._initial_device_enabled(FILTER_PUMP)
        if not await self._current_device_enabled(FILTER_PUMP):
            await self._set_device(
                context,
                FILTER_PUMP,
                True,
                phase="devices.status_menu.setup",
            )

        cursor = context.monitor.cursor
        started = context.timeline.offset_ns()
        event = await context.monitor.wait_for(
            STATUS_MENU_PRESENT,
            after=cursor,
            timeout_seconds=self._config.action_timeout_seconds,
        )
        self._append_measurement(
            name="pda.status_menu.present",
            category="state_wait",
            phase="devices.status_menu",
            target="equipment_status_menu",
            requested_value="present",
            start_offset_ns=started,
            api_ack_offset_ns=None,
            log_completion_offset_ns=event.offset_ns,
            state_observed_offset_ns=None,
        )

        await self._set_device(
            context,
            FILTER_PUMP,
            False,
            phase="devices.status_menu",
        )
        await self._set_device(
            context,
            FILTER_PUMP,
            True,
            phase="devices.status_menu",
        )
        if not initial:
            await self._set_device(
                context,
                FILTER_PUMP,
                False,
                phase="devices.status_menu.restore",
            )

    async def _test_consecutive_devices(
        self,
        context: ScenarioContext,
    ) -> None:
        assert self._initial_snapshot is not None
        requested = list(dict.fromkeys(self._config.test_devices))
        if requested:
            identifiers = requested
            for identifier in identifiers:
                device = self._require_device(
                    self._initial_snapshot,
                    identifier,
                )
                if device.get("type") != "switch":
                    raise ScenarioFailure(
                        f"{identifier} is not a switch device"
                    )
        else:
            identifiers = [
                identifier
                for identifier, device
                in self._initial_snapshot.devices.items()
                if device.get("type") == "switch"
            ]

        self._report["device_selection"]["resolved"] = identifiers
        if not identifiers:
            self._skip(
                "devices.consecutive",
                "No switch devices were discovered in /api/devices",
            )
            return

        for identifier in identifiers:
            await self._set_device(
                context,
                identifier,
                not self._initial_device_enabled(identifier),
                phase="devices.consecutive",
            )
        for identifier in reversed(identifiers):
            await self._set_device(
                context,
                identifier,
                self._initial_device_enabled(identifier),
                phase="devices.consecutive.restore",
            )

    async def _test_pool_heater(self, context: ScenarioContext) -> None:
        assert self._initial_snapshot is not None
        heater = self._initial_snapshot.devices.get(POOL_HEATER)
        if heater is None or heater.get("type") != "setpoint_thermo":
            self._skip(
                "heater",
                "Pool_Heater is not present in /api/devices",
            )
            return

        await self._test_pool_heater_setpoint(context, heater)
        await self._toggle_round_trip(
            context,
            POOL_HEATER,
            phase="heater.on_off",
        )

    async def _test_pool_heater_setpoint(
        self,
        context: ScenarioContext,
        heater: dict[str, Any],
    ) -> None:
        assert self._initial_snapshot is not None
        try:
            original = round(float(heater["spvalue"]))
        except (KeyError, TypeError, ValueError):
            self._skip(
                "heater.setpoint",
                "Pool_Heater has no numeric spvalue",
            )
            return

        if self._initial_snapshot.temp_units == "f":
            minimum, maximum = 36, 104
        elif self._initial_snapshot.temp_units == "c":
            minimum, maximum = 0, 40
        else:
            self._skip(
                "heater.setpoint",
                "Temperature units are unknown",
            )
            return

        values = [
            value
            for value in (max(minimum, original - 1), min(maximum, original + 1))
            if value != original
        ]
        if len(values) < 2:
            self._skip(
                "heater.setpoint.boundary",
                "Original setpoint is at a supported range boundary; only "
                "the available direction will be tested",
            )
        for value in (*values, original):
            await self._set_setpoint(
                context,
                POOL_HEATER,
                value,
                phase="heater.setpoint",
                active_marker=POOL_HEATER_SETPOINT_ACTIVE,
                completion_marker=POOL_HEATER_SETPOINT_FINISHED,
                category="heater_setpoint",
            )

    async def _test_while_sleeping(self, context: ScenarioContext) -> None:
        cursor = context.monitor.cursor
        started = context.timeline.offset_ns()
        event = await context.monitor.wait_for(
            PDA_SLEEPING,
            after=cursor,
            timeout_seconds=self._config.sleep_timeout_seconds,
        )
        self._append_measurement(
            name="pda.sleep.enter",
            category="state_wait",
            phase="devices.sleeping",
            target="pda_sleep",
            requested_value=True,
            start_offset_ns=started,
            api_ack_offset_ns=None,
            log_completion_offset_ns=event.offset_ns,
            state_observed_offset_ns=None,
        )
        await self._toggle_round_trip(
            context,
            FILTER_PUMP,
            phase="devices.sleeping",
        )

    async def _set_device(
        self,
        context: ScenarioContext,
        identifier: str,
        enabled: bool,
        *,
        phase: str,
    ) -> None:
        self._remember_device(identifier)
        if await self._current_device_enabled(identifier) == enabled:
            self._skip(
                f"{phase}.{identifier}.{'on' if enabled else 'off'}",
                "Device is already in the requested state",
            )
            return

        cursor = context.monitor.cursor
        started = context.timeline.offset_ns()
        await context.timeline.write(
            "scenario_action_started",
            phase=phase,
            action="set_device",
            target=identifier,
            value=enabled,
        )
        await self._api_client.set_device(identifier, enabled)
        acknowledged = context.timeline.offset_ns()
        task_name = "Switch PDA device on/off"
        active = await self._wait_for_task_active(
            context,
            task_name=task_name,
            marker=DEVICE_ACTIVE,
            after=cursor,
            requested_offset_ns=started,
            timeout_seconds=self._config.activation_timeout_seconds,
        )
        completed = await self._wait_for_task_completion(
            context,
            task_name=task_name,
            marker=DEVICE_FINISHED,
            active=active,
            timeout_seconds=self._config.action_timeout_seconds,
        )
        requested_state = "on" if enabled else "off"
        print(
            f"[ WAIT ] {identifier}: waiting for API state "
            f"{requested_state} (timeout "
            f"{self._config.state_timeout_seconds:g}s)",
            flush=True,
        )
        try:
            observed = await self._wait_for_state_or_programmer_error(
                context,
                task_name=task_name,
                after=active.sequence,
                state_wait=self._wait_for_device_state(
                    context,
                    identifier,
                    enabled,
                    timeout_seconds=self._config.state_timeout_seconds,
                ),
            )
        except Exception:
            self._append_measurement(
                name=f"{phase}.{identifier}.{'on' if enabled else 'off'}",
                category="device",
                phase=phase,
                target=identifier,
                requested_value=enabled,
                start_offset_ns=started,
                api_ack_offset_ns=acknowledged,
                task_active_offset_ns=active.offset_ns,
                log_completion_offset_ns=completed.offset_ns,
                state_observed_offset_ns=None,
                status="failed",
            )
            raise
        state_seconds = (
            observed - completed.offset_ns
        ) / 1_000_000_000
        print(
            f"[STATE ] {identifier} became {requested_state} "
            f"{state_seconds:.3f}s after programmer completion",
            flush=True,
        )

        self._append_measurement(
            name=f"{phase}.{identifier}.{'on' if enabled else 'off'}",
            category="device",
            phase=phase,
            target=identifier,
            requested_value=enabled,
            start_offset_ns=started,
            api_ack_offset_ns=acknowledged,
            task_active_offset_ns=active.offset_ns,
            log_completion_offset_ns=completed.offset_ns,
            state_observed_offset_ns=observed,
        )
        await context.timeline.write(
            "scenario_action_finished",
            phase=phase,
            action="set_device",
            target=identifier,
            value=enabled,
            status="passed",
        )

    async def _set_setpoint(
        self,
        context: ScenarioContext,
        identifier: str,
        value: int,
        *,
        phase: str,
        active_marker: str,
        completion_marker: str,
        category: str,
    ) -> None:
        self._touched_setpoints.add(identifier)
        cursor = context.monitor.cursor
        started = context.timeline.offset_ns()
        await context.timeline.write(
            "scenario_action_started",
            phase=phase,
            action="set_setpoint",
            target=identifier,
            value=value,
        )
        await self._api_client.set_setpoint(identifier, value)
        acknowledged = context.timeline.offset_ns()
        task_name = (
            "Set PDA Pool Heater"
            if identifier == POOL_HEATER
            else identifier
        )
        active = await self._wait_for_task_active(
            context,
            task_name=task_name,
            marker=active_marker,
            after=cursor,
            requested_offset_ns=started,
            timeout_seconds=self._config.activation_timeout_seconds,
        )
        completed = await self._wait_for_task_completion(
            context,
            task_name=task_name,
            marker=completion_marker,
            active=active,
            timeout_seconds=self._config.action_timeout_seconds,
        )
        print(
            f"[ WAIT ] {identifier}: waiting for API setpoint {value} "
            f"(timeout {self._config.state_timeout_seconds:g}s)",
            flush=True,
        )
        try:
            observed = await self._wait_for_state_or_programmer_error(
                context,
                task_name=task_name,
                after=active.sequence,
                state_wait=self._wait_for_setpoint(
                    context,
                    identifier,
                    value,
                    timeout_seconds=self._config.state_timeout_seconds,
                ),
            )
        except Exception:
            self._append_measurement(
                name=f"{phase}.{value}",
                category=category,
                phase=phase,
                target=identifier,
                requested_value=value,
                start_offset_ns=started,
                api_ack_offset_ns=acknowledged,
                task_active_offset_ns=active.offset_ns,
                log_completion_offset_ns=completed.offset_ns,
                state_observed_offset_ns=None,
                status="failed",
            )
            raise
        state_seconds = (
            observed - completed.offset_ns
        ) / 1_000_000_000
        print(
            f"[STATE ] {identifier} reached setpoint {value} "
            f"{state_seconds:.3f}s after programmer completion",
            flush=True,
        )

        self._append_measurement(
            name=f"{phase}.{value}",
            category=category,
            phase=phase,
            target=identifier,
            requested_value=value,
            start_offset_ns=started,
            api_ack_offset_ns=acknowledged,
            task_active_offset_ns=active.offset_ns,
            log_completion_offset_ns=completed.offset_ns,
            state_observed_offset_ns=observed,
        )

    async def _wait_for_device_state(
        self,
        context: ScenarioContext,
        identifier: str,
        enabled: bool,
        *,
        timeout_seconds: float | None = None,
    ) -> int:
        timeout = timeout_seconds or self._config.state_timeout_seconds
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            snapshot = await self._api_client.devices()
            device = self._require_device(snapshot, identifier)
            if self._device_enabled(device) == enabled:
                return context.timeline.offset_ns()
            await asyncio.sleep(0.25)
        raise ScenarioFailure(
            f"{identifier} did not become {'on' if enabled else 'off'} "
            f"within {timeout:g}s"
        )

    async def _wait_for_setpoint(
        self,
        context: ScenarioContext,
        identifier: str,
        expected: int,
        *,
        timeout_seconds: float | None = None,
    ) -> int:
        timeout = timeout_seconds or self._config.state_timeout_seconds
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            snapshot = await self._api_client.devices()
            device = self._require_device(snapshot, identifier)
            try:
                actual = round(float(device["spvalue"]))
            except (KeyError, TypeError, ValueError) as error:
                raise ScenarioFailure(
                    f"{identifier} returned a non-numeric spvalue"
                ) from error
            if actual == expected:
                return context.timeline.offset_ns()
            await asyncio.sleep(0.25)
        raise ScenarioFailure(
            f"{identifier} setpoint did not become {expected} within "
            f"{timeout:g}s"
        )

    async def _current_device_enabled(self, identifier: str) -> bool:
        snapshot = await self._api_client.devices()
        return self._device_enabled(self._require_device(snapshot, identifier))

    def _initial_device_enabled(self, identifier: str) -> bool:
        assert self._initial_snapshot is not None
        return self._device_enabled(
            self._require_device(self._initial_snapshot, identifier)
        )

    def _remember_device(self, identifier: str) -> None:
        assert self._initial_snapshot is not None
        self._require_device(self._initial_snapshot, identifier)
        self._touched_devices.add(identifier)

    async def _restore_original_state(
        self,
        context: ScenarioContext,
    ) -> list[str]:
        restoration = self._report["restoration"]
        if self._initial_snapshot is None:
            return []
        restoration["attempted"] = True
        errors: list[str] = restoration["errors"]

        for identifier in sorted(self._touched_setpoints):
            device = self._initial_snapshot.devices.get(identifier)
            if device is None:
                errors.append(f"{identifier} setpoint: original device missing")
                continue
            try:
                original = round(float(device["spvalue"]))
                snapshot = await self._api_client.devices()
                current = self._require_device(snapshot, identifier)
                current_value = round(float(current["spvalue"]))
                if current_value != original:
                    if identifier != POOL_HEATER:
                        raise ScenarioFailure(
                            f"No restoration programmer markers for {identifier}"
                        )
                    await self._set_setpoint(
                        context,
                        identifier,
                        original,
                        phase="restoration.setpoint",
                        active_marker=POOL_HEATER_SETPOINT_ACTIVE,
                        completion_marker=POOL_HEATER_SETPOINT_FINISHED,
                        category="restoration",
                    )
                restoration["actions"].append(
                    {
                        "target": identifier,
                        "property": "setpoint",
                        "value": original,
                        "status": "restored",
                    }
                )
            except Exception as error:
                errors.append(f"{identifier} setpoint: {error}")

        identifiers = sorted(
            self._touched_devices,
            key=lambda value: value == FILTER_PUMP,
        )
        for identifier in identifiers:
            try:
                expected = self._initial_device_enabled(identifier)
                if await self._current_device_enabled(identifier) != expected:
                    await self._set_device(
                        context,
                        identifier,
                        expected,
                        phase="restoration.device",
                    )
                restoration["actions"].append(
                    {
                        "target": identifier,
                        "property": "state",
                        "value": expected,
                        "status": "restored",
                    }
                )
            except Exception as error:
                errors.append(f"{identifier} state: {error}")

        restoration["status"] = "failed" if errors else "passed"
        return errors

    def _append_measurement(
        self,
        *,
        name: str,
        category: str,
        phase: str,
        target: str,
        requested_value: Any,
        start_offset_ns: int,
        api_ack_offset_ns: int | None,
        log_completion_offset_ns: int | None,
        state_observed_offset_ns: int | None,
        task_active_offset_ns: int | None = None,
        status: str = "passed",
    ) -> None:
        completion_offsets = [
            offset
            for offset in (
                task_active_offset_ns,
                log_completion_offset_ns,
                state_observed_offset_ns,
                api_ack_offset_ns,
            )
            if offset is not None
        ]
        completed = max(completion_offsets, default=start_offset_ns)
        measurement = {
            "name": name,
            "category": category,
            "status": status,
            "phase": phase,
            "target": target,
            "requested_value": requested_value,
            "start_offset_ns": start_offset_ns,
            "api_ack_offset_ns": api_ack_offset_ns,
            "task_active_offset_ns": task_active_offset_ns,
            "log_completion_offset_ns": log_completion_offset_ns,
            "state_observed_offset_ns": state_observed_offset_ns,
            "completed_offset_ns": completed,
            "duration_ms": round((completed - start_offset_ns) / 1_000_000, 3),
            "api_ack_ms": (
                round((api_ack_offset_ns - start_offset_ns) / 1_000_000, 3)
                if api_ack_offset_ns is not None
                else None
            ),
            "activation_ms": (
                round(
                    (task_active_offset_ns - start_offset_ns) / 1_000_000,
                    3,
                )
                if task_active_offset_ns is not None
                else None
            ),
            "programmer_duration_ms": (
                round(
                    (
                        log_completion_offset_ns - task_active_offset_ns
                    )
                    / 1_000_000,
                    3,
                )
                if (
                    task_active_offset_ns is not None
                    and log_completion_offset_ns is not None
                )
                else None
            ),
            "state_convergence_ms": (
                round(
                    (
                        state_observed_offset_ns - log_completion_offset_ns
                    )
                    / 1_000_000,
                    3,
                )
                if (
                    log_completion_offset_ns is not None
                    and state_observed_offset_ns is not None
                )
                else None
            ),
        }
        self._report["measurements"].append(measurement)

    async def _wait_for_state_or_programmer_error(
        self,
        context: ScenarioContext,
        *,
        task_name: str,
        after: int,
        state_wait: Coroutine[Any, Any, int],
    ) -> int:
        state_task: asyncio.Task[int] = asyncio.create_task(state_wait)
        error_task: asyncio.Task[LineEvent] = asyncio.create_task(
            context.monitor.wait_for(
                f"PDA Device programmer '{task_name}' didn't find",
                after=after,
                timeout_seconds=self._config.state_timeout_seconds,
            )
        )
        done, _ = await asyncio.wait(
            {state_task, error_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if error_task in done:
            try:
                error_event = error_task.result()
            except TimeoutError:
                return await state_task
            state_task.cancel()
            await asyncio.gather(state_task, return_exceptions=True)
            raise ScenarioFailure(error_event.text.strip())

        error_task.cancel()
        await asyncio.gather(error_task, return_exceptions=True)
        return state_task.result()

    def _skip(self, name: str, reason: str) -> None:
        self._report["skipped"].append({"name": name, "reason": reason})
        print(f"[ SKIP ] {name} — {reason}", flush=True)

    def _write_report(self, context: ScenarioContext) -> None:
        (context.artifact_dir / "scenario.json").write_text(
            json.dumps(self._report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _require_device(
        snapshot: DeviceSnapshot,
        identifier: str,
    ) -> dict[str, Any]:
        try:
            return snapshot.devices[identifier]
        except KeyError as error:
            raise ScenarioFailure(
                f"Required device {identifier} is absent from /api/devices"
            ) from error

    @staticmethod
    def _device_enabled(device: dict[str, Any]) -> bool:
        value = device.get("int_status")
        if not isinstance(value, (int, str)):
            raise ScenarioFailure(
                f"Device {device.get('id', '<unknown>')} has invalid "
                f"int_status {value!r}"
            )
        try:
            return int(value) != 0
        except (TypeError, ValueError) as error:
            raise ScenarioFailure(
                f"Device {device.get('id', '<unknown>')} has invalid "
                f"int_status {value!r}"
            ) from error
