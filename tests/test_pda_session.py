from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.protocols.pda import (
    PdaProgrammerObserver,
    PdaSessionInitializer,
)
from aqualinkd_validator.protocols.pda.session import INIT_ACTIVE, INIT_FINISHED
from aqualinkd_validator.supervisor import OutputMonitor, Timeline


class PdaSessionInitializerTests(unittest.TestCase):
    def test_startup_correlates_identity_screen_and_endpoint(self) -> None:
        asyncio.run(self._initialize_session())

    def test_current_web_server_url_log_is_supported(self) -> None:
        asyncio.run(self._discover_current_web_server_url())

    async def _discover_current_web_server_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            timeline = Timeline(Path(directory) / "timeline.jsonl", 0)
            events = OutputMonitor()
            await events.publish(
                100,
                "stdout",
                "Notice: NetService:Starting web server on http://0.0.0.0:8080",
            )
            initializer = PdaSessionInitializer(
                events=events,
                timeline=timeline,
                programmer=PdaProgrammerObserver(),
                timeout_seconds=0.1,
            )
            try:
                discovered = await initializer._discover_api_base_url()
            finally:
                timeline.close()
            self.assertEqual(discovered, "http://127.0.0.1:8080")

    async def _initialize_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timeline.jsonl"
            timeline = Timeline(path, 0)
            events = OutputMonitor()
            lines = (
                "NetService:Starting web server on port 8080",
                "AqualinkD: Starting Aqualink Daemon v3.1.1 (Dev2) !",
                "AqualinkD: panel type = PDA-6 Combo (Pool & Spa)",
                INIT_ACTIVE,
                "PDA Menu Line 1 = PDA-PS6 Combo",
                "PDA Menu Line 3 = Firmware Version",
                "PDA Menu Line 5 = PDA: 7.1.0",
                INIT_FINISHED,
            )
            for sequence, line in enumerate(lines, start=1):
                await events.publish(sequence * 100, "stdout", line)

            try:
                result = await PdaSessionInitializer(
                    events=events,
                    timeline=timeline,
                    programmer=PdaProgrammerObserver(),
                    timeout_seconds=0.1,
                ).initialize(discover_api=True)
            finally:
                timeline.close()

            self.assertEqual(
                result.discovered_api_base_url,
                "http://127.0.0.1:8080",
            )
            self.assertEqual(
                result.aqualinkd_identity,
                {
                    "version": "v3.1.1 (Dev2)",
                    "configured_panel_type": "PDA-6 Combo (Pool & Spa)",
                    "source": "aqualinkd_startup_log",
                },
            )
            self.assertEqual(
                result.init_screen,
                {
                    "panel_type": "PDA-PS6 Combo",
                    "firmware": "PDA: 7.1.0",
                    "source": "pda_firmware_version_screen",
                },
            )
            self.assertEqual(result.active.offset_ns, 400)
            self.assertEqual(result.completed.offset_ns, 800)
            timeline_events = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [event["kind"] for event in timeline_events],
                [
                    "scenario_programmer_active",
                    "scenario_programmer_finished",
                    "scenario_phase",
                ],
            )


if __name__ == "__main__":
    unittest.main()
