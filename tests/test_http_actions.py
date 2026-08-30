from __future__ import annotations

import json
import unittest

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.engine.http_actions import HttpActionFailure, HttpActions
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

    async def test_polls_json_pointer_until_value_matches(self) -> None:
        transport = _ResponseSequence(
            ['{"leds":{"Filter_Pump":"off"}}', '{"leds":{"Filter_Pump":"on"}}']
        )
        timeline = FakeTimeline()
        artifacts = MemoryArtifactStore()
        actions = HttpActions(transport, timeline=timeline, artifacts=artifacts)

        await actions.wait_json(
            "/api/status",
            "/leds/Filter_Pump",
            "on",
            timeout_seconds=1,
            poll_seconds=0.001,
            request_timeout_seconds=0.1,
        )
        actions.close()

        history = [
            json.loads(line) for line in artifacts.values["http.jsonl"].splitlines()
        ]
        self.assertEqual(len(history), 2)
        self.assertEqual([item["purpose"] for item in history], ["json_poll"] * 2)
        self.assertEqual(timeline.events[-1]["kind"], "http_json_matched")

    async def test_poll_timeout_preserves_last_response_and_history(self) -> None:
        response = '{"leds":{"Filter_Pump":"off"}}'
        transport = _ResponseSequence([response])
        timeline = FakeTimeline()
        artifacts = MemoryArtifactStore()
        actions = HttpActions(transport, timeline=timeline, artifacts=artifacts)

        with self.assertRaisesRegex(
            HttpActionFailure,
            "last value was 'off'",
        ):
            await actions.wait_json(
                "/api/status",
                "/leds/Filter_Pump",
                "on",
                timeout_seconds=0.02,
                poll_seconds=0.001,
                request_timeout_seconds=0.01,
            )
        actions.close()

        failure = artifacts.json("http-poll-failure.json")
        self.assertEqual(failure["last_response"], response)
        self.assertEqual(failure["last_value"], "off")
        self.assertGreaterEqual(failure["attempts"], 1)
        self.assertEqual(failure["request_history"], "http.jsonl")
        self.assertTrue(artifacts.values["http.jsonl"].strip())
        self.assertEqual(timeline.events[-1]["kind"], "http_json_poll_failed")


class _ResponseSequence(FakeAqualinkApi):
    def __init__(self, responses: list[str]) -> None:
        super().__init__(EquipmentSnapshot(temp_units="F", devices={}))
        self._responses = responses
        self._index = 0

    async def request(self, method, path, *, value=None, timeout_seconds=None):
        del timeout_seconds
        self.http_calls.append((method, path, value))
        response = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return response


if __name__ == "__main__":
    unittest.main()
