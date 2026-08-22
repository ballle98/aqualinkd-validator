from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from aqualinkd_validator.adapters import OutputMonitor, Timeline
from aqualinkd_validator.protocols.pda import (
    PdaProgrammerObserver,
    PdaSleepWakeConfig,
    PdaSleepWakeFailure,
    PdaSleepWakeService,
)
from aqualinkd_validator.protocols.pda.sleep import (
    PDA_ADDRESS_PROBE,
    PDA_ADDRESS_STATUS,
    PDA_SLEEPING,
    WAKE_INIT_ACTIVE,
    WAKE_INIT_FINISHED,
)


class PdaSleepWakeServiceTests(unittest.TestCase):
    def test_natural_cycle_records_duty_cycle_and_measurements(self) -> None:
        asyncio.run(self._observe_natural_cycle())

    def test_status_retry_window_rejects_probe_transition(self) -> None:
        asyncio.run(self._reject_probe_during_status_retry())

    def test_probe_window_reports_transition_delay(self) -> None:
        asyncio.run(self._observe_probe_window())

    async def _observe_natural_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events = OutputMonitor()
            timeline = Timeline(Path(directory) / "timeline.jsonl", 0)
            measurements: list[dict[str, Any]] = []
            service = self._service(events, timeline, measurements)
            task = asyncio.create_task(service.observe_natural_cycle())
            await asyncio.sleep(0)
            await events.publish(1_000_000_000, "stdout", PDA_SLEEPING)
            await events.publish(11_000_000_000, "stdout", WAKE_INIT_ACTIVE)
            await events.publish(16_000_000_000, "stdout", WAKE_INIT_FINISHED)
            await events.publish(20_000_000_000, "stdout", PDA_SLEEPING)
            try:
                result = await task
            finally:
                timeline.close()

            self.assertEqual(
                result.report,
                {
                    "sleep_ms": 10000.0,
                    "status_refresh_ms": 5000.0,
                    "return_to_sleep_ms": 4000.0,
                    "awake_ms": 9000.0,
                    "cycle_ms": 19000.0,
                    "awake_percent": 47.368,
                    "sleep_percent": 52.632,
                },
            )
            self.assertEqual(
                [measurement["name"] for measurement in measurements],
                [
                    "pda.sleep.enter",
                    "pda.sleep.duration",
                    "pda.after_wake.status_refresh",
                    "pda.after_wake.return_to_sleep",
                    "pda.wake.duration",
                    "pda.sleep_wake.cycle",
                ],
            )

    async def _reject_probe_during_status_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events = OutputMonitor()
            timeline = Timeline(Path(directory) / "timeline.jsonl", 0)
            service = self._service(
                events,
                timeline,
                [],
                status_retry_delay_seconds=0.01,
            )
            task = asyncio.create_task(service.wait_for_status_retry_window())
            await asyncio.sleep(0)
            await events.publish(1_000_000_000, "stdout", PDA_SLEEPING)
            await events.publish(2_000_000_000, "stdout", PDA_ADDRESS_STATUS)
            await events.publish(3_000_000_000, "stdout", PDA_ADDRESS_PROBE)
            try:
                with self.assertRaisesRegex(
                    PdaSleepWakeFailure,
                    "probing began before",
                ):
                    await task
            finally:
                timeline.close()

    async def _observe_probe_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            events = OutputMonitor()
            timeline = Timeline(Path(directory) / "timeline.jsonl", 0)
            service = self._service(events, timeline, [])
            task = asyncio.create_task(service.wait_for_probe_window())
            await asyncio.sleep(0)
            await events.publish(1_000_000_000, "stdout", PDA_SLEEPING)
            await events.publish(3_500_000_000, "stdout", PDA_ADDRESS_PROBE)
            try:
                result = await task
            finally:
                timeline.close()
            self.assertEqual(result.probe_delay_seconds, 2.5)

    @staticmethod
    def _service(
        events: OutputMonitor,
        timeline: Timeline,
        measurements: list[dict[str, Any]],
        *,
        status_retry_delay_seconds: float = 0,
    ) -> PdaSleepWakeService:
        def record(**fields: Any) -> None:
            measurements.append(fields)

        return PdaSleepWakeService(
            events=events,
            timeline=timeline,
            programmer=PdaProgrammerObserver(),
            config=PdaSleepWakeConfig(
                sleep_timeout_seconds=0.1,
                action_timeout_seconds=0.1,
                status_retry_delay_seconds=status_retry_delay_seconds,
                probe_command_min_delay_seconds=0,
            ),
            record_measurement=record,
            progress=lambda message: None,
        )


if __name__ == "__main__":
    unittest.main()
