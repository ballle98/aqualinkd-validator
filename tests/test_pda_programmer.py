from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.protocols.pda import (
    PdaProgrammerFailure,
    PdaProgrammerObserver,
)
from aqualinkd_validator.supervisor import OutputMonitor, Timeline


class PdaProgrammerObserverTests(unittest.TestCase):
    def test_activation_and_completion_use_ordered_events(self) -> None:
        asyncio.run(self._observe_activation_and_completion())

    def test_activation_timeout_has_task_context(self) -> None:
        asyncio.run(self._observe_activation_timeout())

    def test_programmer_error_wins_state_race(self) -> None:
        asyncio.run(self._observe_programmer_error())

    def test_converged_state_cancels_error_wait(self) -> None:
        asyncio.run(self._observe_converged_state())

    async def _observe_activation_and_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timeline.jsonl"
            timeline = Timeline(path, 0)
            events = OutputMonitor()
            observer = PdaProgrammerObserver()
            await events.publish(100, "stdout", "is active (Switch PDA device)")
            await events.publish(250, "stdout", "(Switch PDA device) finished")
            try:
                active = await observer.wait_for_active(
                    events,
                    timeline,
                    task_name="Switch PDA device on/off",
                    marker="is active (Switch PDA device)",
                    after=0,
                    requested_offset_ns=50,
                    timeout_seconds=0.1,
                )
                completed = await observer.wait_for_completion(
                    events,
                    timeline,
                    task_name="Switch PDA device on/off",
                    marker="(Switch PDA device) finished",
                    active=active,
                    timeout_seconds=0.1,
                )
            finally:
                timeline.close()

            self.assertEqual(active.offset_ns, 100)
            self.assertEqual(completed.offset_ns, 250)
            kinds = [
                json.loads(line)["kind"]
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                kinds,
                ["scenario_programmer_active", "scenario_programmer_finished"],
            )

    async def _observe_activation_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            timeline = Timeline(Path(directory) / "timeline.jsonl", 0)
            try:
                with self.assertRaisesRegex(
                    PdaProgrammerFailure,
                    "Init PDA did not become active",
                ):
                    await PdaProgrammerObserver().wait_for_active(
                        OutputMonitor(),
                        timeline,
                        task_name="Init PDA",
                        marker="missing",
                        after=0,
                        requested_offset_ns=0,
                        timeout_seconds=0.01,
                    )
            finally:
                timeline.close()

    async def _observe_programmer_error(self) -> None:
        events = OutputMonitor()
        await events.publish(
            100,
            "stderr",
            "PDA Device programmer 'Switch PDA device on/off' didn't find item",
        )

        async def state_wait() -> int:
            await asyncio.sleep(1)
            return 200

        with self.assertRaisesRegex(PdaProgrammerFailure, "didn't find item"):
            await PdaProgrammerObserver().wait_for_state_or_error(
                events,
                task_name="Switch PDA device on/off",
                after=0,
                state_wait=state_wait(),
                timeout_seconds=0.1,
            )

    async def _observe_converged_state(self) -> None:
        async def state_wait() -> int:
            return 321

        observed = await PdaProgrammerObserver().wait_for_state_or_error(
            OutputMonitor(),
            task_name="Switch PDA device on/off",
            after=0,
            state_wait=state_wait(),
            timeout_seconds=1,
        )
        self.assertEqual(observed, 321)


if __name__ == "__main__":
    unittest.main()
