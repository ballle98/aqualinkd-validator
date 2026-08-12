from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import normalize_api_base_url
from .domain import DeviceState, EquipmentSnapshot, EquipmentStateError
from .engine import RestorationSession
from .http_api import ApiError, AqualinkHttpApi
from .interfaces import AqualinkApi
from .pda.cases import CASES, PdaCaseDefinition, PdaCaseId
from .pda_simulator import (
    AquaPdaSimulator,
    PdaSimulatorClient,
    SimulatorProtocolError,
)
from .protocols.pda import PdaProgrammerFailure, PdaProgrammerObserver
from .supervisor import LineEvent, ScenarioContext, ScenarioOutcome

FILTER_PUMP = "Filter_Pump"
POOL_HEATER = "Pool_Heater"

INIT_FINISHED = "(Init PDA) finished"
INIT_ACTIVE = "is active (Init PDA)"
DEVICE_FINISHED = "(Switch PDA device on/off) finished"
DEVICE_ACTIVE = "is active (Switch PDA device on/off)"
POOL_HEATER_SETPOINT_FINISHED = "(Set PDA Pool Heater) finished"
POOL_HEATER_SETPOINT_ACTIVE = "is active (Set PDA Pool Heater)"
LEGACY_POOL_HEATER_SETPOINT_FINISHED = "(Set Pool heater setpoint) finished"
LEGACY_POOL_HEATER_SETPOINT_ACTIVE = "is active (Set Pool heater setpoint)"
POOL_HEATER_SETPOINT_FINISHED_MARKERS = (
    POOL_HEATER_SETPOINT_FINISHED,
    LEGACY_POOL_HEATER_SETPOINT_FINISHED,
)
POOL_HEATER_SETPOINT_ACTIVE_MARKERS = (
    POOL_HEATER_SETPOINT_ACTIVE,
    LEGACY_POOL_HEATER_SETPOINT_ACTIVE,
)
STATUS_MENU_PRESENT = "PDA Start new Equiptment loop"
LEGACY_STATUS_MENU_PRESENT = "PDA Start new Equipment loop"
STATUS_MENU_PRESENT_MARKERS = (
    STATUS_MENU_PRESENT,
    LEGACY_STATUS_MENU_PRESENT,
)
STATUS_MENU_FINISHED = "PDA End Equiptment loop"
LEGACY_STATUS_MENU_FINISHED = "PDA End Equipment loop"
STATUS_MENU_FINISHED_MARKERS = (
    STATUS_MENU_FINISHED,
    LEGACY_STATUS_MENU_FINISHED,
)
PDA_SLEEPING = "PDA Aqualink daemon in sleep mode"
PDA_ADDRESS_STATUS = "To 0x60 of type           Status"
PDA_ADDRESS_PROBE = "To 0x60 of type            Probe"
WAKE_INIT_ACTIVE = "is active (PDA init after wake)"
WAKE_INIT_FINISHED = "(PDA init after wake) finished"
FIRMWARE_VERSION_SCREEN = "PDA Menu Line 3 = Firmware Version"
WEB_SERVER_STARTED = "Starting web server on "

_EQUIPMENT_STABLE_SECONDS = 0.5
_EQUIPMENT_POLL_SECONDS = 0.25

_PDA_MENU_LINE = re.compile(r"PDA Menu Line (\d+) =\s*(.*?)\s*$")
_WEB_SERVER_URL = re.compile(r"Starting web server on\s+(\S+)")
_WEB_SERVER_PORT = re.compile(r"Starting web server on port\s+(\d+)")
_AQUALINKD_VERSION = re.compile(
    r"(?:Starting\s+)?Aqualink Daemon\s+(v.+?)(?:\s+!\s*)?$",
    re.IGNORECASE,
)
_CONFIGURED_PANEL = re.compile(
    r"(?:Panel set to|panel type\s*=)\s*(.+?)\s*$",
    re.IGNORECASE,
)
_AUX_IDENTIFIER = re.compile(r"Aux_(\d+)$", re.IGNORECASE)
_STATUS_MESSAGE = re.compile(r"\*\*\* Pass Equiptment msg '([^']*)'")
_FOUND_STATUS = re.compile(
    r"Found(?: EQ CTL)? Status for (.+?)\s*=\s*['\"]?(.+?)['\"]?\s*$",
    re.IGNORECASE,
)
_SWG_PERCENT = re.compile(r"AquaPure\s*=\s*(\d+)", re.IGNORECASE)
_SERIAL_SEND_TIME = re.compile(
    r"Time from recv to (?:blocking )?send is\s+([0-9.]+)\s+sec",
    re.IGNORECASE,
)
_TestResult = TypeVar("_TestResult")


@dataclass(frozen=True)
class PdaScenarioConfig:
    suite_name: str = "pda-live-fast"
    include_state_waits: bool = False
    execution_phase: Literal["single", "awake", "sleep"] = "single"
    activation_timeout_seconds: float = 130.0
    action_timeout_seconds: float = 90.0
    status_timeout_seconds: float = 180.0
    state_timeout_seconds: float = 10.0
    restoration_timeout_seconds: float = 300.0
    init_timeout_seconds: float = 180.0
    sleep_timeout_seconds: float = 120.0
    status_retry_command_delay_seconds: float = 1.0
    probe_command_min_delay_seconds: float = 3.0
    test_devices: tuple[str, ...] = ()
    disabled_button_numbers: tuple[int, ...] = ()
    panel_timezone: str = "UTC"
    panel_time_tolerance_seconds: float = 120.0
    case_ids: tuple[PdaCaseId, ...] = ()
    simulator_packet_count: int = 20
    simulator_timeout_seconds: float = 20.0


class ScenarioFailure(RuntimeError):
    """Raised when an expected PDA state transition does not complete."""


