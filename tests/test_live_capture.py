from __future__ import annotations

import json
import struct
import unittest

from aqualinkd_validator.adapters import LogicalSerialLogCapture
from aqualinkd_validator.interfaces import LineEvent
from aqualinkd_validator.protocols.rs485.log_capture import parse_packet_log_line
from aqualinkd_validator.testing import FakeTimeline, MemoryArtifactStore


class PacketLogParserTests(unittest.TestCase):
    def test_parses_timestamped_jandy_read(self) -> None:
        line = (
            "07:56:52.859 Debug:   RS Serial: Read  Jandy   packet To 0x60 "
            "of type            Probe | HEX: "
            "0x10|0x02|0x60|0x00|0x72|0x10|0x03| "
        )

        result = parse_packet_log_line(line)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.valid)
        assert result.packet is not None
        self.assertEqual(result.packet.direction, "panel_to_aqualinkd")
        self.assertEqual(result.packet.protocol, "jandy")
        self.assertEqual(result.packet.destination, 0x60)
        self.assertEqual(result.packet.packet_type, "Probe")
        self.assertEqual(result.packet.payload, bytes.fromhex("10026000721003"))
        self.assertFalse(result.packet.bad_packet)
        self.assertEqual(result.packet.raw_line, line)

    def test_parses_pentair_write_and_bad_packet(self) -> None:
        line = (
            "Warning: RS Serial: Write Pentair packet BAD PACKET To 0x10 "
            "of type Unknown '0x07' | HEX: 0xff|0x00|0xff|0xa5|"
        )

        result = parse_packet_log_line(line)

        self.assertIsNotNone(result)
        assert result is not None and result.packet is not None
        self.assertEqual(result.packet.direction, "aqualinkd_to_panel")
        self.assertEqual(result.packet.protocol, "pentair")
        self.assertEqual(result.packet.destination, 0x10)
        self.assertEqual(result.packet.packet_type, "Unknown '0x07'")
        self.assertEqual(result.packet.payload, bytes.fromhex("ff00ffa5"))
        self.assertTrue(result.packet.bad_packet)

    def test_preserves_malformed_hex_candidate(self) -> None:
        line = (
            "Debug: RS Serial: Read Jandy packet To 0x60 of type Status "
            "| HEX: 0x10|0xGG|0x03|"
        )

        result = parse_packet_log_line(line)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.valid)
        self.assertIsNone(result.packet)
        self.assertEqual(result.error, "invalid hexadecimal byte token: 0xGG")
        self.assertEqual(result.raw_line, line)

    def test_preserves_structurally_malformed_candidate(self) -> None:
        line = "Debug: RS Serial: Read Jandy packet without fields"

        result = parse_packet_log_line(line)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.valid)
        self.assertIn("expected structure", result.error or "")

    def test_ignores_unrelated_logs(self) -> None:
        self.assertIsNone(
            parse_packet_log_line("Debug: PDA: Received PDA packet type 0x02")
        )


class LogicalSerialLogCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_valid_and_malformed_candidates_to_artifacts(self) -> None:
        artifacts = MemoryArtifactStore()
        timeline = FakeTimeline()
        capture = LogicalSerialLogCapture(
            artifacts=artifacts,
            timeline=timeline,
            wall_start_ns=1_000_000_000,
        )
        await capture.observe(
            LineEvent(
                1,
                10,
                "stdout",
                "Debug: RS Serial: Read Jandy packet To 0x60 of type Probe "
                "| HEX: 0x10|0x02|0x60|0x00|0x72|0x10|0x03|",
            )
        )
        await capture.observe(
            LineEvent(
                2,
                20,
                "stderr",
                "Warning: RS Serial: Write Pentair packet BAD PACKET To 0x10 "
                "of type Unknown | HEX: 0xff|0x00|0xa5|",
            )
        )
        await capture.observe(
            LineEvent(
                3,
                30,
                "stdout",
                "Debug: RS Serial: Read Jandy packet To 0x60 of type Status "
                "| HEX: 0x10|invalid|0x03|",
            )
        )
        await capture.observe(LineEvent(4, 40, "stdout", "unrelated"))
        await capture.close()
        await capture.close()

        records = [
            json.loads(line) for line in artifacts.values["serial.jsonl"].splitlines()
        ]
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["offset_ns"], 10)
        self.assertEqual(records[0]["direction"], "panel_to_aqualinkd")
        self.assertEqual(records[0]["data"], "10026000721003")
        self.assertTrue(records[1]["bad_packet"])
        self.assertFalse(records[2]["valid"])
        self.assertIn("invalid hexadecimal", records[2]["error"])

        manifest = artifacts.json("serial-capture.json")
        self.assertEqual(manifest["counts"]["candidates"], 3)
        self.assertEqual(manifest["counts"]["packets"], 2)
        self.assertEqual(manifest["counts"]["bad_packets"], 1)
        self.assertEqual(manifest["dropped_or_unparsed_records"], 1)
        packets = self._pcap_packets(artifacts.binary_values["serial.pcapng"])
        self.assertEqual(len(packets), 2)
        first_header = struct.unpack("<4sBBBBI", packets[0][1][:12])
        self.assertEqual(first_header, (b"AQV1", 1, 1, 2, 0x07, 7))
        self.assertEqual(packets[0][0], 1_000_000_010)
        self.assertEqual(packets[0][1][12:], bytes.fromhex("10026000721003"))
        self.assertEqual(
            [event["kind"] for event in timeline.events],
            ["serial_frame", "serial_frame", "serial_packet_log_unparsed"],
        )

    @staticmethod
    def _pcap_packets(data: bytes) -> list[tuple[int, bytes]]:
        packets: list[tuple[int, bytes]] = []
        offset = 0
        while offset < len(data):
            block_type, block_length = struct.unpack_from("<II", data, offset)
            if block_type == 6:
                high, low, captured_length = struct.unpack_from(
                    "<III", data, offset + 12
                )
                packet_start = offset + 28
                packets.append(
                    (
                        (high << 32) | low,
                        data[packet_start : packet_start + captured_length],
                    )
                )
            offset += block_length
        return packets


if __name__ == "__main__":
    unittest.main()
