from __future__ import annotations

from collections.abc import Awaitable, Callable

from .adapters import AqualinkHttpApi, AquaPdaWebSocketClient
from .domain import EquipmentSnapshot
from .engine import (
    ProgrammerMarkers,
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
    PdaDeviceSelector,
)
from .protocols.pda.aquapda import (
    AquaPdaMenuWalkConfig,
    AquaPdaMenuWalker,
    AquaPdaTransportConfig,
    AquaPdaTransportValidator,
    AquaPdaValidationFailure,
    return_aquapda_home,
)
from .protocols.pda.equipment_status import (
    STATUS_MENU_FINISHED_MARKERS,
    STATUS_MENU_PRESENT_MARKERS,
)
from .protocols.pda.keywords import PdaKeywordMarkers, PdaTestcaseKeywords
from .protocols.pda.live_exercises import (
    PdaLiveExercises,
)
from .protocols.pda.run_report import PdaRunReport, PdaRunReportConfig
from .protocols.pda.run_session import PdaRunSession
from .protocols.pda.runtime_config import (
    DEVICE_ACTIVE,
    DEVICE_FINISHED,
    POOL_HEATER,
    POOL_HEATER_SETPOINT_ACTIVE_MARKERS,
    POOL_HEATER_SETPOINT_FINISHED_MARKERS,
    SPA_HEATER,
    SPA_HEATER_SETPOINT_ACTIVE_MARKERS,
    SPA_HEATER_SETPOINT_FINISHED_MARKERS,
    PdaScenarioConfig,
)
from .run_targets import RuntimeCaseId
from .testcases import (
    DeclarativeScenarioRunner,
    ExerciseDiscoveredDevicesStep,
    ExerciseProbeTransitionStep,
    ExerciseStatusRetryStep,
    TestcaseDefinition,
)


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
        self._aquapda_client_factory = aquapda_client_factory
        self._config = config
        if testcase is not None and testcases:
            raise ValueError("specify testcase or testcases, not both")
        self._testcases = testcases or ((testcase,) if testcase is not None else ())
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
        selection_mode = (
            "not_applicable"
            if not self._uses_selected_devices()
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
        )
        self._run_report = PdaRunReport(
            PdaRunReportConfig(
                suite_name=config.suite_name,
                execution_phase=config.execution_phase,
                api_base_url=(
                    api.base_url if api is not None else api_base_url_override
                ),
                api_endpoint_source=endpoint_source,
                activation_timeout_seconds=config.activation_timeout_seconds,
                action_timeout_seconds=config.action_timeout_seconds,
                status_timeout_seconds=config.status_timeout_seconds,
                state_timeout_seconds=config.state_timeout_seconds,
                restoration_timeout_seconds=config.restoration_timeout_seconds,
                init_timeout_seconds=config.init_timeout_seconds,
                sleep_timeout_seconds=config.sleep_timeout_seconds,
                status_retry_command_delay_seconds=(
                    config.status_retry_command_delay_seconds
                ),
                probe_command_min_delay_seconds=(
                    config.probe_command_min_delay_seconds
                ),
                spa_fill_seconds=config.spa_fill_seconds,
                device_selection_mode=selection_mode,
                requested_devices=config.test_devices,
                disabled_button_numbers=config.disabled_button_numbers,
            )
        )
        device_selector = PdaDeviceSelector(
            PdaDeviceSelectionConfig(
                requested=config.test_devices,
                disabled_button_numbers=config.disabled_button_numbers,
            ),
            record_skip=self._run_report.recorder.skip,
        )
        self._session = PdaRunSession(
            api=api,
            api_base_url_override=api_base_url_override,
            api_factory=api_factory,
            config=config,
            report=self._run_report,
            device_selector=device_selector,
        )
        self._exercises = PdaLiveExercises(
            self._session,
            return_home_for_status=(
                self._return_home_for_status
                if config.force_status_home_with_aquapda
                else None
            ),
        )

    async def run(self, context: ScenarioContext) -> ScenarioOutcome:
        if self._testcases:
            return await DeclarativeScenarioRunner(
                suite_name=self._config.suite_name,
                testcases=self._testcases,
                report=self._session.report,
                recorder=self._session.recorder,
                restoration=self._session.restoration,
                keywords=lambda testcase_id: self._testcase_keywords(
                    context,
                    testcase_id,
                ),
                restore=self._session.restore_with_progress,
                initialized=lambda: self._session.initial_snapshot is not None,
            ).run(context)
        return await RuntimeCaseRunner(
            suite_name=self._config.suite_name,
            case_ids=self._case_ids,
            report=self._session.report,
            recorder=self._session.recorder,
            restoration=self._session.restoration,
            operation=lambda case_id: self._case_operation(case_id, context)(),
            restore=lambda name: self._session.restore_with_progress(context, name),
            initialized=lambda: self._session.initial_snapshot is not None,
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
            initial = await self._session.api_client.devices()
            return await self._session.wait_for_stable(
                context,
                identifiers,
                phase=f"testcase.{testcase_id}.stable",
                timeout_seconds=timeout_seconds,
                initial_snapshot=initial,
            )

        async def restore(timeout_seconds: float) -> None:
            del timeout_seconds
            errors = await self._session.restore(context)
            if errors:
                raise ScenarioFailure("; ".join(errors))

        return PdaTestcaseKeywords(
            events=context.monitor,
            actions=lambda: self._session.equipment_actions(context),
            restoration=self._session.restoration,
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
            initialize=lambda: self._session.initialize(context),
            wait_for_stable=wait_for_stable,
            restore=restore,
            verify_status=lambda: self._exercises.verify_equipment_status(context),
            exercise_devices=lambda: self._exercises.exercise_consecutive_devices(
                context
            ),
            exercise_spa_heating=lambda: self._exercises.exercise_spa_heating(
                context
            ),
            observe_sleep=lambda: self._exercises.observe_sleep_cycle(context),
            exercise_status_retry=lambda: self._exercises.exercise_status_retry(
                context
            ),
            exercise_probe_transition=lambda: (
                self._exercises.exercise_probe_transition(context)
            ),
            record_skip=self._session.recorder.skip,
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
            RuntimeCaseId.INITIALIZATION: lambda: self._session.initialize(context),
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
                client=self._aquapda_client_factory(
                    self._session.api_client.base_url
                ),
                events=context.monitor,
                config=AquaPdaTransportConfig(
                    packet_count=self._config.aquapda_packet_count,
                    timeout_seconds=self._config.aquapda_timeout_seconds,
                ),
                progress=lambda message: print(message, flush=True),
            ).validate()
        except AquaPdaValidationFailure as error:
            raise ScenarioFailure(str(error)) from error
        self._session.report["aquapda_transport"] = result.report

    async def _return_home_for_status(self, context: ScenarioContext) -> None:
        recent = context.monitor.recent_events()
        last_started = max(
            (
                event.sequence
                for event in recent
                if any(
                    marker in event.text for marker in STATUS_MENU_PRESENT_MARKERS
                )
            ),
            default=0,
        )
        last_finished = max(
            (
                event.sequence
                for event in recent
                if any(
                    marker in event.text
                    for marker in STATUS_MENU_FINISHED_MARKERS
                )
            ),
            default=0,
        )
        if last_started > last_finished:
            print(
                "[STATE ] Equipment status is already active; "
                "leaving SimPDA navigation unchanged",
                flush=True,
            )
            return
        client = self._aquapda_client_factory(self._session.api_client.base_url)
        print(
            "[ WAIT ] Equipment status: returning the simulated PDA to home",
            flush=True,
        )
        try:
            await client.connect()
            packet_start = client.packet_count
            await client.wait_for_packets(
                6,
                after=packet_start,
                timeout_seconds=self._config.aquapda_timeout_seconds,
            )
            await return_aquapda_home(
                client,
                timeout_seconds=self._config.aquapda_timeout_seconds,
                home_attempts=8,
                progress=lambda message: print(message, flush=True),
            )
        except Exception as error:
            raise ScenarioFailure(
                f"could not return the simulated PDA home: {error}"
            ) from error
        finally:
            await client.close()

    async def _test_menu_walk(self, context: ScenarioContext) -> None:
        try:
            result = await AquaPdaMenuWalker(
                client=self._aquapda_client_factory(
                    self._session.api_client.base_url
                ),
                config=AquaPdaMenuWalkConfig(
                    timeout_seconds=self._config.aquapda_timeout_seconds
                ),
                progress=lambda message: print(message, flush=True),
            ).walk()
        except AquaPdaValidationFailure as error:
            raise ScenarioFailure(str(error)) from error
        self._session.report["menu_walk"] = result.report