class PdaLivePanelScenario:
    def __init__(
        self,
        api: AqualinkApi | None,
        config: PdaScenarioConfig,
        *,
        api_base_url_override: str | None = None,
        api_factory: Callable[[str], AqualinkApi] = AqualinkHttpApi,
        simulator_factory: Callable[[str], PdaSimulatorClient] = (AquaPdaSimulator),
    ) -> None:
        self._api = api
        self._api_base_url_override = api_base_url_override
        self._api_factory = api_factory
        self._simulator_factory = simulator_factory
        self._config = config
        self._programmer = PdaProgrammerObserver()
        self._case_ids = self._resolve_case_ids(config)
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
            "execution_phase": config.execution_phase,
            "api_base_url": (
                api.base_url if api is not None else api_base_url_override
            ),
            "api_endpoint_source": endpoint_source,
            "status": "running",
            "reason": None,
            "safe_to_continue": False,
            "timeouts_seconds": {
                "activation": config.activation_timeout_seconds,
                "action": config.action_timeout_seconds,
                "status": config.status_timeout_seconds,
                "state": config.state_timeout_seconds,
                "restoration": config.restoration_timeout_seconds,
                "init": config.init_timeout_seconds,
                "sleep": config.sleep_timeout_seconds,
                "status_retry_command_delay": (
                    config.status_retry_command_delay_seconds
                ),
                "probe_command_min_delay": (config.probe_command_min_delay_seconds),
            },
            "checks": [],
            "aqualinkd": None,
            "panel": None,
            "equipment_status": None,
            "equipment_state_observations": [],
            "sleep_cycle": None,
            "simulator": None,
            "menu_walk": None,
            "cases": [],
            "measurements": [],
            "skipped": [],
            "device_selection": {
                "mode": (
                    "not_applicable"
                    if (not self._uses_selected_devices())
                    else (
                        "restricted"
                        if config.test_devices
                        else (
                            "all_discovered_switches"
                            if PdaCaseId.CONSECUTIVE_DEVICES in self._case_ids
                            else "auto_last_switch"
                        )
                    )
                ),
                "requested": list(config.test_devices),
                "resolved": [],
                "configured_none_buttons": list(config.disabled_button_numbers),
                "reported_panel_size": None,
                "excluded": [],
            },
            "restoration": {
                "attempted": False,
                "status": "not-needed",
                "actions": [],
                "errors": [],
            },
        }
        self._initial_snapshot: EquipmentSnapshot | None = None
        self._restoration = RestorationSession()
        self._button_number_by_identifier: dict[str, int] = {}
        self._excluded_device_ids: set[str] = set()
        self._reported_panel_size: int | None = None
        self._reported_panel_combo: bool | None = None

    async def run(self, context: ScenarioContext) -> ScenarioOutcome:
        suite_started = time.monotonic()
        display_name = self._config.suite_name
        print(
            f"\n=== Starting {display_name} ===",
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
        case_failures: list[str] = []
        for case_id in self._case_ids:
            case = CASES[case_id]
            self._restoration.begin_case()
            case_started = time.monotonic()
            case_error: BaseException | None = None
            try:
                await self._run_test(
                    case.name,
                    self._case_operation(case_id, context),
                )
            except asyncio.CancelledError as error:
                case_error = error
                cancelled = True
            except Exception as error:
                case_error = error

            restoration_errors = await self._restore_after_case(context, case)
            case_status = "passed" if case_error is None else "failed"
            case_result = {
                "id": case.id.value,
                "name": case.name,
                "status": case_status,
                "duration_ms": round(
                    (time.monotonic() - case_started) * 1000,
                    3,
                ),
                "error": (
                    self._format_exception(case_error)
                    if case_error is not None
                    else None
                ),
                "restoration": (
                    "failed"
                    if restoration_errors
                    else ("passed" if case.mutates_panel else "not-needed")
                ),
            }
            self._report["cases"].append(case_result)

            if restoration_errors:
                status = "failed"
                reason = "restoration_failed"
                self._report["error"] = "; ".join(restoration_errors)
                break
            if cancelled:
                status = "failed"
                reason = "scenario_cancelled"
                break
            if case_error is not None:
                case_failures.append(case.id.value)
                if case.id == PdaCaseId.INITIALIZATION:
                    status = "failed"
                    reason = "initialization_failed"
                    self._report["error"] = self._format_exception(case_error)
                    break

        if reason == "scenario_completed" and case_failures:
            status = "failed"
            reason = "case_failures"
            self._report["failed_cases"] = case_failures

        final_restoration_errors: list[str] = []
        if self._restoration.has_pending_mutations:
            final_restoration_errors = await self._restore_with_progress(
                context,
                "Restore original equipment state",
            )
        if final_restoration_errors:
            status = "failed"
            reason = "restoration_failed"
            self._report["error"] = "; ".join(final_restoration_errors)
        elif reason == "restoration_failed":
            # The case-level cleanup exceeded its window, but the final
            # safety pass subsequently verified the original state. Keep the
            # run failed while allowing a composite suite to continue.
            reason = "restoration_recovered"
            self._report["restoration"]["status"] = "recovered"

        self._report["safe_to_continue"] = bool(
            self._initial_snapshot is not None
            and not cancelled
            and reason != "restoration_failed"
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
            display_name,
            suite_started,
            passed=status == "passed",
            detail=None if status == "passed" else reason,
        )
        if cancelled:
            raise asyncio.CancelledError
        return ScenarioOutcome(status=status, reason=reason)

    @staticmethod
    def _resolve_case_ids(config: PdaScenarioConfig) -> tuple[PdaCaseId, ...]:
        if config.case_ids:
            return config.case_ids
        case_ids = [
            PdaCaseId.INITIALIZATION,
            PdaCaseId.FILTER_AFTER_INIT,
            PdaCaseId.POOL_HEATER,
        ]
        if config.include_state_waits and config.execution_phase != "sleep":
            case_ids.extend(
                (
                    PdaCaseId.EQUIPMENT_STATUS,
                    PdaCaseId.CONSECUTIVE_DEVICES,
                )
            )
        if config.include_state_waits and config.execution_phase != "awake":
            case_ids.extend(
                (
                    PdaCaseId.SLEEP_CYCLE,
                    PdaCaseId.DEVICE_DURING_STATUS_RETRY,
                    PdaCaseId.DEVICE_AFTER_PROBE,
                )
            )
        if config.execution_phase == "sleep":
            case_ids = [
                PdaCaseId.INITIALIZATION,
                PdaCaseId.SLEEP_CYCLE,
                PdaCaseId.DEVICE_DURING_STATUS_RETRY,
                PdaCaseId.DEVICE_AFTER_PROBE,
            ]
        return tuple(case_ids)

    def _uses_selected_devices(self) -> bool:
        return any(
            case_id in self._case_ids
            for case_id in (
                PdaCaseId.CONSECUTIVE_DEVICES,
                PdaCaseId.DEVICE_DURING_STATUS_RETRY,
                PdaCaseId.DEVICE_AFTER_PROBE,
            )
        )

    def _case_operation(
        self,
        case_id: PdaCaseId,
        context: ScenarioContext,
    ) -> Callable[[], Awaitable[None]]:
        operations: dict[PdaCaseId, Callable[[], Awaitable[None]]] = {
            PdaCaseId.INITIALIZATION: lambda: self._initialize(context),
            PdaCaseId.FILTER_AFTER_INIT: lambda: (
                self._toggle_round_trip_unless_disabled(
                    context,
                    FILTER_PUMP,
                    phase="devices.after_init",
                )
            ),
            PdaCaseId.POOL_HEATER: lambda: self._test_pool_heater(context),
            PdaCaseId.EQUIPMENT_STATUS: lambda: self._test_with_status_menu(context),
            PdaCaseId.CONSECUTIVE_DEVICES: lambda: self._test_consecutive_devices(
                context
            ),
            PdaCaseId.SLEEP_CYCLE: lambda: self._test_sleep_wake_cycle(context),
            PdaCaseId.DEVICE_DURING_STATUS_RETRY: lambda: (
                self._test_device_during_status_retry(context)
            ),
            PdaCaseId.DEVICE_AFTER_PROBE: lambda: self._test_device_after_probe(
                context
            ),
            PdaCaseId.SIMULATOR_TRANSPORT: lambda: self._test_simulator_transport(
                context
            ),
            PdaCaseId.MENU_WALK: lambda: self._test_menu_walk(context),
        }
        return operations[case_id]

    async def _test_simulator_transport(
        self,
        context: ScenarioContext,
    ) -> None:
        api = self._api_client
        simulator = self._simulator_factory(api.base_url)
        log_cursor = context.monitor.cursor
        packet_start = 0
        try:
            print(
                "[ WAIT ] Activating AquaPDA WebSocket simulator and "
                "observing RS485 traffic",
                flush=True,
            )
            await simulator.connect()
            packet_start = simulator.packet_count
            await simulator.wait_for_packets(
                self._config.simulator_packet_count,
                after=packet_start,
                timeout_seconds=self._config.simulator_timeout_seconds,
            )
            before_back = simulator.packet_count
            await simulator.send_key("back")
            await simulator.wait_for_packets(
                2,
                after=before_back,
                timeout_seconds=self._config.simulator_timeout_seconds,
            )
        finally:
            await simulator.close()

        events = [
            event
            for event in context.monitor.recent_events()
            if event.sequence > log_cursor
        ]
        corruption = [
            event.text
            for event in events
            if (
                "Serial read bad Jandy checksum" in event.text
                or "BAD PACKET" in event.text
            )
        ]
        navigation_failures = [
            event.text
            for event in events
            if any(
                marker in event.text
                for marker in (
                    "waitForPDAnextMenu - received STATUS instead of CLEAR",
                    "can't goto PM_EQUIPTMENT_CONTROL menu",
                    "PDA Wake Init :- can't find menu",
                )
            )
        ]
        send_times = [
            float(match.group(1))
            for event in events
            if (match := _SERIAL_SEND_TIME.search(event.text)) is not None
        ]
        slow_send_times = [value for value in send_times if value > 0.010]
        packet_count = simulator.packet_count - packet_start
        self._report["simulator"] = {
            "packets_observed": packet_count,
            "bad_packets": corruption,
            "navigation_failures": navigation_failures,
            "send_time_samples_seconds": send_times,
            "maximum_send_time_seconds": max(send_times, default=None),
            "send_time_limit_seconds": 0.010,
            "slow_send_times_seconds": slow_send_times,
        }
        print(
            f"[STATE ] AquaPDA simulator delivered {packet_count} packets; "
            f"BAD PACKET count {len(corruption)}",
            flush=True,
        )
        if corruption:
            raise ScenarioFailure(
                "AquaPDA simulator caused bad-checksum/BAD PACKET traffic "
                f"({len(corruption)} log entries); see ballle98/AqualinkD#94 "
                "and ballle98/AqualinkD#95"
            )
        if navigation_failures:
            raise ScenarioFailure(
                "AquaPDA simulator caused PDA navigation failures: "
                + "; ".join(navigation_failures)
            )
        if slow_send_times:
            raise ScenarioFailure(
                "AquaPDA ACK path exceeded the 10ms transport budget: "
                + ", ".join(f"{value:.3f}s" for value in slow_send_times)
            )

    async def _test_menu_walk(self, context: ScenarioContext) -> None:
        api = self._api_client
        simulator = self._simulator_factory(api.base_url)
        visited: list[dict[str, Any]] = []
        try:
            await simulator.connect()
            await simulator.wait_for_packets(
                6,
                timeout_seconds=self._config.simulator_timeout_seconds,
            )
            await simulator.wait_for_screen_settle(
                after=0,
                timeout_seconds=self._config.simulator_timeout_seconds,
            )
            await self._return_simulator_to_home(simulator)
            await self._walk_read_only_menus(
                simulator,
                path=("HOME",),
                visited=visited,
                depth=0,
            )
        finally:
            await simulator.close()
        self._report["menu_walk"] = {
            "screens_visited": len(visited),
            "screens": visited,
        }
        print(
            f"[STATE ] PDA menu walk visited {len(visited)} screens",
            flush=True,
        )
        if len(visited) < 2:
            raise ScenarioFailure("PDA menu walk did not reach the main menu")

    async def _return_simulator_to_home(
        self,
        simulator: PdaSimulatorClient,
    ) -> None:
        for _ in range(8):
            visible = {line.strip() for line in simulator.screen.lines}
            if {"MENU", "EQUIPMENT ON/OFF"}.issubset(visible):
                print(
                    "[STATE ] PDA simulator returned to the home screen",
                    flush=True,
                )
                return
            before_packets = simulator.packet_count
            before_updates = simulator.screen_update_count
            previous_screen = tuple(simulator.screen.lines)
            await simulator.send_key("back")
            await simulator.wait_for_screen_change(
                previous_screen,
                after=before_packets,
                timeout_seconds=self._config.simulator_timeout_seconds,
            )
            await simulator.wait_for_screen_settle(
                after=before_updates,
                timeout_seconds=self._config.simulator_timeout_seconds,
                idle_seconds=0.5,
            )
        raise ScenarioFailure(
            "PDA menu walk could not identify the home screen containing "
            "MENU and EQUIPMENT ON/OFF"
        )

    async def _walk_read_only_menus(
        self,
        simulator: PdaSimulatorClient,
        *,
        path: tuple[str, ...],
        visited: list[dict[str, Any]],
        depth: int,
    ) -> None:
        if depth > 8 or len(visited) >= 100:
            raise ScenarioFailure("PDA menu walk exceeded its traversal bound")
        options = await self._enumerate_menu_options(simulator)
        display_path = " / ".join(path)
        print(
            f"[ WALK ] {display_path}: {len(options)} selectable item(s)",
            flush=True,
        )
        visited.append(
            {
                "path": list(path),
                "title": simulator.screen.title,
                "lines": [line.rstrip() for line in simulator.screen.lines],
                "options": options,
            }
        )
        candidates = [
            option
            for option in options
            if option in {"MENU", "EQUIPMENT ON/OFF"} or option.endswith(">")
        ]
        for option in candidates:
            await self._move_to_menu_option(simulator, option)
            before_packets = simulator.packet_count
            before_updates = simulator.screen_update_count
            previous_screen = tuple(simulator.screen.lines)
            await simulator.send_key("select")
            await simulator.wait_for_screen_change(
                previous_screen,
                after=before_packets,
                timeout_seconds=self._config.simulator_timeout_seconds,
            )
            await simulator.wait_for_screen_settle(
                after=before_updates,
                timeout_seconds=self._config.simulator_timeout_seconds,
                idle_seconds=0.5,
            )
            await self._walk_read_only_menus(
                simulator,
                path=(*path, option.rstrip(" >")),
                visited=visited,
                depth=depth + 1,
            )
            before_packets = simulator.packet_count
            before_updates = simulator.screen_update_count
            previous_screen = tuple(simulator.screen.lines)
            await simulator.send_key("back")
            await simulator.wait_for_screen_change(
                previous_screen,
                after=before_packets,
                timeout_seconds=self._config.simulator_timeout_seconds,
            )
            await simulator.wait_for_screen_settle(
                after=before_updates,
                timeout_seconds=self._config.simulator_timeout_seconds,
                idle_seconds=0.5,
            )

    async def _enumerate_menu_options(
        self,
        simulator: PdaSimulatorClient,
    ) -> list[str]:
        first = simulator.screen.highlighted_text
        if not first:
            return []
        options = [first]
        for _ in range(31):
            before_packets = simulator.packet_count
            before_updates = simulator.screen_update_count
            previous = simulator.screen.highlighted_text
            await simulator.send_key("down")
            try:
                await simulator.wait_for_highlight_change(
                    previous,
                    after=before_packets,
                    timeout_seconds=min(
                        2.0,
                        self._config.simulator_timeout_seconds,
                    ),
                )
            except SimulatorProtocolError as error:
                if "highlight did not change" not in str(error):
                    raise
                break
            await simulator.wait_for_screen_settle(
                after=before_updates,
                timeout_seconds=self._config.simulator_timeout_seconds,
                idle_seconds=0.5,
            )
            current = simulator.screen.highlighted_text
            if not current or current in options:
                break
            options.append(current)
        return options

    async def _move_to_menu_option(
        self,
        simulator: PdaSimulatorClient,
        target: str,
    ) -> None:
        for _ in range(32):
            current = simulator.screen.highlighted_text
            if current == target:
                return
            before_packets = simulator.packet_count
            before_updates = simulator.screen_update_count
            await simulator.send_key("down")
            await simulator.wait_for_highlight_change(
                current,
                after=before_packets,
                timeout_seconds=self._config.simulator_timeout_seconds,
            )
            await simulator.wait_for_screen_settle(
                after=before_updates,
                timeout_seconds=self._config.simulator_timeout_seconds,
                idle_seconds=0.5,
            )
        raise ScenarioFailure(f"PDA menu item disappeared during walk: {target}")

    async def _restore_after_case(
        self,
        context: ScenarioContext,
        case: PdaCaseDefinition,
    ) -> list[str]:
        if not case.mutates_panel:
            return []
        return await self._restore_with_progress(
            context,
            f"Restore state after {case.name}",
        )

    async def _restore_with_progress(
        self,
        context: ScenarioContext,
        name: str,
    ) -> list[str]:
        started = self._progress_started(name)
        errors = await self._restore_original_state(context)
        self._progress_finished(
            name,
            started,
            passed=not errors,
            detail="; ".join(errors) if errors else None,
        )
        return errors

    async def _initialize(self, context: ScenarioContext) -> None:
        init_screen = await self._prepare_startup(context)
        ready_snapshot = await self._wait_for_api()
        identifiers = self._actionable_identifiers(ready_snapshot)
        self._initial_snapshot = await self._wait_for_stable_equipment_snapshot(
            context,
            identifiers,
            phase="initialization.snapshot",
            timeout_seconds=self._config.init_timeout_seconds,
            initial_snapshot=ready_snapshot,
        )
        self._restoration.capture_initial(self._initial_snapshot)
        await self._record_panel_identity_and_check_time(init_screen)
        self._record_device_constraints()
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
                detail=self._format_exception(error),
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
        marker: str | tuple[str, ...],
        after: int,
        requested_offset_ns: int,
        timeout_seconds: float,
        wait_reason: str = "waiting in the programmer queue",
    ) -> LineEvent:
        try:
            return await self._programmer.wait_for_active(
                context.monitor,
                context.timeline,
                task_name=task_name,
                marker=marker,
                after=after,
                requested_offset_ns=requested_offset_ns,
                timeout_seconds=timeout_seconds,
                wait_reason=wait_reason,
            )
        except PdaProgrammerFailure as error:
            raise ScenarioFailure(str(error)) from error

    async def _wait_for_task_completion(
        self,
        context: ScenarioContext,
        *,
        task_name: str,
        marker: str | tuple[str, ...],
        active: LineEvent,
        timeout_seconds: float,
    ) -> LineEvent:
        try:
            return await self._programmer.wait_for_completion(
                context.monitor,
                context.timeline,
                task_name=task_name,
                marker=marker,
                active=active,
                timeout_seconds=timeout_seconds,
            )
        except PdaProgrammerFailure as error:
            raise ScenarioFailure(str(error)) from error

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
            identity_task = tasks.create_task(self._capture_aqualinkd_identity(context))
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
        identity = identity_task.result()
        self._report["aqualinkd"] = identity
        print(
            f"[INFO  ] AqualinkD version: {identity['version']}",
            flush=True,
        )
        print(
            f"[INFO  ] Configured panel: {identity['configured_panel_type']}",
            flush=True,
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
        port_match = _WEB_SERVER_PORT.search(event.text)
        if port_match is not None:
            return normalize_api_base_url(f"http://127.0.0.1:{port_match.group(1)}")

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

    async def _capture_aqualinkd_identity(
        self,
        context: ScenarioContext,
    ) -> dict[str, str]:
        async with asyncio.TaskGroup() as tasks:
            version_task = tasks.create_task(
                context.monitor.wait_for(
                    "Aqualink Daemon v",
                    timeout_seconds=self._config.init_timeout_seconds,
                )
            )
            panel_task = tasks.create_task(
                context.monitor.wait_for_any(
                    ("Panel set to ", "panel type"),
                    timeout_seconds=self._config.init_timeout_seconds,
                )
            )

        version_match = _AQUALINKD_VERSION.search(version_task.result().text)
        if version_match is None:
            raise ScenarioFailure(
                "AqualinkD startup log did not contain a parseable version"
            )
        panel_match = _CONFIGURED_PANEL.search(panel_task.result().text)
        if panel_match is None:
            raise ScenarioFailure(
                "AqualinkD startup log did not contain a parseable panel type"
            )
        return {
            "version": version_match.group(1).strip(),
            "configured_panel_type": panel_match.group(1).strip(),
            "source": "aqualinkd_startup_log",
        }

    @staticmethod
    async def _wait_for_marker(
        context: ScenarioContext,
        marker: str | tuple[str, ...],
        *,
        after: int,
        timeout_seconds: float,
    ) -> LineEvent:
        if isinstance(marker, str):
            return await context.monitor.wait_for(
                marker,
                after=after,
                timeout_seconds=timeout_seconds,
            )
        return await context.monitor.wait_for_any(
            marker,
            after=after,
            timeout_seconds=timeout_seconds,
        )

    def _configure_api(self, base_url: str, *, source: str) -> None:
        self._api = self._api_factory(base_url)
        self._report["api_base_url"] = self._api.base_url
        self._report["api_endpoint_source"] = source

    @property
    def _api_client(self) -> AqualinkApi:
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
                "PDA firmware-version screen did not contain a panel type on line 1"
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
        daemon_identity = self._report.get("aqualinkd")
        configured_panel = (
            daemon_identity.get("configured_panel_type")
            if isinstance(daemon_identity, dict)
            else None
        )
        reported_panel = init_screen["panel_type"]
        reported_signature = self._panel_signature(reported_panel)
        self._reported_panel_size = reported_signature[1]
        self._reported_panel_combo = reported_signature[2]
        self._report["device_selection"]["reported_panel_size"] = (
            self._reported_panel_size
        )
        print(
            f"[INFO  ] Panel reported: {reported_panel}; "
            f"firmware {init_screen['firmware']}",
            flush=True,
        )
        panel_type_check = self._panel_type_check(
            configured_panel,
            reported_panel,
        )
        self._report["checks"].append(panel_type_check)
        if panel_type_check["status"] == "warning":
            print(
                "[ WARN ] Configured panel type does not match the "
                f"physical panel: configured {configured_panel}; "
                f"reported {reported_panel}",
                flush=True,
            )

        try:
            timezone = ZoneInfo(self._config.panel_timezone)
        except ZoneInfoNotFoundError as error:
            raise ScenarioFailure(
                f"Unknown panel timezone: {self._config.panel_timezone}"
            ) from error
        deadline = asyncio.get_running_loop().time() + self._config.init_timeout_seconds
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
            self._report["panel"]["api_status_after_clock_sync"] = final_identity
        self._report["checks"].append(
            {
                "name": "panel.time",
                "status": "passed" if passed else "failed",
                "panel_time": panel_time.strip(),
                "system_time": now.isoformat(),
                "timezone": self._config.panel_timezone,
                "difference_seconds": difference,
                "waited_seconds": round(waited_seconds, 3),
                "tolerance_seconds": (self._config.panel_time_tolerance_seconds),
            }
        )
        if not passed:
            raise ScenarioFailure(
                f"Panel time differs from {self._config.panel_timezone} "
                f"system time by {difference}s; tolerance is "
                f"{self._config.panel_time_tolerance_seconds:g}s"
            )

    @classmethod
    def _panel_type_check(
        cls,
        configured: str | None,
        reported: str,
    ) -> dict[str, Any]:
        configured_signature = cls._panel_signature(configured)
        reported_signature = cls._panel_signature(reported)
        comparable = (
            configured_signature[0] is not None
            and reported_signature[0] is not None
            and configured_signature[1] is not None
            and reported_signature[1] is not None
        )
        matches = comparable and configured_signature == reported_signature
        return {
            "name": "panel.type",
            "status": "passed" if matches else "warning",
            "configured": configured,
            "reported": reported,
            "configured_signature": list(configured_signature),
            "reported_signature": list(reported_signature),
            "reason": (
                None
                if matches
                else "Configured panel identity differs from panel screen"
            ),
        }

    @staticmethod
    def _panel_signature(
        value: str | None,
    ) -> tuple[str | None, int | None, bool | None]:
        if value is None:
            return (None, None, None)
        normalized = value.upper()
        family = "PDA" if "PDA" in normalized else None
        capacity_match = re.search(r"PDA-(?:PS)?(\d+)", normalized)
        capacity = int(capacity_match.group(1)) if capacity_match else None
        combo = "COMBO" in normalized if family is not None else None
        return (family, capacity, combo)

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

    async def _wait_for_api(self) -> EquipmentSnapshot:
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
    def _actionable_identifiers(snapshot: EquipmentSnapshot) -> tuple[str, ...]:
        return tuple(
            identifier
            for identifier, device in snapshot.devices.items()
            if device.get("type") in {"switch", "setpoint_thermo"}
        )

    async def _wait_for_stable_equipment_snapshot(
        self,
        context: ScenarioContext,
        identifiers: tuple[str, ...] | list[str],
        *,
        phase: str,
        timeout_seconds: float,
        initial_snapshot: EquipmentSnapshot | None = None,
    ) -> EquipmentSnapshot:
        selected = tuple(identifiers)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        stable_since: float | None = None
        previous_signature: tuple[tuple[str, int, str, str], ...] | None = None
        recorded_signature: tuple[tuple[str, int, str, str], ...] | None = None
        snapshot = initial_snapshot
        print(
            f"[ WAIT ] Equipment state: waiting for {phase} to stabilize "
            f"(timeout {timeout_seconds:g}s)",
            flush=True,
        )
        while asyncio.get_running_loop().time() < deadline:
            if snapshot is None:
                snapshot = await self._api_client.devices()
            states = {
                identifier: self._device_state_details(
                    self._require_device(snapshot, identifier)
                )
                for identifier in selected
            }
            signature = tuple(
                (
                    identifier,
                    state["int_status"],
                    state["state"],
                    state["status"],
                )
                for identifier, state in states.items()
            )
            pending = [
                identifier
                for identifier, state in states.items()
                if state["transitioning"]
            ]
            now = asyncio.get_running_loop().time()
            if pending or signature != previous_signature:
                stable_since = None if pending else now
            elif stable_since is None:
                stable_since = now

            if signature != recorded_signature:
                await self._record_equipment_observation(
                    context,
                    phase=phase,
                    states=states,
                    pending=pending,
                    stable=False,
                )
                recorded_signature = signature

            if (
                not pending
                and stable_since is not None
                and now - stable_since >= _EQUIPMENT_STABLE_SECONDS
            ):
                await self._record_equipment_observation(
                    context,
                    phase=phase,
                    states=states,
                    pending=[],
                    stable=True,
                )
                print(
                    f"[STATE ] Equipment state stable for {phase}",
                    flush=True,
                )
                return snapshot

            previous_signature = signature
            snapshot = None
            await asyncio.sleep(_EQUIPMENT_POLL_SECONDS)

        pending_text = ", ".join(pending) if pending else "state kept changing"
        raise ScenarioFailure(
            f"Equipment state did not stabilize for {phase} within "
            f"{timeout_seconds:g}s ({pending_text})"
        )

    async def _record_equipment_observation(
        self,
        context: ScenarioContext,
        *,
        phase: str,
        states: dict[str, dict[str, Any]],
        pending: list[str],
        stable: bool,
    ) -> None:
        observation = {
            "offset_ns": context.timeline.offset_ns(),
            "phase": phase,
            "stable": stable,
            "pending": pending,
            "devices": states,
        }
        self._report["equipment_state_observations"].append(observation)
        await context.timeline.write(
            "equipment_state_observation",
            phase=phase,
            stable=stable,
            pending=pending,
            devices=states,
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
        assert self._initial_snapshot is not None
        controls = [
            identifier
            for identifier, device in self._initial_snapshot.devices.items()
            if device.get("type") in {"switch", "setpoint_thermo"}
            and not self._skip_unactionable_device(
                identifier,
                phase="devices.status_menu.setup",
            )
        ]
        if not controls:
            self._skip(
                "devices.status_menu",
                "No configured equipment can be enabled for status testing",
            )
            return

        await self._wait_for_stable_equipment_snapshot(
            context,
            controls,
            phase="devices.status_menu.precondition",
            timeout_seconds=self._config.status_timeout_seconds,
        )

        print(
            f"[STATE ] Equipment status setup: enabling "
            f"{len(controls)} configured controls",
            flush=True,
        )
        for identifier in controls:
            await self._set_device(
                context,
                identifier,
                True,
                phase="devices.status_menu.setup",
                state_timeout_seconds=self._config.status_timeout_seconds,
            )

        setup_snapshot = await self._wait_for_stable_equipment_snapshot(
            context,
            controls,
            phase="devices.status_menu.setup_complete",
            timeout_seconds=self._config.status_timeout_seconds,
        )
        setup_states = {
            identifier: self._device_state_details(
                self._require_device(setup_snapshot, identifier)
            )
            for identifier in controls
        }
        setup_failures = [
            identifier
            for identifier, state in setup_states.items()
            if not state["enabled"]
        ]
        if setup_failures:
            raise ScenarioFailure(
                "Equipment status setup did not remain enabled after "
                "transitions settled: " + ", ".join(setup_failures)
            )

        cursor = context.monitor.cursor
        wait_started = context.timeline.offset_ns()
        print(
            "[ WAIT ] Equipment status: waiting for the PDA home menu "
            f"(timeout {self._config.status_timeout_seconds:g}s)",
            flush=True,
        )
        home = await context.monitor.wait_for(
            "PDA Menu Line 1 = AIR",
            after=cursor,
            timeout_seconds=self._config.status_timeout_seconds,
        )
        print("[STATE ] PDA returned to the home menu", flush=True)
        print(
            "[ WAIT ] Equipment status: waiting for a complete multi-page "
            f"loop (timeout {self._config.status_timeout_seconds:g}s)",
            flush=True,
        )
        started = await self._wait_for_marker(
            context,
            STATUS_MENU_PRESENT_MARKERS,
            after=home.sequence,
            timeout_seconds=self._config.status_timeout_seconds,
        )
        print("[STATE ] EQUIPMENT STATUS loop started", flush=True)
        finished = await self._wait_for_marker(
            context,
            STATUS_MENU_FINISHED_MARKERS,
            after=started.sequence,
            timeout_seconds=self._config.status_timeout_seconds,
        )
        reconciled = await context.monitor.wait_for(
            "Start new equipment cycle bitmask",
            after=finished.sequence,
            timeout_seconds=self._config.state_timeout_seconds,
        )
        print(
            "[STATE ] EQUIPMENT STATUS loop completed and reconciled",
            flush=True,
        )

        events = self._status_loop_events(context, started, reconciled)
        verification = await self._verify_status_loop(
            context,
            controls,
            events,
            setup_states=setup_states,
        )
        self._report["equipment_status"] = verification
        swg_suffix = (
            f"; SWG {verification['swg']['percent']}%"
            if verification["swg"]["percent"] is not None
            else ("; SWG status observed" if verification["swg"]["present"] else "")
        )
        print(
            f"[STATE ] Equipment status verified "
            f"{len(verification['verified_devices'])}/"
            f"{len(verification['expected_devices'])} devices"
            f"{swg_suffix}",
            flush=True,
        )
        self._append_measurement(
            name="pda.status_menu.complete",
            category="state_wait",
            phase="devices.status_menu",
            target="equipment_status_menu",
            requested_value="complete",
            start_offset_ns=wait_started,
            api_ack_offset_ns=None,
            log_completion_offset_ns=reconciled.offset_ns,
            state_observed_offset_ns=context.timeline.offset_ns(),
        )

    def _status_loop_events(
        self,
        context: ScenarioContext,
        started: LineEvent,
        finished: LineEvent,
    ) -> list[LineEvent]:
        history = context.monitor.recent_events()
        first_sequence = started.sequence
        for event in reversed(history):
            if event.sequence >= started.sequence:
                continue
            if "Pass Equiptment msg 'EQUIPMENT STATUS" in event.text:
                first_sequence = event.sequence
                break
        return [
            event
            for event in history
            if first_sequence <= event.sequence <= finished.sequence
        ]

    async def _verify_status_loop(
        self,
        context: ScenarioContext,
        controls: list[str],
        events: list[LineEvent],
        *,
        setup_states: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        assert self._initial_snapshot is not None
        status_messages: list[str] = []
        found_names: set[str] = set()
        found_status: dict[str, str] = {}
        heater_ids: set[str] = set()
        swg_percent: int | None = None
        for event in events:
            message = _STATUS_MESSAGE.search(event.text)
            if message is not None:
                status_messages.append(message.group(1).strip())
            found = _FOUND_STATUS.search(event.text)
            if found is not None:
                normalized = self._normalize_status_name(found.group(1))
                found_names.add(normalized)
                found_status[normalized] = found.group(2).strip()
            lowered = event.text.casefold()
            if "pool hearter is enabled" in lowered:
                heater_ids.add("Pool_Heater")
            if "spa hearter is enabled" in lowered:
                heater_ids.add("Spa_Heater")
            percent = _SWG_PERCENT.search(event.text)
            if percent is not None:
                swg_percent = int(percent.group(1))

        missing: list[str] = []
        verified: list[str] = []
        for identifier in controls:
            device = self._initial_snapshot.devices[identifier]
            name = self._normalize_status_name(str(device.get("name", "")))
            if identifier in heater_ids or name in found_names:
                verified.append(identifier)
            else:
                missing.append(identifier)

        snapshot = await self._wait_for_stable_equipment_snapshot(
            context,
            controls,
            phase="devices.status_menu.verification",
            timeout_seconds=self._config.status_timeout_seconds,
        )
        incorrect_states = [
            identifier
            for identifier in controls
            if not self._device_enabled(self._require_device(snapshot, identifier))
        ]
        swg_devices = [
            device
            for device in snapshot.devices.values()
            if device.get("type") == "setpoint_swg"
        ]
        swg_present = bool(swg_devices)
        swg_observed = (
            any(
                any(
                    marker in message.casefold()
                    for marker in ("aquapure", "salt", "boost")
                )
                for message in status_messages
            )
            or swg_percent is not None
        )
        swg_api_percent: int | None = None
        if swg_devices:
            with contextlib.suppress(KeyError, TypeError, ValueError):
                swg_api_percent = round(float(swg_devices[0]["spvalue"]))

        heater_states: dict[str, dict[str, Any]] = {}
        for identifier in controls:
            device = self._require_device(snapshot, identifier)
            if device.get("type") != "setpoint_thermo":
                continue
            details = self._device_state_details(device)
            candidates = {
                self._normalize_status_name(identifier),
                self._normalize_status_name(str(device.get("name", ""))),
            }
            if identifier == POOL_HEATER:
                candidates.update({"poolheat", "poolheater"})
            elif identifier == "Spa_Heater":
                candidates.update({"spaheat", "spaheater"})
            pda_lines = [
                message
                for message in status_messages
                if any(
                    self._normalize_status_name(message).startswith(candidate)
                    for candidate in candidates
                    if candidate
                )
            ]
            pda_lines.extend(
                status
                for name, status in found_status.items()
                if name in candidates and status not in pda_lines
            )
            normalized_lines = [
                self._normalize_status_name(message) for message in pda_lines
            ]
            pda_enabled: bool | None = None
            pda_active: bool | None = None
            for line in normalized_lines:
                if line.endswith("off"):
                    pda_enabled = False
                    pda_active = False
                elif line.endswith(("ena", "enabled")):
                    pda_enabled = True
                    pda_active = False
                elif line.endswith("on") or line in candidates:
                    pda_enabled = True
                    pda_active = True
            heater_states[identifier] = {
                **details,
                "pda_status_lines": pda_lines,
                "pda_enabled": pda_enabled,
                "pda_active": pda_active,
                "pda_enabled_marker": identifier in heater_ids,
                "found_status": found_status.get(
                    self._normalize_status_name(str(device.get("name", "")))
                ),
            }

        heater_enabled_mismatches = [
            identifier
            for identifier, state in heater_states.items()
            if state["pda_enabled"] is not None
            and state["pda_enabled"] != state["enabled"]
        ]
        heater_active_mismatches = [
            identifier
            for identifier, state in heater_states.items()
            if state["pda_active"] is not None
            and state["pda_active"] != state["active"]
        ]

        failures: list[str] = []
        if missing:
            failures.append("missing status entries for " + ", ".join(missing))
        if incorrect_states:
            failures.append(
                "API marked expected-on devices off after status processing: "
                + ", ".join(incorrect_states)
            )
        if heater_enabled_mismatches:
            failures.append(
                "PDA heater enabled status disagreed with the API: "
                + ", ".join(heater_enabled_mismatches)
            )
        if heater_active_mismatches:
            failures.append(
                "PDA heater active status disagreed with the API: "
                + ", ".join(heater_active_mismatches)
            )
        if swg_present and not swg_observed:
            failures.append("SWG is present but no SWG status was captured")
        if (
            swg_percent is not None
            and swg_api_percent is not None
            and swg_percent != swg_api_percent
        ):
            failures.append(
                f"SWG status reported {swg_percent}% but API reported "
                f"{swg_api_percent}%"
            )

        verification = {
            "setup_states": setup_states or {},
            "expected_devices": controls,
            "verified_devices": verified,
            "missing_devices": missing,
            "incorrect_api_states": incorrect_states,
            "status_messages": status_messages,
            "heater_states": heater_states,
            "heater_enabled_mismatches": heater_enabled_mismatches,
            "heater_active_mismatches": heater_active_mismatches,
            "swg": {
                "present": swg_present,
                "observed": swg_observed,
                "percent": swg_percent,
                "api_percent": swg_api_percent,
            },
        }
        for identifier, state in heater_states.items():
            pda_enabled_state = (
                "enabled"
                if state["pda_enabled"] is True
                else "disabled"
                if state["pda_enabled"] is False
                else "not reported"
            )
            pda_active_state = (
                "active"
                if state["pda_active"] is True
                else "inactive"
                if state["pda_active"] is False
                else "not reported"
            )
            print(
                f"[STATE ] {identifier}: "
                f"{'enabled' if state['enabled'] else 'disabled'}, "
                f"{'actively heating' if state['active'] else 'not actively heating'}, "
                f"PDA {pda_enabled_state}/{pda_active_state}",
                flush=True,
            )
        if failures:
            self._report["equipment_status"] = verification
            raise ScenarioFailure("; ".join(failures))
        return verification

    @staticmethod
    def _normalize_status_name(value: str) -> str:
        return "".join(
            character for character in value.casefold() if character.isalnum()
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
                    raise ScenarioFailure(f"{identifier} is not a switch device")
        else:
            identifiers = [
                identifier
                for identifier, device in self._initial_snapshot.devices.items()
                if device.get("type") == "switch"
            ]

        identifiers = [
            identifier
            for identifier in identifiers
            if not self._skip_unactionable_device(
                identifier,
                phase="devices.consecutive",
            )
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
        if self._skip_unactionable_device(
            POOL_HEATER,
            phase="heater",
        ):
            return
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
        heater: DeviceState,
    ) -> None:
        assert self._initial_snapshot is not None
        try:
            original = heater.setpoint
        except EquipmentStateError:
            original = None
        if original is None:
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
                active_marker=POOL_HEATER_SETPOINT_ACTIVE_MARKERS,
                completion_marker=POOL_HEATER_SETPOINT_FINISHED_MARKERS,
                category="heater_setpoint",
            )

    async def _test_sleep_wake_cycle(self, context: ScenarioContext) -> None:
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
        print(
            "[STATE ] PDA entered sleep; observing one natural wake cycle",
            flush=True,
        )

        wake_active = await self._wait_for_task_active(
            context,
            task_name="PDA init after wake",
            marker=WAKE_INIT_ACTIVE,
            after=event.sequence,
            requested_offset_ns=event.offset_ns,
            timeout_seconds=self._config.sleep_timeout_seconds,
            wait_reason="waiting for the natural PDA wake",
        )
        wake_finished = await self._wait_for_task_completion(
            context,
            task_name="PDA init after wake",
            marker=WAKE_INIT_FINISHED,
            active=wake_active,
            timeout_seconds=self._config.action_timeout_seconds,
        )
        print(
            "[STATE ] Post-wake equipment status refresh complete; "
            "waiting for PDA sleep",
            flush=True,
        )
        try:
            returned_to_sleep = await context.monitor.wait_for(
                PDA_SLEEPING,
                after=wake_finished.sequence,
                timeout_seconds=self._config.sleep_timeout_seconds,
            )
        except TimeoutError as error:
            raise ScenarioFailure(
                "PDA did not return to sleep within "
                f"{self._config.sleep_timeout_seconds:g}s after the "
                "post-wake status refresh"
            ) from error

        asleep_ns = wake_active.offset_ns - event.offset_ns
        status_refresh_ns = wake_finished.offset_ns - wake_active.offset_ns
        return_to_sleep_ns = returned_to_sleep.offset_ns - wake_finished.offset_ns
        awake_ns = returned_to_sleep.offset_ns - wake_active.offset_ns
        cycle_ns = returned_to_sleep.offset_ns - event.offset_ns
        awake_percent = 100 * awake_ns / cycle_ns
        sleep_percent = 100 * asleep_ns / cycle_ns
        self._report["sleep_cycle"] = {
            "sleep_ms": round(asleep_ns / 1_000_000, 3),
            "status_refresh_ms": round(status_refresh_ns / 1_000_000, 3),
            "return_to_sleep_ms": round(return_to_sleep_ns / 1_000_000, 3),
            "awake_ms": round(awake_ns / 1_000_000, 3),
            "cycle_ms": round(cycle_ns / 1_000_000, 3),
            "awake_percent": round(awake_percent, 3),
            "sleep_percent": round(sleep_percent, 3),
        }
        self._append_measurement(
            name="pda.sleep.duration",
            category="sleep_cycle",
            phase="devices.sleeping",
            target="pda_sleep",
            requested_value=True,
            start_offset_ns=event.offset_ns,
            api_ack_offset_ns=None,
            log_completion_offset_ns=wake_active.offset_ns,
            state_observed_offset_ns=None,
        )
        self._append_measurement(
            name="pda.after_wake.status_refresh",
            category="sleep_cycle",
            phase="devices.sleeping",
            target="pda_status",
            requested_value=True,
            start_offset_ns=wake_active.offset_ns,
            api_ack_offset_ns=None,
            task_active_offset_ns=wake_active.offset_ns,
            log_completion_offset_ns=wake_finished.offset_ns,
            state_observed_offset_ns=None,
        )
        self._append_measurement(
            name="pda.after_wake.return_to_sleep",
            category="sleep_cycle",
            phase="devices.sleeping",
            target="pda_sleep",
            requested_value=True,
            start_offset_ns=wake_finished.offset_ns,
            api_ack_offset_ns=None,
            log_completion_offset_ns=returned_to_sleep.offset_ns,
            state_observed_offset_ns=None,
        )
        self._append_measurement(
            name="pda.wake.duration",
            category="sleep_cycle",
            phase="devices.sleeping",
            target="pda_awake",
            requested_value=True,
            start_offset_ns=wake_active.offset_ns,
            api_ack_offset_ns=None,
            log_completion_offset_ns=returned_to_sleep.offset_ns,
            state_observed_offset_ns=None,
        )
        self._append_measurement(
            name="pda.sleep_wake.cycle",
            category="sleep_cycle",
            phase="devices.sleeping",
            target="pda_sleep_wake_cycle",
            requested_value=True,
            start_offset_ns=event.offset_ns,
            api_ack_offset_ns=None,
            log_completion_offset_ns=returned_to_sleep.offset_ns,
            state_observed_offset_ns=None,
        )
        print(
            "[STATE ] PDA returned to sleep: "
            f"asleep {asleep_ns / 1_000_000_000:.3f}s, "
            f"status refresh {status_refresh_ns / 1_000_000_000:.3f}s, "
            f"post-status awake {return_to_sleep_ns / 1_000_000_000:.3f}s, "
            f"cycle {cycle_ns / 1_000_000_000:.3f}s, "
            f"awake {awake_percent:.1f}% / sleep {sleep_percent:.1f}%",
            flush=True,
        )

    def _sleep_test_device(self, *, phase: str) -> str | None:
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
                    raise ScenarioFailure(f"{identifier} is not a switch device")
        else:
            identifiers = [
                identifier
                for identifier, device in self._initial_snapshot.devices.items()
                if device.get("type") == "switch"
            ]

        identifiers = [
            identifier
            for identifier in identifiers
            if not self._skip_unactionable_device(identifier, phase=phase)
        ]
        if not identifiers:
            self._skip(
                phase,
                "No actionable switch devices were discovered",
            )
            return None

        # Exercise the deepest configured equipment entry without depending
        # on the API's object ordering. Even the smallest pool-only panel has
        # auxiliary circuits; Filter Pump remains the universal fallback for
        # configurations that deliberately disable every auxiliary.
        identifier = max(
            identifiers,
            key=self._sleep_device_priority,
        )
        if PdaCaseId.CONSECUTIVE_DEVICES not in self._case_ids:
            self._report["device_selection"]["resolved"] = [identifier]
        return identifier

    def _sleep_device_priority(self, identifier: str) -> tuple[int, int, str]:
        auxiliary = _AUX_IDENTIFIER.fullmatch(identifier)
        if auxiliary is not None:
            return (2, int(auxiliary.group(1)), identifier)
        if identifier == FILTER_PUMP:
            return (0, 0, identifier)
        button_number = self._button_number_by_identifier.get(identifier, 0)
        return (1, button_number, identifier)

    async def _wait_for_sleep(
        self,
        context: ScenarioContext,
        *,
        phase: str,
        measurement_name: str,
    ) -> LineEvent:
        cursor = context.monitor.cursor
        started = context.timeline.offset_ns()
        event = await context.monitor.wait_for(
            PDA_SLEEPING,
            after=cursor,
            timeout_seconds=self._config.sleep_timeout_seconds,
        )
        self._append_measurement(
            name=measurement_name,
            category="state_wait",
            phase=phase,
            target="pda_sleep",
            requested_value=True,
            start_offset_ns=started,
            api_ack_offset_ns=None,
            log_completion_offset_ns=event.offset_ns,
            state_observed_offset_ns=None,
        )
        return event

    async def _test_device_during_status_retry(
        self,
        context: ScenarioContext,
    ) -> None:
        phase = "devices.sleep.status_retry"
        identifier = self._sleep_test_device(phase=phase)
        if identifier is None:
            return
        sleep_event = await self._wait_for_sleep(
            context,
            phase=phase,
            measurement_name="pda.sleep.status_retry.command_ready",
        )
        delay = self._config.status_retry_command_delay_seconds
        print(
            f"[ WAIT ] PDA STATUS retry phase: delaying {delay:g}s after sleep begins",
            flush=True,
        )
        await asyncio.sleep(delay)
        events = [
            event
            for event in context.monitor.recent_events()
            if event.sequence > sleep_event.sequence
        ]
        if any(PDA_ADDRESS_PROBE in event.text for event in events):
            raise ScenarioFailure(
                "PDA address probing began before the STATUS-retry command was sent"
            )
        retry_count = sum(PDA_ADDRESS_STATUS in event.text for event in events)
        if retry_count == 0:
            raise ScenarioFailure(
                "No repeated PDA STATUS packet was observed before the "
                "STATUS-retry command"
            )
        print(
            f"[STATE ] Observed {retry_count} repeated PDA STATUS packet(s); "
            f"toggling {identifier}",
            flush=True,
        )
        await self._toggle_round_trip(
            context,
            identifier,
            phase=phase,
        )

    async def _test_device_after_probe(
        self,
        context: ScenarioContext,
    ) -> None:
        phase = "devices.sleep.probing"
        identifier = self._sleep_test_device(phase=phase)
        if identifier is None:
            return
        sleep_event = await self._wait_for_sleep(
            context,
            phase=phase,
            measurement_name="pda.sleep.probe.command_ready",
        )
        print(
            "[ WAIT ] PDA probe phase: waiting for a probe to address 0x60 "
            f"(timeout {self._config.sleep_timeout_seconds:g}s)",
            flush=True,
        )
        try:
            probe = await context.monitor.wait_for(
                PDA_ADDRESS_PROBE,
                after=sleep_event.sequence,
                timeout_seconds=self._config.sleep_timeout_seconds,
            )
        except TimeoutError as error:
            raise ScenarioFailure(
                "Panel did not begin probing PDA address 0x60 after sleep"
            ) from error
        probe_delay = (probe.offset_ns - sleep_event.offset_ns) / 1_000_000_000
        remaining_delay = max(
            0.0,
            self._config.probe_command_min_delay_seconds - probe_delay,
        )
        if remaining_delay:
            print(
                f"[ WAIT ] Probe observed early; delaying "
                f"{remaining_delay:.3f}s so the command is at least "
                f"{self._config.probe_command_min_delay_seconds:g}s after "
                "sleep began",
                flush=True,
            )
            await asyncio.sleep(remaining_delay)
        print(
            f"[STATE ] PDA address probe observed {probe_delay:.3f}s after "
            f"sleep began; toggling {identifier}",
            flush=True,
        )
        await self._toggle_round_trip(
            context,
            identifier,
            phase=phase,
        )

    async def _toggle_round_trip_unless_disabled(
        self,
        context: ScenarioContext,
        identifier: str,
        *,
        phase: str,
    ) -> None:
        if self._skip_unactionable_device(identifier, phase=phase):
            return
        await self._toggle_round_trip(context, identifier, phase=phase)

    def _record_device_constraints(self) -> None:
        assert self._initial_snapshot is not None
        self._button_number_by_identifier = {
            identifier: number
            for identifier in self._initial_snapshot.devices
            if (number := self._button_number(identifier)) is not None
        }
        disabled = set(self._config.disabled_button_numbers)
        excluded: list[dict[str, Any]] = []
        for identifier, device in self._initial_snapshot.devices.items():
            if device.get("type") not in {"switch", "setpoint_thermo"}:
                continue
            reasons: list[str] = []
            button_number = self._button_number_by_identifier.get(identifier)
            if button_number is not None and button_number in disabled:
                reasons.append(
                    f"button_{button_number:02d}_label is configured as NONE"
                )
            api_name = str(device.get("name", "")).strip()
            if api_name.casefold() == "none":
                reasons.append("API device name is NONE")
            auxiliary = _AUX_IDENTIFIER.fullmatch(identifier)
            if auxiliary is not None and self._reported_panel_size is not None:
                auxiliary_number = int(auxiliary.group(1))
                if auxiliary_number >= self._reported_panel_size:
                    reasons.append(
                        f"Aux_{auxiliary_number} is beyond reported "
                        f"panel size {self._reported_panel_size}"
                    )
            if not reasons:
                continue
            self._excluded_device_ids.add(identifier)
            excluded.append(
                {
                    "button": button_number,
                    "identifier": identifier,
                    "name": api_name,
                    "reasons": reasons,
                }
            )
        self._report["device_selection"]["excluded"] = excluded

    def _button_number(self, identifier: str) -> int | None:
        if identifier == FILTER_PUMP:
            return 1
        if self._is_spa_mode(identifier):
            return 2 if self._reported_panel_combo else None
        auxiliary = _AUX_IDENTIFIER.fullmatch(identifier)
        if auxiliary is None:
            return None
        offset = 2 if self._reported_panel_combo else 1
        return int(auxiliary.group(1)) + offset

    def _skip_unactionable_device(
        self,
        identifier: str,
        *,
        phase: str,
    ) -> bool:
        if identifier not in self._excluded_device_ids:
            return False
        excluded = next(
            item
            for item in self._report["device_selection"]["excluded"]
            if item["identifier"] == identifier
        )
        self._skip(
            f"{phase}.{identifier}",
            "; ".join(excluded["reasons"]),
        )
        return True

    async def _set_device(
        self,
        context: ScenarioContext,
        identifier: str,
        enabled: bool,
        *,
        phase: str,
        state_timeout_seconds: float | None = None,
    ) -> None:
        self._remember_device(identifier)
        if not phase.startswith("restoration."):
            self._restoration.forget_requested_state(identifier)
        current_snapshot = await self._api_client.devices()
        current_device = self._require_device(current_snapshot, identifier)
        if self._device_transition_pending(current_device):
            current_snapshot = await self._wait_for_stable_equipment_snapshot(
                context,
                [identifier],
                phase=f"{phase}.{identifier}.precondition",
                # A water-mode change can leave the filter in a panel-managed
                # cooldown for several minutes.  Do not send a second toggle
                # while that transition is pending.
                timeout_seconds=self._config.restoration_timeout_seconds,
                initial_snapshot=current_snapshot,
            )
            current_device = self._require_device(
                current_snapshot,
                identifier,
            )
        requested_state = self._requested_device_state_label(
            current_device,
            enabled,
        )
        if self._device_enabled(current_device) == enabled:
            self._skip(
                f"{phase}.{identifier}.{requested_state}",
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
        state_timeout = (
            state_timeout_seconds
            if state_timeout_seconds is not None
            else self._config.state_timeout_seconds
        )
        print(
            f"[ WAIT ] {identifier}: waiting for API state "
            f"{requested_state} (timeout "
            f"{state_timeout:g}s)",
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
                    timeout_seconds=state_timeout,
                ),
            )
        except Exception:
            self._append_measurement(
                name=f"{phase}.{identifier}.{requested_state}",
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
        state_seconds = (observed - completed.offset_ns) / 1_000_000_000
        print(
            f"[STATE ] {identifier} became {requested_state} "
            f"{state_seconds:.3f}s after programmer completion",
            flush=True,
        )

        self._append_measurement(
            name=f"{phase}.{identifier}.{requested_state}",
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
        active_marker: str | tuple[str, ...],
        completion_marker: str | tuple[str, ...],
        category: str,
    ) -> None:
        self._restoration.touch_setpoint(identifier)
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
        task_name = "Set PDA Pool Heater" if identifier == POOL_HEATER else identifier
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
        state_seconds = (observed - completed.offset_ns) / 1_000_000_000
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
            if (
                not self._device_transition_pending(device)
                and self._device_enabled(device) == enabled
            ):
                return context.timeline.offset_ns()
            await asyncio.sleep(0.25)
        requested_state = self._requested_device_state_label(device, enabled)
        raise ScenarioFailure(
            f"{identifier} did not become {requested_state} within {timeout:g}s"
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
            f"{identifier} setpoint did not become {expected} within {timeout:g}s"
        )

    async def _current_device_enabled(self, identifier: str) -> bool:
        snapshot = await self._api_client.devices()
        return self._device_enabled(self._require_device(snapshot, identifier))

    def _initial_device_enabled(self, identifier: str) -> bool:
        return self._restoration.initial_device_enabled(identifier)

    def _remember_device(self, identifier: str) -> None:
        self._restoration.touch_device(identifier)

    async def _restore_original_state(
        self,
        context: ScenarioContext,
    ) -> list[str]:
        restoration = self._report["restoration"]
        if self._initial_snapshot is None:
            return []
        if self._restoration.initial_snapshot is not self._initial_snapshot:
            self._restoration.capture_initial(self._initial_snapshot)
        restoration["attempted"] = True
        async def restore_setpoint(identifier: str, original: int) -> None:
            if identifier != POOL_HEATER:
                raise ScenarioFailure(
                    f"No restoration programmer markers for {identifier}"
                )
            await self._set_setpoint(
                context,
                identifier,
                original,
                phase="restoration.setpoint",
                active_marker=POOL_HEATER_SETPOINT_ACTIVE_MARKERS,
                completion_marker=POOL_HEATER_SETPOINT_FINISHED_MARKERS,
                category="restoration",
            )

        result = await self._restoration.restore(
            read_snapshot=self._api_client.devices,
            restore_setpoint=restore_setpoint,
            restore_device=lambda identifier, expected: (
                self._restore_device_state(context, identifier, expected)
            ),
        )
        restoration["actions"].extend(
            {
                "target": action.target,
                "property": action.property,
                "value": action.value,
                "status": action.status,
            }
            for action in result.actions
        )
        restoration["errors"].extend(result.errors)
        restoration["status"] = "passed" if result.passed else "failed"
        return list(result.errors)

    @staticmethod
    def _is_spa_mode(identifier: str) -> bool:
        return identifier in {"Spa", "Spa_Mode"}

    async def _restore_device_state(
        self,
        context: ScenarioContext,
        identifier: str,
        expected: bool,
    ) -> None:
        snapshot = await self._api_client.devices()
        device = self._require_device(snapshot, identifier)
        requested = self._restoration.requested_state(identifier)
        if self._device_transition_pending(device):
            requested_state = self._requested_device_state_label(
                device,
                expected,
            )
            print(
                f"[ WAIT ] {identifier}: equipment transition is already "
                "pending; not sending another toggle "
                f"(timeout {self._config.restoration_timeout_seconds:g}s)",
                flush=True,
            )
            snapshot = await self._wait_for_stable_equipment_snapshot(
                context,
                [identifier],
                phase=f"restoration.{identifier}.pending_transition",
                timeout_seconds=self._config.restoration_timeout_seconds,
                initial_snapshot=snapshot,
            )
            device = self._require_device(snapshot, identifier)
            if self._device_enabled(device) == expected:
                print(
                    f"[STATE ] {identifier} completed the pending "
                    f"{requested_state} transition",
                    flush=True,
                )
                return

        if self._device_enabled(device) == expected:
            return

        if requested == expected:
            reason = "a restoration request was already sent"
            print(
                f"[ WAIT ] {identifier}: {reason}; not sending another "
                f"toggle (timeout "
                f"{self._config.restoration_timeout_seconds:g}s)",
                flush=True,
            )
            await self._wait_for_device_state(
                context,
                identifier,
                expected,
                timeout_seconds=self._config.restoration_timeout_seconds,
            )
            print(
                f"[STATE ] {identifier} completed the pending "
                f"{self._requested_device_state_label(device, expected)} "
                "transition",
                flush=True,
            )
            return

        self._restoration.mark_requested_state(identifier, expected)
        await self._set_device(
            context,
            identifier,
            expected,
            phase="restoration.device",
            state_timeout_seconds=self._config.restoration_timeout_seconds,
        )

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
                    (log_completion_offset_ns - task_active_offset_ns) / 1_000_000,
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
                    (state_observed_offset_ns - log_completion_offset_ns) / 1_000_000,
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
        try:
            return await self._programmer.wait_for_state_or_error(
                context.monitor,
                task_name=task_name,
                after=after,
                state_wait=state_wait,
                timeout_seconds=self._config.state_timeout_seconds,
            )
        except PdaProgrammerFailure as error:
            raise ScenarioFailure(str(error)) from error

    def _skip(self, name: str, reason: str) -> None:
        self._report["skipped"].append({"name": name, "reason": reason})
        print(f"[ SKIP ] {name} — {reason}", flush=True)

    @classmethod
    def _format_exception(cls, error: BaseException) -> str:
        if isinstance(error, BaseExceptionGroup):
            details = [cls._format_exception(nested) for nested in error.exceptions]
            if len(details) == 1:
                return details[0]
            return "; ".join(details)
        return f"{type(error).__name__}: {error}"

    def _write_report(self, context: ScenarioContext) -> None:
        (context.artifact_dir / "scenario.json").write_text(
            json.dumps(self._report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _require_device(
        snapshot: EquipmentSnapshot,
        identifier: str,
    ) -> DeviceState:
        try:
            return snapshot.devices[identifier]
        except KeyError as error:
            raise ScenarioFailure(
                f"Required device {identifier} is absent from /api/devices"
            ) from error

    @staticmethod
    def _device_state(
        device: DeviceState | Mapping[str, Any],
    ) -> DeviceState:
        return device if isinstance(device, DeviceState) else DeviceState(device)

    @classmethod
    def _device_int_status(
        cls,
        device: DeviceState | Mapping[str, Any],
    ) -> int:
        try:
            return cls._device_state(device).int_status
        except EquipmentStateError as error:
            raise ScenarioFailure(str(error)) from error

    @classmethod
    def _device_enabled(
        cls,
        device: DeviceState | Mapping[str, Any],
    ) -> bool:
        return cls._device_state(device).enabled

    @classmethod
    def _device_active(
        cls,
        device: DeviceState | Mapping[str, Any],
    ) -> bool:
        return cls._device_state(device).active

    @classmethod
    def _device_transition_pending(
        cls,
        device: DeviceState | Mapping[str, Any],
    ) -> bool:
        return cls._device_state(device).transitioning

    @classmethod
    def _device_state_details(
        cls,
        device: DeviceState | Mapping[str, Any],
    ) -> dict[str, Any]:
        state = cls._device_state(device)
        return {
            "int_status": state.int_status,
            "state": state.state,
            "status": state.status,
            "enabled": state.enabled,
            "active": state.active,
            "transitioning": state.transitioning,
        }

    @staticmethod
    def _requested_device_state_label(
        device: DeviceState | Mapping[str, Any],
        enabled: bool,
    ) -> str:
        return PdaLivePanelScenario._device_state(device).requested_state_label(
            enabled
        )
