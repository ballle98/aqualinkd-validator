from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from .aquapda_client import (
    AquaPdaClient,
    AquaPdaWebSocketClient,
)
from .domain import DeviceState, EquipmentSnapshot, EquipmentStateError
from .engine import (
    EquipmentActionFailure,
    EquipmentActions,
    EquipmentActionTimeouts,
    EquipmentStabilityConfig,
    EquipmentStabilityFailure,
    EquipmentStabilityService,
    ProgrammerMarkers,
    RestorationSession,
)
from .http_api import ApiError, AqualinkHttpApi
from .interfaces import AqualinkApi
from .protocols.pda import (
    PdaDeviceSelectionConfig,
    PdaDeviceSelectionFailure,
    PdaDeviceSelector,
    PdaEquipmentStatusFailure,
    PdaEquipmentStatusService,
    PdaPanelIdentityConfig,
    PdaPanelIdentityFailure,
    PdaPanelIdentityResult,
    PdaPanelIdentityValidator,
    PdaProgrammerFailure,
    PdaProgrammerObserver,
    PdaRestorationConfig,
    PdaRestorationService,
    PdaSleepWakeConfig,
    PdaSleepWakeFailure,
    PdaSleepWakeService,
)
from .protocols.pda import equipment_status as pda_equipment_status
from .protocols.pda import session as pda_session
from .protocols.pda import sleep as pda_sleep
from .protocols.pda.aquapda import (
    AquaPdaMenuWalkConfig,
    AquaPdaMenuWalker,
    AquaPdaTransportConfig,
    AquaPdaTransportValidator,
    AquaPdaValidationFailure,
)
from .protocols.pda.equipment_setup import (
    PdaEquipmentSetupConfig,
    PdaEquipmentSetupFailure,
    PdaEquipmentStatusSetup,
)
from .protocols.pda.keywords import PdaKeywordMarkers, PdaTestcaseKeywords
from .protocols.pda.spa import PdaSpaExercise, SpaExerciseConfig
from .run_targets import RUNTIME_CASES, RuntimeCaseDefinition, RuntimeCaseId
from .supervisor import LineEvent, ScenarioContext, ScenarioOutcome
from .testcases import (
    ExerciseDiscoveredDevicesStep,
    ExerciseHeaterStep,
    ExerciseProbeTransitionStep,
    ExerciseStatusRetryStep,
    TestcaseDefinition,
    TestcaseExecutor,
)

FILTER_PUMP = "Filter_Pump"
POOL_HEATER = "Pool_Heater"
SPA_HEATER = "Spa_Heater"
INIT_ACTIVE = pda_session.INIT_ACTIVE
INIT_FINISHED = pda_session.INIT_FINISHED

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
SPA_HEATER_SETPOINT_FINISHED = "(Set PDA Spa Heater) finished"
SPA_HEATER_SETPOINT_ACTIVE = "is active (Set PDA Spa Heater)"
LEGACY_SPA_HEATER_SETPOINT_FINISHED = "(Set Spa heater setpoint) finished"
LEGACY_SPA_HEATER_SETPOINT_ACTIVE = "is active (Set Spa heater setpoint)"
SPA_HEATER_SETPOINT_FINISHED_MARKERS = (
    SPA_HEATER_SETPOINT_FINISHED,
    LEGACY_SPA_HEATER_SETPOINT_FINISHED,
)
SPA_HEATER_SETPOINT_ACTIVE_MARKERS = (
    SPA_HEATER_SETPOINT_ACTIVE,
    LEGACY_SPA_HEATER_SETPOINT_ACTIVE,
)
STATUS_MENU_PRESENT = pda_equipment_status.STATUS_MENU_PRESENT
LEGACY_STATUS_MENU_PRESENT = pda_equipment_status.LEGACY_STATUS_MENU_PRESENT
PDA_SLEEPING = pda_sleep.PDA_SLEEPING
PDA_ADDRESS_STATUS = pda_sleep.PDA_ADDRESS_STATUS
PDA_ADDRESS_PROBE = pda_sleep.PDA_ADDRESS_PROBE
WAKE_INIT_ACTIVE = pda_sleep.WAKE_INIT_ACTIVE
WAKE_INIT_FINISHED = pda_sleep.WAKE_INIT_FINISHED
_EQUIPMENT_POLL_SECONDS = 0.25

