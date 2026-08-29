from __future__ import annotations

from collections.abc import Awaitable, Callable

from ...domain import EquipmentSnapshot
from ...interfaces import ScenarioContext
from .device_selection import PdaDeviceSelectionFailure
from .equipment_setup import (
    PdaEquipmentSetupConfig,
    PdaEquipmentSetupFailure,
    PdaEquipmentStatusSetup,
)
from .equipment_status import (
    PdaEquipmentStatusFailure,
    PdaEquipmentStatusService,
)
from .run_session import PdaRunSession, PdaRunSessionFailure
from .runtime_config import (
    EQUIPMENT_POLL_SECONDS,
    POOL_HEATER,
    POOL_HEATER_SETPOINT_ACTIVE_MARKERS,
    POOL_HEATER_SETPOINT_FINISHED_MARKERS,
    SPA_HEATER,
    SPA_HEATER_SETPOINT_ACTIVE_MARKERS,
    SPA_HEATER_SETPOINT_FINISHED_MARKERS,
)
from .sleep import (
    PdaSleepWakeConfig,
    PdaSleepWakeFailure,
    PdaSleepWakeService,
    PdaStatusRetryUnavailable,
)
from .spa import PdaSpaExercise, SpaExerciseConfig
from .status_exercise import PdaEquipmentStatusExercise


class PdaLiveExerciseFailure(RuntimeError):
    """Raised when a live-panel PDA exercise fails its bounded validation."""


