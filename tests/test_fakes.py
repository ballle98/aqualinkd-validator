from __future__ import annotations

import unittest

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.testing import (
    FakeAqualinkApi,
    FakeAquaPdaClient,
    FakeClock,
    FakeOrderedLogEvents,
    FakeProcessRunner,
    FakeSerialTransport,
    FakeTimeline,
    MemoryArtifactStore,
)


class FakeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_events_timeline_and_artifacts_are_deterministic(self) -> None:
        snapshot = EquipmentSnapshot(
            temp_units="f",
            devices={
                "Filter_Pump": {
                    "id": "Filter_Pump",
                    "name": "Filter Pump",
                    "type": "switch",
                    "int_status": 0,
                }
            },
        )
        api = FakeAqualinkApi(snapshot)
        await api.set_device("Filter_Pump", True)
        self.assertEqual(api.device_calls, [("Filter_Pump", True)])

        events = FakeOrderedLogEvents()
        cursor = events.cursor
        await events.publish(12, "stdout", "PDA init complete")
        event = await events.wait_for(
            "init complete",
            after=cursor,
            timeout_seconds=0.1,
        )
        self.assertEqual(event.offset_ns, 12)

        clock = FakeClock(nanoseconds=10)
        timeline = FakeTimeline(clock)
        clock.advance(0.001)
        await timeline.write("observed", value=1)
        self.assertEqual(timeline.events[0]["offset_ns"], 1_000_010)

        artifacts = MemoryArtifactStore()
        artifacts.write_json("result.json", {"passed": True})
        self.assertEqual(artifacts.json("result.json"), {"passed": True})

    async def test_serial_aquapda_and_process_fakes_capture_requests(self) -> None:
        serial = FakeSerialTransport()
        await serial.open()
        await serial.incoming.put(b"panel")
        self.assertEqual(await serial.read(), b"panel")
        await serial.write(b"key")
        self.assertEqual(serial.outgoing, [b"key"])

        aquapda = FakeAquaPdaClient()
        await aquapda.connect()
        await aquapda.send_key("up")
        self.assertEqual(aquapda.keys, ["up"])
        self.assertEqual(await aquapda.wait_for_packets(2), 2)

        process = FakeProcessRunner()
        result = await process.run(
            ["aqualinkd", "-d"],
            MemoryArtifactStore().root,
            cwd=None,
            duration_seconds=None,
            sample_interval_seconds=1,
            terminate_grace_seconds=1,
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(process.commands, [["aqualinkd", "-d"]])
