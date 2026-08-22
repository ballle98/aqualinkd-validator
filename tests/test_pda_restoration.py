from __future__ import annotations

import unittest
from typing import Any

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.engine import ProgrammerMarkers, RestorationSession
from aqualinkd_validator.protocols.pda import (
    PdaRestorationConfig,
    PdaRestorationService,
)
from aqualinkd_validator.protocols.pda.restoration_coordinator import (
    PdaRestorationCoordinator,
    PdaRestorationCoordinatorConfig,
)


def snapshot(*, status: int, state: str = "off") -> EquipmentSnapshot:
    return EquipmentSnapshot(
        temp_units="f",
        devices={
            "Filter_Pump": {
                "id": "Filter_Pump",
                "name": "Filter Pump",
                "type": "switch",
                "int_status": status,
                "state": state,
                "status": state,
            }
        },
    )


class RestorationApi:
    base_url = "http://127.0.0.1:8080"

    def __init__(self, current: EquipmentSnapshot) -> None:
        self.current = current

    async def devices(self) -> EquipmentSnapshot:
        return self.current

    async def status(self) -> dict[str, Any]:
        return {}

    async def set_device(self, identifier: str, enabled: bool) -> None:
        raise AssertionError("service uses injected actions")

    async def set_setpoint(self, identifier: str, value: int) -> None:
        raise AssertionError("service uses injected actions")


class PdaRestorationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_existing_long_transition_without_retoggle(self) -> None:
        initial = snapshot(status=0)
        session = RestorationSession()
        session.capture_initial(initial)
        session.touch_device("Filter_Pump")
        api = RestorationApi(snapshot(status=2, state="*** off ***"))
        set_calls: list[tuple[str, bool, str, float]] = []
        stable_calls: list[tuple[tuple[str, ...], str, float]] = []

        async def wait_for_stable(
            identifiers: Any,
            phase: str,
            timeout: float,
            current: EquipmentSnapshot,
        ) -> EquipmentSnapshot:
            del current
            stable_calls.append((tuple(identifiers), phase, timeout))
            return initial

        result = await self._service(
            api,
            session,
            set_calls=set_calls,
            wait_for_stable=wait_for_stable,
        ).restore(initial)

        self.assertTrue(result.passed)
        self.assertEqual(set_calls, [])
        self.assertEqual(
            stable_calls,
            [
                (
                    ("Filter_Pump",),
                    "restoration.Filter_Pump.pending_transition",
                    12.0,
                )
            ],
        )

    async def test_retry_waits_for_prior_request_without_duplicate_toggle(
        self,
    ) -> None:
        initial = snapshot(status=0)
        session = RestorationSession()
        session.capture_initial(initial)
        session.touch_device("Filter_Pump")
        session.mark_requested_state("Filter_Pump", False)
        api = RestorationApi(snapshot(status=1, state="on"))
        set_calls: list[tuple[str, bool, str, float]] = []
        state_waits: list[tuple[str, bool, float]] = []

        async def wait_for_state(
            identifier: str,
            enabled: bool,
            timeout: float,
        ) -> None:
            state_waits.append((identifier, enabled, timeout))

        result = await self._service(
            api,
            session,
            set_calls=set_calls,
            wait_for_device_state=wait_for_state,
        ).restore(initial)

        self.assertTrue(result.passed)
        self.assertEqual(set_calls, [])
        self.assertEqual(state_waits, [("Filter_Pump", False, 12.0)])

    def _service(
        self,
        api: RestorationApi,
        session: RestorationSession,
        *,
        set_calls: list[tuple[str, bool, str, float]],
        wait_for_stable: Any | None = None,
        wait_for_device_state: Any | None = None,
    ) -> PdaRestorationService:
        async def set_device(
            identifier: str,
            enabled: bool,
            phase: str,
            timeout: float,
        ) -> None:
            set_calls.append((identifier, enabled, phase, timeout))

        async def unexpected_setpoint(identifier: str, value: int) -> None:
            raise AssertionError(f"unexpected setpoint: {identifier}={value}")

        async def unexpected_stable(*args: Any) -> EquipmentSnapshot:
            raise AssertionError(f"unexpected stable wait: {args}")

        async def unexpected_state(*args: Any) -> None:
            raise AssertionError(f"unexpected state wait: {args}")

        return PdaRestorationService(
            api=api,
            session=session,
            config=PdaRestorationConfig(timeout_seconds=12.0),
            set_device=set_device,
            set_setpoint=unexpected_setpoint,
            wait_for_stable=wait_for_stable or unexpected_stable,
            wait_for_device_state=(
                wait_for_device_state or unexpected_state
            ),
            progress=lambda message: None,
        )


class RecordingEquipmentControl:
    def __init__(self, stable: EquipmentSnapshot) -> None:
        self.stable = stable
        self.device_calls: list[tuple[str, bool, str, ProgrammerMarkers, float]] = []

    async def set_device(
        self,
        identifier: str,
        enabled: bool,
        *,
        phase: str,
        markers: ProgrammerMarkers,
        convergence_timeout_seconds: float | None = None,
    ) -> None:
        assert convergence_timeout_seconds is not None
        self.device_calls.append(
            (
                identifier,
                enabled,
                phase,
                markers,
                convergence_timeout_seconds,
            )
        )

    async def set_setpoint(
        self,
        identifier: str,
        value: int,
        *,
        phase: str,
        category: str,
        markers: ProgrammerMarkers,
    ) -> None:
        raise AssertionError(
            f"unexpected setpoint: {identifier}={value} {phase} {category} {markers}"
        )

    async def wait_for_stable(
        self,
        identifiers: Any,
        *,
        phase: str,
        timeout_seconds: float,
        initial_snapshot: EquipmentSnapshot | None = None,
    ) -> EquipmentSnapshot:
        del identifiers, phase, timeout_seconds, initial_snapshot
        return self.stable

    async def wait_for_device_state(
        self,
        identifier: str,
        enabled: bool,
        *,
        timeout_seconds: float,
    ) -> int:
        raise AssertionError(
            f"unexpected state wait: {identifier}={enabled} {timeout_seconds}"
        )


class PdaRestorationCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_binds_device_markers_and_timeout(self) -> None:
        initial = snapshot(status=0)
        session = RestorationSession()
        session.capture_initial(initial)
        session.touch_device("Filter_Pump")
        api = RestorationApi(snapshot(status=1, state="on"))
        control = RecordingEquipmentControl(initial)
        markers = ProgrammerMarkers(
            "Switch PDA device on/off",
            "active",
            "completed",
        )

        result = await PdaRestorationCoordinator(
            api=api,
            session=session,
            control=control,
            config=PdaRestorationCoordinatorConfig(
                timeout_seconds=12,
                device_markers=markers,
                setpoint_markers={},
            ),
            progress=lambda message: None,
        ).restore(initial)

        self.assertTrue(result.passed)
        self.assertEqual(
            control.device_calls,
            [("Filter_Pump", False, "restoration.device", markers, 12)],
        )
