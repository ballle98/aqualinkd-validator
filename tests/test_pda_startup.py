from __future__ import annotations

import unittest
from collections.abc import Sequence
from datetime import UTC, datetime

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.interfaces import AqualinkApi
from aqualinkd_validator.protocols.pda import (
    PdaProgrammerObserver,
    PdaStartupConfig,
    PdaStartupCoordinator,
)
from aqualinkd_validator.protocols.pda.session import INIT_ACTIVE, INIT_FINISHED
from aqualinkd_validator.testing import (
    FakeAqualinkApi,
    FakeOrderedLogEvents,
    FakeTimeline,
)


class PdaStartupCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_api_and_returns_stable_initialized_panel(self) -> None:
        events = FakeOrderedLogEvents()
        timeline = FakeTimeline()
        for line in (
            "NetService:Starting web server on port 8080",
            "AqualinkD: Starting Aqualink Daemon v3.1.1 (Dev2) !",
            "AqualinkD: panel type = PDA-6 Combo (Pool & Spa)",
            INIT_ACTIVE,
            "PDA Menu Line 1 = PDA-PS6 Combo",
            "PDA Menu Line 3 = Firmware Version",
            "PDA Menu Line 5 = PDA: 7.1.0",
            INIT_FINISHED,
        ):
            await events.publish(100, "stdout", line)

        snapshot = EquipmentSnapshot(
            temp_units="f",
            devices={
                "Filter_Pump": {
                    "id": "Filter_Pump",
                    "type": "switch",
                    "int_status": 0,
                },
                "Temperature_Air": {
                    "id": "Temperature_Air",
                    "type": "temperature",
                    "int_status": 80,
                },
            },
        )
        api = FakeAqualinkApi(
            snapshot,
            status={"time": datetime.now(UTC).strftime("%I:%M%p")},
        )
        configured: list[tuple[str, str]] = []
        sessions: list[object] = []
        stabilized: list[tuple[str, ...]] = []

        async def stabilize(
            selected_api: AqualinkApi,
            identifiers: Sequence[str],
            initial: EquipmentSnapshot,
        ) -> EquipmentSnapshot:
            self.assertIs(selected_api, api)
            stabilized.append(identifiers)
            return initial

        result = await PdaStartupCoordinator(
            events=events,
            timeline=timeline,
            programmer=PdaProgrammerObserver(),
            api_factory=lambda base_url: api,
            config=PdaStartupConfig(
                init_timeout_seconds=0.1,
                api_timeout_seconds=0.1,
                panel_timezone="UTC",
                panel_time_tolerance_seconds=120,
            ),
            progress=lambda message: None,
            retryable_api_errors=(OSError,),
        ).initialize(
            api=None,
            api_base_url_override=None,
            api_configured=lambda selected_api, source: configured.append(
                (selected_api.base_url, source)
            ),
            session_observed=sessions.append,
            stabilize=stabilize,
        )

        self.assertIs(result.api, api)
        self.assertEqual(result.api_endpoint_source, "aqualinkd_startup_log")
        self.assertEqual(
            configured,
            [("http://127.0.0.1:8080", "aqualinkd_startup_log")],
        )
        self.assertEqual(stabilized, [("Filter_Pump",)])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(result.panel_identity.reported_panel_size, 6)
        self.assertEqual(
            [event["kind"] for event in timeline.events],
            [
                "scenario_programmer_active",
                "scenario_programmer_finished",
                "scenario_phase",
                "api_endpoint_discovered",
            ],
        )


if __name__ == "__main__":
    unittest.main()
