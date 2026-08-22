from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .adapters import ApiError, AqualinkHttpApi, AquaPdaWebSocketClient
from .domain import DeviceState, EquipmentSnapshot
from .engine import (
    EquipmentActions,
    ProgrammerMarkers,
    RestorationSession,
    ScenarioRecorder,
)
from .engine.runtime_cases import RuntimeCaseRunner
from .interfaces import (
    AqualinkApi,
    AquaPdaClient,
    ScenarioContext,
    ScenarioOutcome,
)
from .protocols.pda import (
    PdaDeviceSelectionConfig,
    PdaDeviceSelectionFailure,
    PdaDeviceSelector,
    PdaEquipmentStatusExercise,
    PdaEquipmentStatusFailure,
    PdaEquipmentStatusService,
    PdaPanelIdentityFailure,
    PdaPanelIdentityResult,
    PdaProgrammerObserver,
    PdaSleepWakeConfig,
    PdaSleepWakeFailure,
    PdaSleepWakeService,
    PdaStartupConfig,
    PdaStartupCoordinator,
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
from .protocols.pda.equipment_control import (
    PdaEquipmentControlConfig,
    PdaEquipmentControlFailure,
    PdaEquipmentController,
)
from .protocols.pda.equipment_setup import (
    PdaEquipmentSetupConfig,
    PdaEquipmentSetupFailure,
    PdaEquipmentStatusSetup,
)
from .protocols.pda.keywords import PdaKeywordMarkers, PdaTestcaseKeywords
from .protocols.pda.restoration_coordinator import (
    PdaRestorationCoordinator,
    PdaRestorationCoordinatorConfig,
)
from .protocols.pda.spa import PdaSpaExercise, SpaExerciseConfig
from .run_targets import RuntimeCaseId
from .testcases import (
    DeclarativeScenarioRunner,
    ExerciseDiscoveredDevicesStep,
    ExerciseProbeTransitionStep,
    ExerciseStatusRetryStep,
    TestcaseDefinition,
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
        self._recorder = ScenarioRecorder(self._report)
        self._initial_snapshot: EquipmentSnapshot | None = None
        self._restoration = RestorationSession()
        self._device_selector = PdaDeviceSelector(
            PdaDeviceSelectionConfig(
                requested=config.test_devices,
                disabled_button_numbers=config.disabled_button_numbers,
            ),
            record_skip=self._recorder.skip,
        )
        self._reported_panel_size: int | None = None
        self._reported_panel_combo: bool | None = None

    async def run(self, context: ScenarioContext) -> ScenarioOutcome:
        if self._testcases:
            return await DeclarativeScenarioRunner(
                suite_name=self._config.suite_name,
                testcases=self._testcases,
                report=self._report,
                recorder=self._recorder,
                restoration=self._restoration,
                keywords=lambda testcase_id: self._testcase_keywords(
                    context,
                    testcase_id,
                ),
                restore=self._restore_with_progress,
                initialized=lambda: self._initial_snapshot is not None,
            ).run(context)
        return await RuntimeCaseRunner(
            suite_name=self._config.suite_name,
            case_ids=self._case_ids,
            report=self._report,
            recorder=self._recorder,
            restoration=self._restoration,
            operation=lambda case_id: self._case_operation(case_id, context)(),
            restore=lambda name: self._restore_with_progress(context, name),
            initialized=lambda: self._initial_snapshot is not None,
        ).run(context)

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
            record_skip=self._recorder.skip,
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

    async def _restore_with_progress(
        self,
        context: ScenarioContext,
        name: str,
    ) -> list[str]:
        started = self._recorder.progress_started(name)
        errors = await self._restore_original_state(context)
        self._recorder.progress_finished(
            name,
            started,
            passed=not errors,
            detail="; ".join(errors) if errors else None,
        )
        return errors

    async def _initialize(self, context: ScenarioContext) -> None:
        async def stabilize(
            api: AqualinkApi,
            identifiers: Sequence[str],
            initial_snapshot: EquipmentSnapshot,
        ) -> EquipmentSnapshot:
            del api
            return await self._wait_for_stable_equipment_snapshot(
                context,
                identifiers,
                phase="initialization.snapshot",
                timeout_seconds=self._config.init_timeout_seconds,
                initial_snapshot=initial_snapshot,
            )

        try:
            result = await PdaStartupCoordinator(
                events=context.monitor,
                timeline=context.timeline,
                programmer=self._programmer,
                api_factory=self._api_factory,
                config=PdaStartupConfig(
                    init_timeout_seconds=self._config.init_timeout_seconds,
                    api_timeout_seconds=self._config.action_timeout_seconds,
                    panel_timezone=self._config.panel_timezone,
                    panel_time_tolerance_seconds=(
                        self._config.panel_time_tolerance_seconds
                    ),
                ),
                progress=lambda message: print(message, flush=True),
                retryable_api_errors=(ApiError,),
            ).initialize(
                api=self._api,
                api_base_url_override=self._api_base_url_override,
                api_configured=self._api_configured,
                session_observed=self._record_startup_session,
                stabilize=stabilize,
            )
        except PdaPanelIdentityFailure as error:
            self._record_panel_identity_result(error.result)
            raise ScenarioFailure(str(error)) from error
        except (pda_session.PdaSessionFailure, RuntimeError) as error:
            raise ScenarioFailure(str(error)) from error

        self._initial_snapshot = result.snapshot
        self._restoration.capture_initial(self._initial_snapshot)
        self._record_panel_identity_result(result.panel_identity)
        constraints = self._device_selector.configure(
            self._initial_snapshot,
            reported_panel_size=self._reported_panel_size,
            reported_panel_combo=self._reported_panel_combo,
        )
        self._report["device_selection"]["excluded"] = list(
            constraints.excluded
        )
        self._require_device(self._initial_snapshot, FILTER_PUMP)

    def _record_startup_session(self, result: pda_session.PdaStartupResult) -> None:
        self._report["aqualinkd"] = result.aqualinkd_identity
        self._recorder.append_measurement(
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

    def _api_configured(self, api: AqualinkApi, source: str) -> None:
        self._api = api
        self._report["api_base_url"] = self._api.base_url
        self._report["api_endpoint_source"] = source

    @property
    def _api_client(self) -> AqualinkApi:
        if self._api is None:
            raise ScenarioFailure("AqualinkD HTTP API endpoint is not configured")
        return self._api

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

    async def _wait_for_stable_equipment_snapshot(
        self,
        context: ScenarioContext,
        identifiers: Sequence[str],
        *,
        phase: str,
        timeout_seconds: float,
        initial_snapshot: EquipmentSnapshot | None = None,
    ) -> EquipmentSnapshot:
        try:
            return await self._equipment_control(context).wait_for_stable(
                identifiers,
                phase=phase,
                timeout_seconds=timeout_seconds,
                initial_snapshot=initial_snapshot,
            )
        except PdaEquipmentControlFailure as error:
            raise ScenarioFailure(str(error)) from error

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
        try:
            result = await PdaEquipmentStatusExercise(
                events=context.monitor,
                timeline=context.timeline,
                setup=self._equipment_status_setup(context),
                status=status_service,
                record_skip=self._recorder.skip,
                record_measurement=self._recorder.append_measurement,
                progress=lambda message: print(message, flush=True),
            ).run(
                initial_snapshot=self._initial_snapshot,
                candidates=candidates,
            )
        except PdaEquipmentSetupFailure as error:
            raise ScenarioFailure(str(error)) from error
        except PdaEquipmentStatusFailure as error:
            if error.result is not None:
                self._report["equipment_status"] = error.result.report
            raise ScenarioFailure(str(error)) from error
        if result.verification is not None:
            self._report["equipment_status"] = result.verification.report

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
            record_skip=self._recorder.skip,
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
            record_measurement=self._recorder.append_measurement,
            record_skip=self._recorder.skip,
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
            record_measurement=self._recorder.append_measurement,
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

    def _equipment_actions(
        self,
        context: ScenarioContext,
    ) -> EquipmentActions:
        return self._equipment_control(context).actions()

    def _equipment_control(
        self,
        context: ScenarioContext,
    ) -> PdaEquipmentController:
        return PdaEquipmentController(
            api=self._api_client,
            events=context.monitor,
            timeline=context.timeline,
            programmer=self._programmer,
            restoration=self._restoration,
            config=PdaEquipmentControlConfig(
                activation_timeout_seconds=(
                    self._config.activation_timeout_seconds
                ),
                action_timeout_seconds=self._config.action_timeout_seconds,
                state_timeout_seconds=self._config.state_timeout_seconds,
                restoration_timeout_seconds=(
                    self._config.restoration_timeout_seconds
                ),
                poll_seconds=_EQUIPMENT_POLL_SECONDS,
            ),
            record_measurement=self._report["measurements"].append,
            record_observation=(
                self._report["equipment_state_observations"].append
            ),
            record_skip=self._recorder.skip,
            progress=lambda message: print(message, flush=True),
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
            await self._equipment_control(context).set_device(
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
        except PdaEquipmentControlFailure as error:
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
            await self._equipment_control(context).set_setpoint(
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
        except PdaEquipmentControlFailure as error:
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
            return await self._equipment_control(context).wait_for_device_state(
                identifier,
                enabled,
                timeout_seconds=timeout,
            )
        except PdaEquipmentControlFailure as error:
            raise ScenarioFailure(str(error)) from error

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

        result = await PdaRestorationCoordinator(
            api=self._api_client,
            session=self._restoration,
            control=self._equipment_control(context),
            config=PdaRestorationCoordinatorConfig(
                timeout_seconds=self._config.restoration_timeout_seconds,
                device_markers=ProgrammerMarkers(
                    "Switch PDA device on/off",
                    DEVICE_ACTIVE,
                    DEVICE_FINISHED,
                ),
                setpoint_markers={
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
            progress=lambda message: print(message, flush=True),
        ).restore(self._initial_snapshot)
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
