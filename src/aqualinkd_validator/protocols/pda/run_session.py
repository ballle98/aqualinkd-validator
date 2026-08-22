from __future__ import annotations

from collections.abc import Callable, Sequence

from ...adapters import ApiError
from ...domain import DeviceState, EquipmentSnapshot
from ...engine import EquipmentActions, ProgrammerMarkers, RestorationSession
from ...interfaces import AqualinkApi, ScenarioContext
from .device_selection import PdaDeviceSelector
from .equipment_control import (
    PdaEquipmentControlConfig,
    PdaEquipmentControlFailure,
    PdaEquipmentController,
)
from .identity import PdaPanelIdentityFailure, PdaPanelIdentityResult
from .programmer import PdaProgrammerObserver
from .restoration_coordinator import (
    PdaRestorationCoordinator,
    PdaRestorationCoordinatorConfig,
)
from .run_report import PdaRunReport
from .runtime_config import (
    DEVICE_ACTIVE,
    DEVICE_FINISHED,
    EQUIPMENT_POLL_SECONDS,
    FILTER_PUMP,
    POOL_HEATER,
    POOL_HEATER_SETPOINT_ACTIVE_MARKERS,
    POOL_HEATER_SETPOINT_FINISHED_MARKERS,
    SPA_HEATER,
    SPA_HEATER_SETPOINT_ACTIVE_MARKERS,
    SPA_HEATER_SETPOINT_FINISHED_MARKERS,
    PdaScenarioConfig,
)
from .session import PdaSessionFailure, PdaStartupResult
from .startup import PdaStartupConfig, PdaStartupCoordinator


class PdaRunSessionFailure(RuntimeError):
    """Raised when shared PDA run state cannot be initialized or restored."""


