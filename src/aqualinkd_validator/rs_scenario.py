from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from .adapters import ApiError, AqualinkHttpApi
from .config import normalize_api_base_url
from .domain import EquipmentSnapshot, EquipmentStateError, device_state_details
from .engine import ScenarioRecorder
from .interfaces import AqualinkApi, ScenarioContext, ScenarioOutcome
from .protocols.pda.programmer import PdaProgrammerObserver
from .protocols.time_sync import (
    PanelTimeSyncConfig,
    PanelTimeSyncValidator,
    ProcessTimeController,
)

RS_FAST_CASES = ("rs-initialization", "rs-filter-pump", "rs-aux-1")
RS_FULL_CASES = ("rs-initialization", "rs-filter-pump", "rs-auxiliaries")

_AUX_IDENTIFIER = re.compile(r"^Aux_(B)?(\d+)$", re.IGNORECASE)

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


class RsScenarioFailure(RuntimeError):
    """Raised when RS panel validation cannot complete safely."""


class RsScenarioRuntime:
    """Run protocol-neutral HTTP checks against an RS/AllButton panel."""

    def __init__(
        self,
        *,
        suite_name: str,
        case_ids: tuple[str, ...] = RS_FAST_CASES,
        api_base_url_override: str | None = None,
        api_factory: Callable[[str], AqualinkApi] = AqualinkHttpApi,
        init_timeout_seconds: float = 180.0,
        state_timeout_seconds: float = 10.0,
        device_id: str = "0x0b",
        time_controller: ProcessTimeController | None = None,
        panel_timezone: str = "UTC",
        panel_time_tolerance_seconds: float = 120.0,
        time_observation_seconds: float = 300.0,
        time_poll_seconds: float = 0.1,
        activation_timeout_seconds: float = 130.0,
        action_timeout_seconds: float = 90.0,
    ) -> None:
        self._case_ids = case_ids
        self._api_base_url_override = api_base_url_override
        self._api_factory = api_factory
        self._init_timeout_seconds = init_timeout_seconds
        self._state_timeout_seconds = state_timeout_seconds
        self._device_id = device_id.casefold()
        self._time_controller = time_controller
        self._time_config = PanelTimeSyncConfig(
            protocol="rs",
            task_name="Set time",
            active_marker="is active (Set time)",
            finished_marker="(Set time) finished",
            artifact_name="rs-time.json",
            timezone=panel_timezone,
            observation_seconds=time_observation_seconds,
            poll_seconds=time_poll_seconds,
            convergence_tolerance_seconds=panel_time_tolerance_seconds,
            activation_timeout_seconds=activation_timeout_seconds,
            action_timeout_seconds=action_timeout_seconds,
        )
        self._api: AqualinkApi | None = None
        self._initial_snapshot: EquipmentSnapshot | None = None
        self._touched: set[str] = set()
        self._report: dict[str, Any] = {
            "schema_version": 1,
            "suite": suite_name,
            "protocol": "rs",
            "status": "running",
            "reason": None,
            "safe_to_continue": False,
            "api_base_url": api_base_url_override,
            "api_endpoint_source": (
                "explicit_override"
                if api_base_url_override is not None
                else "aqualinkd_startup_log"
            ),
            "aqualinkd": None,
            "cases": [],
            "measurements": [],
            "time_sync": None,
            "skipped": [],
            "restoration": {
                "attempted": False,
                "status": "not-needed",
                "actions": [],
                "errors": [],
            },
        }
        self._recorder = ScenarioRecorder(self._report)

    async def run(self, context: ScenarioContext) -> ScenarioOutcome:
        status = "passed"
        reason = "scenario_completed"
        try:
            for case_id in self._case_ids:
                await self._run_case(case_id, context)
        except Exception as error:
            status = "failed"
            reason = "scenario_failed"
            self._report["error"] = self._recorder.format_exception(error)
        finally:
            restoration_errors = await self._restore(context)
            if restoration_errors:
                status = "failed"
                reason = "restoration_failed"
            if self._time_controller is not None:
                try:
                    await self._restore_real_time(context)
                except Exception as error:
                    status = "failed"
                    reason = "time_restoration_failed"
                    self._report["restoration"]["errors"].append(str(error))

        self._report["status"] = status
        self._report["reason"] = reason
        self._report["safe_to_continue"] = not self._report["restoration"]["errors"]
        self._recorder.write(context.artifacts)
        return ScenarioOutcome(status=status, reason=reason)

    async def _run_case(
        self,
        case_id: str,
        context: ScenarioContext,
    ) -> None:
        started = self._recorder.progress_started(case_id)
        case: dict[str, Any] = {"id": case_id, "status": "running"}
        self._report["cases"].append(case)
        try:
            if case_id == "rs-initialization":
                await self._initialize(context)
            elif case_id == "rs-time-sync":
                await self._test_time_sync(context)
            elif case_id == "rs-filter-pump":
                await self._exercise_device("Filter_Pump", context)
            elif case_id == "rs-aux-1":
                await self._exercise_device("Aux_1", context)
            elif case_id == "rs-auxiliaries":
                await self._exercise_auxiliaries(context)
            else:
                raise RsScenarioFailure(f"unsupported RS case: {case_id}")
        except Exception as error:
            case["status"] = "failed"
            case["error"] = self._recorder.format_exception(error)
            self._recorder.progress_finished(
                case_id,
                started,
                passed=False,
                detail=case["error"],
            )
            raise
        case["status"] = "passed"
        self._recorder.progress_finished(case_id, started, passed=True)

    def _time_validator(self) -> PanelTimeSyncValidator:
        if self._time_controller is None:
            raise RsScenarioFailure("RS time suite has no process clock controller")
        return PanelTimeSyncValidator(
            api=self._require_api(),
            controller=self._time_controller,
            programmer=PdaProgrammerObserver(),
            config=self._time_config,
        )

    async def _test_time_sync(self, context: ScenarioContext) -> None:
        self._report["time_sync"] = await self._time_validator().validate(context)

    async def _restore_real_time(self, context: ScenarioContext) -> None:
        assert self._time_controller is not None
        if not self._time_controller.panel_requires_restore:
            return
        await self._time_validator().restore(context)

    async def _initialize(self, context: ScenarioContext) -> None:
        identity_task = asyncio.create_task(self._capture_identity(context))
        if self._api_base_url_override is None:
            endpoint = await self._discover_api_base_url(context)
            endpoint_source = "aqualinkd_startup_log"
        else:
            endpoint = normalize_api_base_url(self._api_base_url_override)
            endpoint_source = "explicit_override"
        self._api = self._api_factory(endpoint)
        self._report["api_base_url"] = endpoint
        self._report["api_endpoint_source"] = endpoint_source
        identity = await identity_task
        if not identity["configured_panel_type"].casefold().startswith("rs-"):
            raise RsScenarioFailure(
                "AqualinkD did not start as an RS panel: "
                f"{identity['configured_panel_type']}"
            )
        self._report["aqualinkd"] = identity
        await context.monitor.wait_for(
            "Starting communication with Control Panel",
            timeout_seconds=self._init_timeout_seconds,
        )
        await context.monitor.wait_for_match(
            lambda event: (
                "Get Freeze protect" in event.text
                and "finished" in event.text.casefold()
            ),
            description="RS startup programming completion",
            timeout_seconds=self._init_timeout_seconds,
        )
        self._initial_snapshot = await self._wait_for_equipment()
        missing = sorted(
            {"Filter_Pump", "Aux_1"} - self._initial_snapshot.devices.keys()
        )
        if missing:
            raise RsScenarioFailure(
                "Required RS devices are absent from /api/devices: "
                + ", ".join(missing)
            )
        self._report["initial_equipment"] = {
            identifier: device_state_details(device)
            for identifier, device in self._initial_snapshot.devices.items()
            if identifier == "Filter_Pump" or _AUX_IDENTIFIER.fullmatch(identifier)
        }

    async def _exercise_auxiliaries(self, context: ScenarioContext) -> None:
        if self._initial_snapshot is None:
            raise RsScenarioFailure("RS initialization must run before equipment tests")
        identifiers = sorted(
            (
                identifier
                for identifier in self._initial_snapshot.devices
                if _AUX_IDENTIFIER.fullmatch(identifier)
            ),
            key=self._aux_sort_key,
        )
        if not identifiers:
            raise RsScenarioFailure("No RS auxiliary circuits were discovered")
        self._report["full_suite_devices"] = identifiers
        for identifier in identifiers:
            await self._exercise_device(identifier, context)

    @staticmethod
    def _aux_sort_key(identifier: str) -> tuple[int, int]:
        match = _AUX_IDENTIFIER.fullmatch(identifier)
        if match is None:
            return (2, 0)
        return (1 if match.group(1) else 0, int(match.group(2)))

    async def _exercise_device(
        self,
        identifier: str,
        context: ScenarioContext,
    ) -> None:
        if self._initial_snapshot is None:
            raise RsScenarioFailure("RS initialization must run before equipment tests")
        api = self._require_api()
        try:
            original = self._initial_snapshot.devices[identifier].enabled
        except (KeyError, EquipmentStateError) as error:
            raise RsScenarioFailure(
                f"Cannot read initial {identifier} state"
            ) from error
        requested = not original
        self._touched.add(identifier)
        started = context.timeline.offset_ns()
        await context.timeline.write(
            "scenario_action_started",
            phase=f"rs.{identifier}",
            action="set_device",
            target=identifier,
            value=requested,
        )
        cursor = context.monitor.cursor
        await api.set_device(identifier, requested)
        acknowledged = context.timeline.offset_ns()
        await context.timeline.write(
            "scenario_http_acknowledged",
            phase=f"rs.{identifier}",
            action="set_device",
            target=identifier,
            value=requested,
            requested_offset_ns=started,
        )
        await self._wait_for_panel_confirmation(context, after=cursor)
        await self._wait_for_state(identifier, requested)
        observed = context.timeline.offset_ns()
        await context.timeline.write(
            "scenario_action_finished",
            phase=f"rs.{identifier}",
            action="set_device",
            target=identifier,
            value=requested,
            status="passed",
        )
        self._recorder.append_measurement(
            name=f"rs.{identifier}.toggle",
            category="device",
            phase="rs.equipment",
            target=identifier,
            requested_value=requested,
            start_offset_ns=started,
            api_ack_offset_ns=acknowledged,
            log_completion_offset_ns=None,
            state_observed_offset_ns=observed,
        )
        print(
            f"[STATE ] {identifier} became {'on' if requested else 'off'}",
            flush=True,
        )
        await self._restore_device(identifier, original, context)
        restoration = self._report["restoration"]
        restoration["attempted"] = True
        restoration["status"] = "passed"
        restoration["actions"].append(
            {"target": identifier, "property": "state", "value": original}
        )
        self._touched.discard(identifier)

    async def _restore(self, context: ScenarioContext) -> list[str]:
        restoration = self._report["restoration"]
        if self._initial_snapshot is None or not self._touched:
            return []
        restoration["attempted"] = True
        errors: list[str] = []
        for identifier in sorted(self._touched):
            try:
                original = self._initial_snapshot.devices[identifier].enabled
                await self._restore_device(identifier, original, context)
                restoration["actions"].append(
                    {"target": identifier, "property": "state", "value": original}
                )
            except Exception as error:
                errors.append(f"{identifier}: {error}")
        restoration["errors"] = errors
        restoration["status"] = "failed" if errors else "passed"
        if not errors:
            self._touched.clear()
        return errors

    async def _restore_device(
        self,
        identifier: str,
        enabled: bool,
        context: ScenarioContext,
    ) -> None:
        api = self._require_api()
        current = (await api.devices()).devices[identifier].enabled
        if current != enabled:
            cursor = context.monitor.cursor
            await api.set_device(identifier, enabled)
            await self._wait_for_panel_confirmation(context, after=cursor)
            await self._wait_for_state(identifier, enabled)

    async def _wait_for_panel_confirmation(
        self,
        context: ScenarioContext,
        *,
        after: int,
    ) -> None:
        command = await context.monitor.wait_for(
            "Ack w/ Command",
            after=after,
            timeout_seconds=self._state_timeout_seconds,
        )
        await context.monitor.wait_for_match(
            lambda event: (
                f"packet to {self._device_id}" in event.text.casefold()
                and "status" in event.text.casefold()
            ),
            description="RS status following AllButton command",
            after=command.sequence,
            timeout_seconds=self._state_timeout_seconds,
        )

    async def _wait_for_equipment(self) -> EquipmentSnapshot:
        deadline = asyncio.get_running_loop().time() + self._init_timeout_seconds
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                snapshot = await self._require_api().devices()
                if "Filter_Pump" in snapshot.devices and "Aux_1" in snapshot.devices:
                    return snapshot
            except (ApiError, OSError) as error:
                last_error = error
            await asyncio.sleep(0.25)
        suffix = f": {last_error}" if last_error is not None else ""
        raise RsScenarioFailure(
            f"RS equipment did not initialize within {self._init_timeout_seconds:g}s"
            + suffix
        )

    async def _wait_for_state(self, identifier: str, enabled: bool) -> int:
        deadline = asyncio.get_running_loop().time() + self._state_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            device = (await self._require_api().devices()).devices.get(identifier)
            if (
                device is not None
                and not device.transitioning
                and device.enabled == enabled
            ):
                return int(asyncio.get_running_loop().time() * 1_000_000_000)
            await asyncio.sleep(0.25)
        raise RsScenarioFailure(
            f"{identifier} did not become {'on' if enabled else 'off'} within "
            f"{self._state_timeout_seconds:g}s"
        )

    async def _discover_api_base_url(self, context: ScenarioContext) -> str:
        event = await context.monitor.wait_for(
            "Starting web server on",
            timeout_seconds=self._init_timeout_seconds,
        )
        port_match = _WEB_SERVER_PORT.search(event.text)
        if port_match is not None:
            return normalize_api_base_url(f"http://127.0.0.1:{port_match.group(1)}")
        match = _WEB_SERVER_URL.search(event.text)
        if match is None:
            raise RsScenarioFailure("AqualinkD startup log did not contain an API URL")
        return normalize_api_base_url(match.group(1))

    async def _capture_identity(self, context: ScenarioContext) -> dict[str, str]:
        async with asyncio.TaskGroup() as tasks:
            version_task = tasks.create_task(
                context.monitor.wait_for(
                    "Aqualink Daemon v",
                    timeout_seconds=self._init_timeout_seconds,
                )
            )
            panel_task = tasks.create_task(
                context.monitor.wait_for_any(
                    ("Panel set to ", "panel type"),
                    timeout_seconds=self._init_timeout_seconds,
                )
            )
        version_match = _AQUALINKD_VERSION.search(version_task.result().text)
        panel_match = _CONFIGURED_PANEL.search(panel_task.result().text)
        if version_match is None or panel_match is None:
            raise RsScenarioFailure("AqualinkD startup identity was not parseable")
        return {
            "version": version_match.group(1).strip(),
            "configured_panel_type": panel_match.group(1).strip(),
            "source": "aqualinkd_startup_log",
        }

    def _require_api(self) -> AqualinkApi:
        if self._api is None:
            raise RsScenarioFailure("AqualinkD HTTP API is not initialized")
        return self._api
