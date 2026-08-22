from __future__ import annotations

import unittest

from aqualinkd_validator.engine import (
    SerialActionFailure,
    SerialActions,
    parse_hex_bytes,
)
from aqualinkd_validator.testing import FakeSerialTransport, FakeTimeline


class SerialActionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_and_fragmented_expectation_preserve_extra_bytes(self) -> None:
        transport = FakeSerialTransport()
        timeline = FakeTimeline()
        actions = SerialActions(transport, timeline=timeline)
        await actions.open()
        await transport.incoming.put(bytes.fromhex("100200"))
        await transport.incoming.put(bytes.fromhex("0100031003aabb"))

        await actions.send(bytes.fromhex("100260001003"), timeout_seconds=0.1)
        observed = await actions.expect_exact(
            bytes.fromhex("1002000100031003"),
            timeout_seconds=0.1,
        )
        await actions.expect_exact(bytes.fromhex("aabb"), timeout_seconds=0.1)
        await actions.close()

        self.assertEqual(observed, bytes.fromhex("1002000100031003"))
        self.assertEqual(transport.outgoing, [bytes.fromhex("100260001003")])
        self.assertEqual(
            [event["kind"] for event in timeline.events],
            [
                "serial_send_requested",
                "serial_send_completed",
                "serial_expect_requested",
                "serial_expect_matched",
                "serial_expect_requested",
                "serial_expect_matched",
            ],
        )

    async def test_mismatch_reports_expected_and_observed_bytes(self) -> None:
        transport = FakeSerialTransport()
        actions = SerialActions(transport, timeline=FakeTimeline())
        await actions.open()
        await transport.incoming.put(bytes.fromhex("100201"))
        with self.assertRaisesRegex(
            SerialActionFailure,
            "expected 100200, observed 100201",
        ):
            await actions.expect_exact(
                bytes.fromhex("100200"),
                timeout_seconds=0.1,
            )
        await actions.close()

    async def test_timeout_reports_partial_bytes(self) -> None:
        transport = FakeSerialTransport()
        actions = SerialActions(transport, timeline=FakeTimeline())
        await actions.open()
        await transport.incoming.put(bytes.fromhex("1002"))
        with self.assertRaisesRegex(
            SerialActionFailure,
            r"timed out after 0.01s: expected 100200, observed 1002",
        ):
            await actions.expect_exact(
                bytes.fromhex("100200"),
                timeout_seconds=0.01,
            )
        await actions.close()

    def test_hex_parser_accepts_readable_forms_and_rejects_bad_tokens(self) -> None:
        expected = bytes.fromhex("100260001003")
        self.assertEqual(parse_hex_bytes("10 02 60 00 10 03"), expected)
        self.assertEqual(parse_hex_bytes("0x10|0x02|0x60|00|10|03"), expected)
        self.assertEqual(parse_hex_bytes("100260001003"), expected)
        with self.assertRaisesRegex(ValueError, "invalid serial byte"):
            parse_hex_bytes("10 02 nope")


if __name__ == "__main__":
    unittest.main()
