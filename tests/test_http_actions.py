from __future__ import annotations

import json
import unittest

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.engine.http_actions import HttpActions
from aqualinkd_validator.testing import (
    FakeAqualinkApi,
    FakeTimeline,
    MemoryArtifactStore,
)


class HttpActionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_request_response_and_timeline(self) -> None:
        transport = FakeAqualinkApi(
            EquipmentSnapshot(temp_units="F", devices={}),
            base_url="http://127.0.0.1:1234",
        )
        timeline = FakeTimeline()
        artifacts = MemoryArtifactStore()
        actions = HttpActions(
            transport,
            timeline=timeline,
            artifacts=artifacts,
        )

        response = await actions.request(
            "PUT",
            "/api/Filter_Pump/set",
            value="1",
            timeout_seconds=2,
        )
        actions.close()

        self.assertEqual(response, "{}")
        self.assertEqual(
            transport.http_calls,
            [("PUT", "/api/Filter_Pump/set", "1")],
        )
        history = json.loads(artifacts.values["http.jsonl"].strip())
        self.assertEqual(history["url"], "http://127.0.0.1:1234/api/Filter_Pump/set")
        self.assertIsNone(history["error"])
        self.assertEqual(
            [event["kind"] for event in timeline.events],
            ["http_request", "http_response"],
        )


if __name__ == "__main__":
    unittest.main()