_PDA_MENU_LINE = re.compile(r"PDA Menu Line (\d+) =\s*(.*?)\s*$")
_TestResult = TypeVar("_TestResult")


@dataclass(frozen=True)
class PdaScenarioConfig:
    suite_name: str = "pda-live-fast"
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
    spa_fill_seconds: float | None = None
    case_ids: tuple[RuntimeCaseId, ...] = ()
    aquapda_packet_count: int = 20
    aquapda_timeout_seconds: float = 20.0


class ScenarioFailure(RuntimeError):
    """Raised when an expected PDA state transition does not complete."""


class PdaScenarioRuntime:
    def __init__(
        self,
        api: AqualinkApi | None,
        config: PdaScenarioConfig,
        *,
        api_base_url_override: str | None = None,
        api_factory: Callable[[str], AqualinkApi] = AqualinkHttpApi,
        aquapda_client_factory: Callable[[str], AquaPdaClient] = (
            AquaPdaWebSocketClient
        ),
        testcase: TestcaseDefinition | None = None,
        testcases: tuple[TestcaseDefinition, ...] = (),
    ) -> None:
        self._api = api
        self._api_base_url_override = api_base_url_override
        self._api_factory = api_factory
        self._aquapda_client_factory = aquapda_client_factory
        self._config = config
        self._testcase = testcase
        if testcase is not None and testcases:
            raise ValueError("specify testcase or testcases, not both")
        self._testcases = testcases or ((testcase,) if testcase is not None else ())
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
            "site_profile": {
                "spa_fill_seconds": config.spa_fill_seconds,
            },
            "checks": [],
            "aqualinkd": None,
            "panel": None,
            "equipment_status": None,
            "equipment_state_observations": [],
            "sleep_cycle": None,
            "aquapda_transport": None,
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
                            if any(
                                isinstance(step, ExerciseDiscoveredDevicesStep)
                                for testcase in self._testcases
                                for step in testcase.steps
                            )
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
        self._device_selector = PdaDeviceSelector(
            PdaDeviceSelectionConfig(
                requested=config.test_devices,
                disabled_button_numbers=config.disabled_button_numbers,
            ),
            record_skip=self._skip,
        )
        self._reported_panel_size: int | None = None
        self._reported_panel_combo: bool | None = None

    async def run(self, context: ScenarioContext) -> ScenarioOutcome:
        if self._testcases:
            suite_started = time.monotonic()
            if len(self._testcases) > 1:
                print(
                    f"\n=== Starting {self._config.suite_name} ===",
                    flush=True,
                )
            outcome = ScenarioOutcome(
                status="passed",
                reason="scenario_completed",
            )
            for testcase in self._testcases:
                outcome = await self._run_declarative_testcase(context, testcase)
                if outcome.status != "passed":
                    break
            if len(self._testcases) > 1:
                self._report["testcases"] = [
                    testcase.identifier for testcase in self._testcases
                ]
                self._report.pop("testcase", None)
                self._write_report(context)
                self._progress_finished(
                    self._config.suite_name,
                    suite_started,
                    passed=outcome.status == "passed",
                    detail=(None if outcome.status == "passed" else outcome.reason),
                )
            return outcome
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
            case = RUNTIME_CASES[case_id]
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
                if case.id == RuntimeCaseId.INITIALIZATION:
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

    async def _run_declarative_testcase(
        self,
        context: ScenarioContext,
        testcase: TestcaseDefinition,
    ) -> ScenarioOutcome:
        started = time.monotonic()
        print(f"\n=== Starting {testcase.identifier} ===", flush=True)
        await context.timeline.write(
            "scenario_started",
            suite=self._config.suite_name,
            testcase=testcase.identifier,
            api_base_url=self._report["api_base_url"],
            api_endpoint_source=self._report["api_endpoint_source"],
        )
        status = "passed"
        reason = "scenario_completed"
        cancelled = False
        error: BaseException | None = None
        case_started = time.monotonic()
        try:
            execution = await TestcaseExecutor(
                self._testcase_keywords(context, testcase.identifier)
            ).execute(testcase)
            execution_report = {
                "id": execution.identifier,
                "duration_ms": round(execution.duration_seconds * 1000, 3),
                "steps": [
                    {
                        "section": step.section,
                        "index": step.index,
                        "keyword": step.keyword,
                        "duration_ms": round(step.duration_seconds * 1000, 3),
                    }
                    for step in execution.steps
                ],
            }
            self._report.setdefault("testcase_executions", []).append(execution_report)
            self._report["testcase_execution"] = execution_report
        except asyncio.CancelledError as caught:
            error = caught
            cancelled = True
            status = "failed"
            reason = "scenario_cancelled"
        except BaseException as caught:
            error = caught
            status = "failed"
            reason = "testcase_failed"

        final_restoration_errors: list[str] = []
        if self._restoration.has_pending_mutations:
            final_restoration_errors = await self._restore_with_progress(
                context,
                "Final safety restoration",
            )
        if final_restoration_errors:
            status = "failed"
            reason = "restoration_failed"
            self._report["error"] = "; ".join(final_restoration_errors)
        elif error is not None:
            self._report["error"] = self._format_exception(error)

        self._report["cases"].append(
            {
                "id": testcase.identifier,
                "name": testcase.description,
                "status": status,
                "duration_ms": round((time.monotonic() - case_started) * 1000, 3),
                "error": self._format_exception(error) if error else None,
                "restoration": self._report["restoration"]["status"],
            }
        )
        self._report["safe_to_continue"] = bool(
            self._initial_snapshot is not None
            and not cancelled
            and reason != "restoration_failed"
        )
        self._report["status"] = status
        self._report["reason"] = reason
        self._report["testcase"] = testcase.identifier
        self._write_report(context)
        await context.timeline.write(
            "scenario_finished",
            suite=self._config.suite_name,
            testcase=testcase.identifier,
            status=status,
            reason=reason,
        )
        self._progress_finished(
            testcase.identifier,
            started,
            passed=status == "passed",
            detail=None if status == "passed" else reason,
        )
        if cancelled:
            raise asyncio.CancelledError
        return ScenarioOutcome(status=status, reason=reason)

    def _testcase_keywords(
        self,
        context: ScenarioContext,
        testcase_id: str,
    ) -> PdaTestcaseKeywords:
        async def wait_for_stable(
            identifiers: tuple[str, ...],
            timeout_seconds: float,
        ) -> EquipmentSnapshot:
            initial = await self._api_client.devices()
            return await self._wait_for_stable_equipment_snapshot(
                context,
                identifiers,
                phase=f"testcase.{testcase_id}.stable",
                timeout_seconds=timeout_seconds,
                initial_snapshot=initial,
            )

        async def restore(timeout_seconds: float) -> None:
            del timeout_seconds
            errors = await self._restore_original_state(context)
            if errors:
                raise ScenarioFailure("; ".join(errors))

        return PdaTestcaseKeywords(
            events=context.monitor,
            actions=lambda: self._equipment_actions(context),
            restoration=self._restoration,
            markers=PdaKeywordMarkers(
                device=ProgrammerMarkers(
                    "Switch PDA device on/off",
                    DEVICE_ACTIVE,
                    DEVICE_FINISHED,
                ),
                setpoints={
                    POOL_HEATER: ProgrammerMarkers(
                        "Set PDA Pool Heater",
                        POOL_HEATER_SETPOINT_ACTIVE_MARKERS,
                        POOL_HEATER_SETPOINT_FINISHED_MARKERS,
                    ),
                    SPA_HEATER: ProgrammerMarkers(
                        "Set PDA Spa Heater",
                        SPA_HEATER_SETPOINT_ACTIVE_MARKERS,
                        SPA_HEATER_SETPOINT_FINISHED_MARKERS,
                    ),
                },
            ),
            initialize=lambda: self._initialize(context),
            wait_for_stable=wait_for_stable,
            restore=restore,
            verify_status=lambda: self._test_with_status_menu(context),
            exercise_devices=lambda: self._test_consecutive_devices(context),
            exercise_spa_heating=lambda: self._test_spa_heating(context),
            observe_sleep=lambda: self._test_sleep_wake_cycle(context),
            exercise_status_retry=lambda: self._test_device_during_status_retry(
                context
            ),
            exercise_probe_transition=lambda: self._test_device_after_probe(context),
            record_skip=self._skip,
            phase_prefix=f"testcase.{testcase_id}",
        )

    @staticmethod
    def _resolve_case_ids(config: PdaScenarioConfig) -> tuple[RuntimeCaseId, ...]:
        return config.case_ids

    def _uses_selected_devices(self) -> bool:
        declarative_uses_selected_devices = any(
            isinstance(
                step,
                (
                    ExerciseDiscoveredDevicesStep,
                    ExerciseStatusRetryStep,
                    ExerciseProbeTransitionStep,
                ),
            )
            for testcase in self._testcases
            for step in testcase.steps
        )
        return declarative_uses_selected_devices

    def _case_operation(
        self,
        case_id: RuntimeCaseId,
        context: ScenarioContext,
    ) -> Callable[[], Awaitable[None]]:
        operations: dict[RuntimeCaseId, Callable[[], Awaitable[None]]] = {
            RuntimeCaseId.INITIALIZATION: lambda: self._initialize(context),
            RuntimeCaseId.AQUAPDA_TRANSPORT: lambda: self._test_aquapda_transport(
                context
            ),
            RuntimeCaseId.AQUAPDA_MENU_WALK: lambda: self._test_menu_walk(context),
        }
        return operations[case_id]

    async def _test_aquapda_transport(
        self,
        context: ScenarioContext,
    ) -> None:
        try:
            result = await AquaPdaTransportValidator(
                client=self._aquapda_client_factory(self._api_client.base_url),
                events=context.monitor,
                config=AquaPdaTransportConfig(
                    packet_count=self._config.aquapda_packet_count,
                    timeout_seconds=self._config.aquapda_timeout_seconds,
                ),
                progress=lambda message: print(message, flush=True),
            ).validate()
        except AquaPdaValidationFailure as error:
            raise ScenarioFailure(str(error)) from error
        self._report["aquapda_transport"] = result.report

    async def _test_menu_walk(self, context: ScenarioContext) -> None:
        try:
            result = await AquaPdaMenuWalker(
                client=self._aquapda_client_factory(self._api_client.base_url),
                config=AquaPdaMenuWalkConfig(
                    timeout_seconds=self._config.aquapda_timeout_seconds
                ),
                progress=lambda message: print(message, flush=True),
            ).walk()
        except AquaPdaValidationFailure as error:
            raise ScenarioFailure(str(error)) from error
        self._report["menu_walk"] = result.report

    async def _restore_after_case(
        self,
        context: ScenarioContext,
        case: RuntimeCaseDefinition,
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
        constraints = self._device_selector.configure(
            self._initial_snapshot,
            reported_panel_size=self._reported_panel_size,
            reported_panel_combo=self._reported_panel_combo,
        )
        self._report["device_selection"]["excluded"] = list(
            constraints.excluded
        )
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

        try:
            result = await pda_session.PdaSessionInitializer(
                events=context.monitor,
                timeline=context.timeline,
                programmer=self._programmer,
                timeout_seconds=self._config.init_timeout_seconds,
            ).initialize(discover_api=self._api is None)
        except pda_session.PdaSessionFailure as error:
            raise ScenarioFailure(str(error)) from error
        if result.discovered_api_base_url is not None:
            discovered = result.discovered_api_base_url
            self._configure_api(discovered, source="aqualinkd_startup_log")
            await context.timeline.write(
                "api_endpoint_discovered",
                api_base_url=discovered,
                source="aqualinkd_startup_log",
            )
        identity = result.aqualinkd_identity
        self._report["aqualinkd"] = identity
        print(
            f"[INFO  ] AqualinkD version: {identity['version']}",
            flush=True,
        )
        print(
            f"[INFO  ] Configured panel: {identity['configured_panel_type']}",
            flush=True,
        )
        self._append_measurement(
            name="pda.init",
            category="initialization",
            phase="startup",
            target="PDA_INIT",
            requested_value=None,
            start_offset_ns=0,
            api_ack_offset_ns=None,
            task_active_offset_ns=result.active.offset_ns,
            log_completion_offset_ns=result.completed.offset_ns,
            state_observed_offset_ns=None,
        )
        return result.init_screen

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

    async def _record_panel_identity_and_check_time(
        self,
        init_screen: dict[str, str],
    ) -> None:
        daemon_identity = self._report.get("aqualinkd")
        configured_panel = (
            daemon_identity.get("configured_panel_type")
            if isinstance(daemon_identity, dict)
            else None
        )
        try:
            result = await PdaPanelIdentityValidator(
                api=self._api_client,
                config=PdaPanelIdentityConfig(
                    timezone=self._config.panel_timezone,
                    time_tolerance_seconds=(
                        self._config.panel_time_tolerance_seconds
                    ),
                    timeout_seconds=self._config.init_timeout_seconds,
                ),
                progress=lambda message: print(message, flush=True),
            ).validate(
                init_screen=init_screen,
                configured_panel=configured_panel,
            )
        except PdaPanelIdentityFailure as error:
            self._record_panel_identity_result(error.result)
            raise ScenarioFailure(str(error)) from error
        self._record_panel_identity_result(result)

    def _record_panel_identity_result(
        self,
        result: PdaPanelIdentityResult,
    ) -> None:
        self._report["panel"] = result.panel
        self._report["checks"].extend(result.checks)
        self._reported_panel_size = result.reported_panel_size
        self._reported_panel_combo = result.reported_panel_combo
        self._report["device_selection"]["reported_panel_size"] = (
            self._reported_panel_size
        )

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
        identifiers: Sequence[str],
        *,
        phase: str,
        timeout_seconds: float,
        initial_snapshot: EquipmentSnapshot | None = None,
    ) -> EquipmentSnapshot:
        service = EquipmentStabilityService(
            api=self._api_client,
            timeline=context.timeline,
            config=EquipmentStabilityConfig(
                stable_seconds=0.5,
                poll_seconds=_EQUIPMENT_POLL_SECONDS,
            ),
            record_observation=(
                self._report["equipment_state_observations"].append
            ),
            progress=lambda message: print(message, flush=True),
        )
        try:
            return await service.wait(
                identifiers,
                phase=phase,
                timeout_seconds=timeout_seconds,
                initial_snapshot=initial_snapshot,
            )
        except EquipmentStabilityFailure as error:
            raise ScenarioFailure(str(error)) from error

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
        candidates = self._device_selector.status_candidates(
            phase="devices.status_menu.setup"
        )
        try:
            setup = await self._equipment_status_setup(context).prepare(
                self._initial_snapshot,
                candidates,
            )
        except PdaEquipmentSetupFailure as error:
            raise ScenarioFailure(str(error)) from error
        controls = list(setup.controls)
        setup_states = setup.states
        if not controls:
            self._skip(
                "devices.status_menu",
                "No configured equipment can be enabled for status testing",
            )
            return

        cursor = context.monitor.cursor
        wait_started = context.timeline.offset_ns()
        status_service = PdaEquipmentStatusService(
            events=context.monitor,
            wait_for_stable=lambda identifiers, phase, timeout: (
                self._wait_for_stable_equipment_snapshot(
                    context,
                    identifiers,
                    phase=phase,
                    timeout_seconds=timeout,
                )
            ),
            status_timeout_seconds=self._config.status_timeout_seconds,
            state_timeout_seconds=self._config.state_timeout_seconds,
            progress=lambda message: print(message, flush=True),
        )
        loop = await status_service.wait_for_complete_loop(after=cursor)
        try:
            result = await status_service.verify(
                initial_snapshot=self._initial_snapshot,
                controls=controls,
                events=loop.events,
                setup_states=setup_states,
            )
        except PdaEquipmentStatusFailure as error:
            if error.result is not None:
                self._report["equipment_status"] = error.result.report
            raise ScenarioFailure(str(error)) from error
        verification = result.report
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
            log_completion_offset_ns=loop.reconciled.offset_ns,
            state_observed_offset_ns=context.timeline.offset_ns(),
        )

    def _equipment_status_setup(
        self,
        context: ScenarioContext,
    ) -> PdaEquipmentStatusSetup:
        async def set_heater_setpoint(
            identifier: str,
            value: int,
            phase: str,
        ) -> None:
            active, completed = self._heater_setpoint_markers(identifier)
            await self._set_setpoint(
                context,
                identifier,
                value,
                phase=phase,
                active_marker=active,
                completion_marker=completed,
                category="heater_safety",
            )

        return PdaEquipmentStatusSetup(
            api=self._api_client,
            events=context.monitor,
            config=PdaEquipmentSetupConfig(
                status_timeout_seconds=self._config.status_timeout_seconds,
                restoration_timeout_seconds=(
                    self._config.restoration_timeout_seconds
                ),
                poll_seconds=_EQUIPMENT_POLL_SECONDS,
            ),
            set_device=lambda identifier, enabled, phase, timeout: (
                self._set_device(
                    context,
                    identifier,
                    enabled,
                    phase=phase,
                    state_timeout_seconds=timeout,
                )
            ),
            set_setpoint=set_heater_setpoint,
            wait_for_stable=lambda identifiers, phase, timeout: (
                self._wait_for_stable_equipment_snapshot(
                    context,
                    identifiers,
                    phase=phase,
                    timeout_seconds=timeout,
                )
            ),
            record_skip=self._skip,
            progress=lambda message: print(message, flush=True),
        )

    @staticmethod
    def _heater_setpoint_markers(
        identifier: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        markers = {
            POOL_HEATER: (
                POOL_HEATER_SETPOINT_ACTIVE_MARKERS,
                POOL_HEATER_SETPOINT_FINISHED_MARKERS,
            ),
            SPA_HEATER: (
                SPA_HEATER_SETPOINT_ACTIVE_MARKERS,
                SPA_HEATER_SETPOINT_FINISHED_MARKERS,
            ),
        }.get(identifier)
        if markers is None:
            raise ScenarioFailure(
                f"No PDA heater setpoint markers for {identifier}"
            )
        return markers

    async def _test_spa_heating(self, context: ScenarioContext) -> None:
        assert self._initial_snapshot is not None
        fill_seconds = self._config.spa_fill_seconds
        if fill_seconds is None:
            raise ScenarioFailure(
                "pda-live-spa requires spa.fill_time in "
                "aqualinkd-validator.yaml beside aqualinkd.conf"
            )
        exercise = PdaSpaExercise(
            api=self._api_client,
            config=SpaExerciseConfig(
                fill_seconds=fill_seconds,
                active_timeout_seconds=self._config.status_timeout_seconds,
                transition_timeout_seconds=self._config.restoration_timeout_seconds,
            ),
            set_device=lambda identifier, enabled, phase, timeout: self._set_device(
                context,
                identifier,
                enabled,
                phase=phase,
                state_timeout_seconds=timeout,
            ),
            set_setpoint=lambda identifier, value, phase: self._set_setpoint(
                context,
                identifier,
                value,
                phase=phase,
                active_marker=SPA_HEATER_SETPOINT_ACTIVE_MARKERS,
                completion_marker=SPA_HEATER_SETPOINT_FINISHED_MARKERS,
                category="spa_heating",
            ),
            record_measurement=self._append_measurement,
            record_skip=self._skip,
            offset_ns=context.timeline.offset_ns,
        )
        await exercise.run(self._initial_snapshot)

    async def _test_consecutive_devices(
        self,
        context: ScenarioContext,
    ) -> None:
        assert self._initial_snapshot is not None
        try:
            identifiers = list(
                self._device_selector.consecutive_switches(
                    phase="devices.consecutive"
                )
            )
        except PdaDeviceSelectionFailure as error:
            raise ScenarioFailure(str(error)) from error
        self._report["device_selection"]["resolved"] = identifiers
        if not identifiers:
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
        if self._device_selector.skip_unactionable(
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

        await self._testcase_keywords(
            context,
            "legacy.pool-heater",
        ).exercise_heater(
            ExerciseHeaterStep(
                identifier=POOL_HEATER,
                optional=True,
                activation_timeout_seconds=self._config.activation_timeout_seconds,
                completion_timeout_seconds=self._config.action_timeout_seconds,
                convergence_timeout_seconds=self._config.state_timeout_seconds,
            )
        )

    async def _test_sleep_wake_cycle(self, context: ScenarioContext) -> None:
        try:
            result = await self._sleep_wake_service(context).observe_natural_cycle()
        except PdaSleepWakeFailure as error:
            raise ScenarioFailure(str(error)) from error
        self._report["sleep_cycle"] = result.report

    def _sleep_test_device(self, *, phase: str) -> str | None:
        try:
            identifier = self._device_selector.sleep_switch(phase=phase)
        except PdaDeviceSelectionFailure as error:
            raise ScenarioFailure(str(error)) from error
        if identifier is None:
            return None
        self._report["device_selection"]["resolved"] = [identifier]
        return identifier

    def _sleep_wake_service(
        self,
        context: ScenarioContext,
    ) -> PdaSleepWakeService:
        return PdaSleepWakeService(
            events=context.monitor,
            timeline=context.timeline,
            programmer=self._programmer,
            config=PdaSleepWakeConfig(
                sleep_timeout_seconds=self._config.sleep_timeout_seconds,
                action_timeout_seconds=self._config.action_timeout_seconds,
                status_retry_delay_seconds=(
                    self._config.status_retry_command_delay_seconds
                ),
                probe_command_min_delay_seconds=(
                    self._config.probe_command_min_delay_seconds
                ),
            ),
            record_measurement=self._append_measurement,
            progress=lambda message: print(message, flush=True),
        )

    async def _test_device_during_status_retry(
        self,
        context: ScenarioContext,
    ) -> None:
        phase = "devices.sleep.status_retry"
        identifier = self._sleep_test_device(phase=phase)
        if identifier is None:
            return
        try:
            window = await self._sleep_wake_service(
                context
            ).wait_for_status_retry_window()
        except PdaSleepWakeFailure as error:
            raise ScenarioFailure(str(error)) from error
        print(
            f"[STATE ] Observed {window.retry_count} repeated PDA STATUS "
            f"packet(s); toggling {identifier}",
            flush=True,
        )
        await self._toggle_round_trip(context, identifier, phase=phase)

    async def _test_device_after_probe(
        self,
        context: ScenarioContext,
    ) -> None:
        phase = "devices.sleep.probing"
        identifier = self._sleep_test_device(phase=phase)
        if identifier is None:
            return
        try:
            window = await self._sleep_wake_service(
                context
            ).wait_for_probe_window()
        except PdaSleepWakeFailure as error:
            raise ScenarioFailure(str(error)) from error
        print(
            f"[STATE ] PDA address probe observed "
            f"{window.probe_delay_seconds:.3f}s after sleep began; "
            f"toggling {identifier}",
            flush=True,
        )
        await self._toggle_round_trip(context, identifier, phase=phase)

    async def _toggle_round_trip_unless_disabled(
        self,
        context: ScenarioContext,
        identifier: str,
        *,
        phase: str,
    ) -> None:
        if self._device_selector.skip_unactionable(identifier, phase=phase):
            return
        await self._toggle_round_trip(context, identifier, phase=phase)

    def _equipment_actions(
        self,
        context: ScenarioContext,
    ) -> EquipmentActions:
        async def wait_for_stable(
            identifier: str,
            phase: str,
            initial: EquipmentSnapshot,
            timeout_seconds: float,
        ) -> EquipmentSnapshot:
            return await self._wait_for_stable_equipment_snapshot(
                context,
                [identifier],
                phase=phase,
                timeout_seconds=timeout_seconds,
                initial_snapshot=initial,
            )

        return EquipmentActions(
            api=self._api_client,
            events=context.monitor,
            timeline=context.timeline,
            programmer=self._programmer,
            restoration=self._restoration,
            timeouts=EquipmentActionTimeouts(
                activation_seconds=self._config.activation_timeout_seconds,
                completion_seconds=self._config.action_timeout_seconds,
                convergence_seconds=self._config.state_timeout_seconds,
                stabilization_seconds=(self._config.restoration_timeout_seconds),
            ),
            wait_for_stable=wait_for_stable,
            record_measurement=self._report["measurements"].append,
            record_skip=self._skip,
        )

    async def _set_device(
        self,
        context: ScenarioContext,
        identifier: str,
        enabled: bool,
        *,
        phase: str,
        state_timeout_seconds: float | None = None,
    ) -> None:
        try:
            await self._equipment_actions(context).set_device(
                identifier,
                enabled,
                phase=phase,
                markers=ProgrammerMarkers(
                    task_name="Switch PDA device on/off",
                    active=DEVICE_ACTIVE,
                    completed=DEVICE_FINISHED,
                ),
                convergence_timeout_seconds=state_timeout_seconds,
            )
        except (EquipmentActionFailure, PdaProgrammerFailure) as error:
            raise ScenarioFailure(str(error)) from error

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
        task_name = {
            POOL_HEATER: "Set PDA Pool Heater",
            SPA_HEATER: "Set PDA Spa Heater",
        }.get(identifier, identifier)
        try:
            await self._equipment_actions(context).set_setpoint(
                identifier,
                value,
                phase=phase,
                category=category,
                markers=ProgrammerMarkers(
                    task_name=task_name,
                    active=active_marker,
                    completed=completion_marker,
                ),
            )
        except (EquipmentActionFailure, PdaProgrammerFailure) as error:
            raise ScenarioFailure(str(error)) from error

    async def _wait_for_device_state(
        self,
        context: ScenarioContext,
        identifier: str,
        enabled: bool,
        *,
        timeout_seconds: float | None = None,
    ) -> int:
        timeout = timeout_seconds or self._config.state_timeout_seconds
        try:
            return await self._equipment_actions(context).wait_for_device_state(
                identifier,
                enabled,
                timeout_seconds=timeout,
            )
        except EquipmentActionFailure as error:
            raise ScenarioFailure(str(error)) from error

    async def _current_device_enabled(self, identifier: str) -> bool:
        snapshot = await self._api_client.devices()
        return self._device_enabled(self._require_device(snapshot, identifier))

    def _initial_device_enabled(self, identifier: str) -> bool:
        return self._restoration.initial_device_enabled(identifier)

    async def _restore_original_state(
        self,
        context: ScenarioContext,
    ) -> list[str]:
        restoration = self._report["restoration"]
        if self._initial_snapshot is None:
            return []
        restoration["attempted"] = True

        async def restore_setpoint(identifier: str, original: int) -> None:
            markers = {
                POOL_HEATER: (
                    POOL_HEATER_SETPOINT_ACTIVE_MARKERS,
                    POOL_HEATER_SETPOINT_FINISHED_MARKERS,
                ),
                SPA_HEATER: (
                    SPA_HEATER_SETPOINT_ACTIVE_MARKERS,
                    SPA_HEATER_SETPOINT_FINISHED_MARKERS,
                ),
            }.get(identifier)
            if markers is None:
                raise ScenarioFailure(
                    f"No restoration programmer markers for {identifier}"
                )
            await self._set_setpoint(
                context,
                identifier,
                original,
                phase="restoration.setpoint",
                active_marker=markers[0],
                completion_marker=markers[1],
                category="restoration",
            )

        async def wait_for_stable(
            identifiers: Sequence[str],
            phase: str,
            timeout: float,
            initial: EquipmentSnapshot,
        ) -> EquipmentSnapshot:
            return await self._wait_for_stable_equipment_snapshot(
                context,
                identifiers,
                phase=phase,
                timeout_seconds=timeout,
                initial_snapshot=initial,
            )

        async def wait_for_device_state(
            identifier: str,
            expected: bool,
            timeout: float,
        ) -> None:
            await self._wait_for_device_state(
                context,
                identifier,
                expected,
                timeout_seconds=timeout,
            )

        result = await PdaRestorationService(
            api=self._api_client,
            session=self._restoration,
            config=PdaRestorationConfig(
                timeout_seconds=self._config.restoration_timeout_seconds
            ),
            set_device=lambda identifier, enabled, phase, timeout: (
                self._set_device(
                    context,
                    identifier,
                    enabled,
                    phase=phase,
                    state_timeout_seconds=timeout,
                )
            ),
            set_setpoint=restore_setpoint,
            wait_for_stable=wait_for_stable,
            wait_for_device_state=wait_for_device_state,
            progress=lambda message: print(message, flush=True),
        ).restore(
            self._initial_snapshot,
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

    @staticmethod
    def _requested_device_state_label(
        device: DeviceState | Mapping[str, Any],
        enabled: bool,
    ) -> str:
        return PdaScenarioRuntime._device_state(device).requested_state_label(enabled)