class PdaRunSession:
    """Own mutable state and common services for one PDA validator process."""

    def __init__(
        self,
        *,
        api: AqualinkApi | None,
        api_base_url_override: str | None,
        api_factory: Callable[[str], AqualinkApi],
        config: PdaScenarioConfig,
        report: PdaRunReport,
        device_selector: PdaDeviceSelector,
    ) -> None:
        self._api = api
        self._api_base_url_override = api_base_url_override
        self._api_factory = api_factory
        self.config = config
        self.run_report = report
        self.report = report.data
        self.recorder = report.recorder
        self.device_selector = device_selector
        self.programmer = PdaProgrammerObserver()
        self.restoration = RestorationSession()
        self.initial_snapshot: EquipmentSnapshot | None = None
        self.reported_panel_size: int | None = None
        self.reported_panel_combo: bool | None = None

    @property
    def api_client(self) -> AqualinkApi:
        if self._api is None:
            raise PdaRunSessionFailure(
                "AqualinkD HTTP API endpoint is not configured"
            )
        return self._api

    async def initialize(self, context: ScenarioContext) -> None:
        async def stabilize(
            api: AqualinkApi,
            identifiers: Sequence[str],
            initial_snapshot: EquipmentSnapshot,
        ) -> EquipmentSnapshot:
            del api
            return await self.wait_for_stable(
                context,
                identifiers,
                phase="initialization.snapshot",
                timeout_seconds=self.config.init_timeout_seconds,
                initial_snapshot=initial_snapshot,
            )

        try:
            result = await PdaStartupCoordinator(
                events=context.monitor,
                timeline=context.timeline,
                programmer=self.programmer,
                api_factory=self._api_factory,
                config=PdaStartupConfig(
                    init_timeout_seconds=self.config.init_timeout_seconds,
                    api_timeout_seconds=self.config.action_timeout_seconds,
                    panel_timezone=self.config.panel_timezone,
                    panel_time_tolerance_seconds=(
                        self.config.panel_time_tolerance_seconds
                    ),
                ),
                progress=lambda message: print(message, flush=True),
                retryable_api_errors=(ApiError,),
            ).initialize(
                api=self._api,
                api_base_url_override=self._api_base_url_override,
                api_configured=self._api_configured,
                session_observed=self._record_startup,
                stabilize=stabilize,
            )
        except PdaPanelIdentityFailure as error:
            self._record_panel_identity(error.result)
            raise PdaRunSessionFailure(str(error)) from error
        except (PdaSessionFailure, RuntimeError) as error:
            raise PdaRunSessionFailure(str(error)) from error

        self.initial_snapshot = result.snapshot
        self.restoration.capture_initial(result.snapshot)
        self._record_panel_identity(result.panel_identity)
        constraints = self.device_selector.configure(
            result.snapshot,
            reported_panel_size=self.reported_panel_size,
            reported_panel_combo=self.reported_panel_combo,
        )
        self.report["device_selection"]["excluded"] = list(
            constraints.excluded
        )
        self.require_device(result.snapshot, FILTER_PUMP)

    def _record_startup(self, result: PdaStartupResult) -> None:
        self.run_report.record_startup(result, self.recorder)

    def _api_configured(self, api: AqualinkApi, source: str) -> None:
        self._api = api
        self.run_report.configure_api(api.base_url, source)

    def _record_panel_identity(self, result: PdaPanelIdentityResult) -> None:
        self.reported_panel_size = result.reported_panel_size
        self.reported_panel_combo = result.reported_panel_combo
        self.run_report.record_panel_identity(result)

    def equipment_control(
        self,
        context: ScenarioContext,
    ) -> PdaEquipmentController:
        return PdaEquipmentController(
            api=self.api_client,
            events=context.monitor,
            timeline=context.timeline,
            programmer=self.programmer,
            restoration=self.restoration,
            config=PdaEquipmentControlConfig(
                activation_timeout_seconds=self.config.activation_timeout_seconds,
                action_timeout_seconds=self.config.action_timeout_seconds,
                state_timeout_seconds=self.config.state_timeout_seconds,
                restoration_timeout_seconds=(
                    self.config.restoration_timeout_seconds
                ),
                poll_seconds=EQUIPMENT_POLL_SECONDS,
            ),
            record_measurement=self.report["measurements"].append,
            record_observation=(
                self.report["equipment_state_observations"].append
            ),
            record_skip=self.recorder.skip,
            progress=lambda message: print(message, flush=True),
        )

    def equipment_actions(self, context: ScenarioContext) -> EquipmentActions:
        return self.equipment_control(context).actions()

    async def wait_for_stable(
        self,
        context: ScenarioContext,
        identifiers: Sequence[str],
        *,
        phase: str,
        timeout_seconds: float,
        initial_snapshot: EquipmentSnapshot | None = None,
    ) -> EquipmentSnapshot:
        try:
            return await self.equipment_control(context).wait_for_stable(
                identifiers,
                phase=phase,
                timeout_seconds=timeout_seconds,
                initial_snapshot=initial_snapshot,
            )
        except PdaEquipmentControlFailure as error:
            raise PdaRunSessionFailure(str(error)) from error

    async def set_device(
        self,
        context: ScenarioContext,
        identifier: str,
        enabled: bool,
        *,
        phase: str,
        state_timeout_seconds: float | None = None,
    ) -> None:
        try:
            await self.equipment_control(context).set_device(
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
            raise PdaRunSessionFailure(str(error)) from error

    async def set_setpoint(
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
            await self.equipment_control(context).set_setpoint(
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
            raise PdaRunSessionFailure(str(error)) from error

    async def wait_for_device_state(
        self,
        context: ScenarioContext,
        identifier: str,
        enabled: bool,
        *,
        timeout_seconds: float | None = None,
    ) -> int:
        timeout = timeout_seconds or self.config.state_timeout_seconds
        try:
            return await self.equipment_control(context).wait_for_device_state(
                identifier,
                enabled,
                timeout_seconds=timeout,
            )
        except PdaEquipmentControlFailure as error:
            raise PdaRunSessionFailure(str(error)) from error

    def initial_device_enabled(self, identifier: str) -> bool:
        return self.restoration.initial_device_enabled(identifier)

    async def restore(self, context: ScenarioContext) -> list[str]:
        if self.initial_snapshot is None:
            return []
        self.run_report.begin_restoration()
        result = await PdaRestorationCoordinator(
            api=self.api_client,
            session=self.restoration,
            control=self.equipment_control(context),
            config=PdaRestorationCoordinatorConfig(
                timeout_seconds=self.config.restoration_timeout_seconds,
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
        ).restore(self.initial_snapshot)
        self.run_report.record_restoration(result)
        return list(result.errors)

    async def restore_with_progress(
        self,
        context: ScenarioContext,
        name: str,
    ) -> list[str]:
        started = self.recorder.progress_started(name)
        errors = await self.restore(context)
        self.recorder.progress_finished(
            name,
            started,
            passed=not errors,
            detail="; ".join(errors) if errors else None,
        )
        return errors

    @staticmethod
    def require_device(
        snapshot: EquipmentSnapshot,
        identifier: str,
    ) -> DeviceState:
        try:
            return snapshot.devices[identifier]
        except KeyError as error:
            raise PdaRunSessionFailure(
                f"Required device {identifier} is absent from /api/devices"
            ) from error