class PdaLiveExercises:
    """Run the higher-level physical-panel exercises for one PDA session."""

    def __init__(
        self,
        session: PdaRunSession,
        *,
        return_home_for_status: (
            Callable[[ScenarioContext], Awaitable[None]] | None
        ) = None,
    ) -> None:
        self._session = session
        self._return_home_for_status = return_home_for_status

    async def verify_equipment_status(self, context: ScenarioContext) -> None:
        initial = self._require_initial_snapshot()
        prepare_for_status: Callable[[], Awaitable[None]] | None = None
        if self._return_home_for_status is not None:
            return_home = self._return_home_for_status

            async def prepare_for_status() -> None:
                await return_home(context)

        candidates = self._session.device_selector.status_candidates(
            phase="devices.status_menu.setup"
        )
        status_service = PdaEquipmentStatusService(
            events=context.monitor,
            wait_for_stable=lambda identifiers, phase, timeout: (
                self._session.wait_for_stable(
                    context,
                    identifiers,
                    phase=phase,
                    timeout_seconds=timeout,
                )
            ),
            status_timeout_seconds=self._session.config.status_timeout_seconds,
            state_timeout_seconds=self._session.config.state_timeout_seconds,
            progress=lambda message: print(message, flush=True),
        )
        try:
            result = await PdaEquipmentStatusExercise(
                events=context.monitor,
                timeline=context.timeline,
                setup=self._equipment_status_setup(context),
                status=status_service,
                record_skip=self._session.recorder.skip,
                record_measurement=self._session.recorder.append_measurement,
                progress=lambda message: print(message, flush=True),
                prepare_for_status=prepare_for_status,
            ).run(initial_snapshot=initial, candidates=candidates)
        except (PdaEquipmentSetupFailure, PdaRunSessionFailure) as error:
            raise PdaLiveExerciseFailure(str(error)) from error
        except PdaEquipmentStatusFailure as error:
            if error.result is not None:
                self._session.report["equipment_status"] = error.result.report
            raise PdaLiveExerciseFailure(str(error)) from error
        if result.verification is not None:
            self._session.report["equipment_status"] = result.verification.report

    async def exercise_spa_heating(self, context: ScenarioContext) -> None:
        initial = self._require_initial_snapshot()
        fill_seconds = self._session.config.spa_fill_seconds
        if fill_seconds is None:
            raise PdaLiveExerciseFailure(
                "pda-live-spa requires spa.fill_time in "
                "aqualinkd-validator.yaml beside aqualinkd.conf"
            )
        exercise = PdaSpaExercise(
            api=self._session.api_client,
            config=SpaExerciseConfig(
                fill_seconds=fill_seconds,
                active_timeout_seconds=(
                    self._session.config.status_timeout_seconds
                ),
                transition_timeout_seconds=(
                    self._session.config.restoration_timeout_seconds
                ),
            ),
            set_device=lambda identifier, enabled, phase, timeout: (
                self._session.set_device(
                    context,
                    identifier,
                    enabled,
                    phase=phase,
                    state_timeout_seconds=timeout,
                )
            ),
            set_setpoint=lambda identifier, value, phase: (
                self._session.set_setpoint(
                    context,
                    identifier,
                    value,
                    phase=phase,
                    active_marker=SPA_HEATER_SETPOINT_ACTIVE_MARKERS,
                    completion_marker=SPA_HEATER_SETPOINT_FINISHED_MARKERS,
                    category="spa_heating",
                )
            ),
            record_measurement=self._session.recorder.append_measurement,
            record_skip=self._session.recorder.skip,
            offset_ns=context.timeline.offset_ns,
        )
        try:
            await exercise.run(initial)
        except PdaRunSessionFailure as error:
            raise PdaLiveExerciseFailure(str(error)) from error

    async def exercise_consecutive_devices(
        self,
        context: ScenarioContext,
    ) -> None:
        self._require_initial_snapshot()
        try:
            identifiers = list(
                self._session.device_selector.consecutive_switches(
                    phase="devices.consecutive"
                )
            )
        except PdaDeviceSelectionFailure as error:
            raise PdaLiveExerciseFailure(str(error)) from error
        self._session.report["device_selection"]["resolved"] = identifiers
        try:
            for identifier in identifiers:
                await self._session.set_device(
                    context,
                    identifier,
                    not self._session.initial_device_enabled(identifier),
                    phase="devices.consecutive",
                )
            for identifier in reversed(identifiers):
                await self._session.set_device(
                    context,
                    identifier,
                    self._session.initial_device_enabled(identifier),
                    phase="devices.consecutive.restore",
                )
        except PdaRunSessionFailure as error:
            raise PdaLiveExerciseFailure(str(error)) from error

    async def observe_sleep_cycle(self, context: ScenarioContext) -> None:
        try:
            result = await self._sleep_wake_service(
                context
            ).observe_natural_cycle()
        except PdaSleepWakeFailure as error:
            raise PdaLiveExerciseFailure(str(error)) from error
        self._session.report["sleep_cycle"] = result.report

    async def exercise_status_retry(self, context: ScenarioContext) -> None:
        phase = "devices.sleep.status_retry"
        identifier = self._sleep_test_device(phase=phase)
        if identifier is None:
            return
        service = self._sleep_wake_service(context)
        try:
            window = await service.wait_for_status_retry_window()
        except PdaStatusRetryUnavailable:
            print(
                "[STATE ] Initial sleep followed a non-STATUS packet; "
                f"priming a clean wake/sleep cycle with {identifier}",
                flush=True,
            )
            await self._toggle_round_trip(
                context,
                identifier,
                phase=f"{phase}.prime",
            )
            try:
                window = await service.wait_for_status_retry_window()
            except PdaSleepWakeFailure as error:
                raise PdaLiveExerciseFailure(str(error)) from error
        except PdaSleepWakeFailure as error:
            raise PdaLiveExerciseFailure(str(error)) from error
        print(
            f"[STATE ] Observed {window.retry_count} repeated PDA STATUS "
            f"packet(s); toggling {identifier}",
            flush=True,
        )
        await self._toggle_round_trip(context, identifier, phase=phase)

    async def exercise_probe_transition(self, context: ScenarioContext) -> None:
        phase = "devices.sleep.probing"
        identifier = self._sleep_test_device(phase=phase)
        if identifier is None:
            return
        try:
            window = await self._sleep_wake_service(
                context
            ).wait_for_probe_window()
        except PdaSleepWakeFailure as error:
            raise PdaLiveExerciseFailure(str(error)) from error
        print(
            f"[STATE ] PDA address probe observed "
            f"{window.probe_delay_seconds:.3f}s after sleep began; "
            f"toggling {identifier}",
            flush=True,
        )
        await self._toggle_round_trip(context, identifier, phase=phase)

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
            await self._session.set_setpoint(
                context,
                identifier,
                value,
                phase=phase,
                active_marker=active,
                completion_marker=completed,
                category="heater_safety",
            )

        return PdaEquipmentStatusSetup(
            api=self._session.api_client,
            events=context.monitor,
            config=PdaEquipmentSetupConfig(
                status_timeout_seconds=(
                    self._session.config.status_timeout_seconds
                ),
                restoration_timeout_seconds=(
                    self._session.config.restoration_timeout_seconds
                ),
                poll_seconds=EQUIPMENT_POLL_SECONDS,
            ),
            set_device=lambda identifier, enabled, phase, timeout: (
                self._session.set_device(
                    context,
                    identifier,
                    enabled,
                    phase=phase,
                    state_timeout_seconds=timeout,
                )
            ),
            set_setpoint=set_heater_setpoint,
            wait_for_stable=lambda identifiers, phase, timeout: (
                self._session.wait_for_stable(
                    context,
                    identifiers,
                    phase=phase,
                    timeout_seconds=timeout,
                )
            ),
            record_skip=self._session.recorder.skip,
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
            raise PdaLiveExerciseFailure(
                f"No PDA heater setpoint markers for {identifier}"
            )
        return markers

    def _sleep_test_device(self, *, phase: str) -> str | None:
        try:
            identifier = self._session.device_selector.sleep_switch(phase=phase)
        except PdaDeviceSelectionFailure as error:
            raise PdaLiveExerciseFailure(str(error)) from error
        if identifier is not None:
            self._session.report["device_selection"]["resolved"] = [identifier]
        return identifier

    def _sleep_wake_service(
        self,
        context: ScenarioContext,
    ) -> PdaSleepWakeService:
        return PdaSleepWakeService(
            events=context.monitor,
            timeline=context.timeline,
            programmer=self._session.programmer,
            config=PdaSleepWakeConfig(
                sleep_timeout_seconds=self._session.config.sleep_timeout_seconds,
                action_timeout_seconds=(
                    self._session.config.action_timeout_seconds
                ),
                status_retry_delay_seconds=(
                    self._session.config.status_retry_command_delay_seconds
                ),
                probe_command_min_delay_seconds=(
                    self._session.config.probe_command_min_delay_seconds
                ),
            ),
            record_measurement=self._session.recorder.append_measurement,
            progress=lambda message: print(message, flush=True),
        )

    async def _toggle_round_trip(
        self,
        context: ScenarioContext,
        identifier: str,
        *,
        phase: str,
    ) -> None:
        initial = self._session.initial_device_enabled(identifier)
        try:
            await self._session.set_device(
                context,
                identifier,
                not initial,
                phase=phase,
            )
            await self._session.set_device(
                context,
                identifier,
                initial,
                phase=phase,
            )
        except PdaRunSessionFailure as error:
            raise PdaLiveExerciseFailure(str(error)) from error

    def _require_initial_snapshot(self) -> EquipmentSnapshot:
        snapshot = self._session.initial_snapshot
        if snapshot is None:
            raise PdaLiveExerciseFailure("PDA session has not been initialized")
        return snapshot
