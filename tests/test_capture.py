from __future__ import annotations

import struct
import unittest

from aqualinkd_validator.capture import CapturedSerialTransport, JandyFrameBuffer
from aqualinkd_validator.testing import (
    FakeClock,
    FakeSerialTransport,
    FakeTimeline,
    MemoryArtifactStore,
)


class SerialCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_bidirectional_frames_to_timeline_and_pcapng(self) -> None:
        panel_frame = bytes.fromhex("10026000521003")
        daemon_frame = bytes.fromhex("100200010000131003")
        clock = FakeClock()
        timeline = FakeTimeline(clock)
        artifacts = MemoryArtifactStore()
        serial = FakeSerialTransport()
        captured = CapturedSerialTransport(
            serial,
            timeline=timeline,
            artifacts=artifacts,
            wall_start_ns=1_000_000_000,
        )
        await captured.open()

        clock.advance(0.001)
        await captured.write(panel_frame)
        await serial.incoming.put(daemon_frame[:4])
        await serial.incoming.put(daemon_frame[4:])
        clock.advance(0.002)
        self.assertEqual(await captured.read(), daemon_frame[:4])
        clock.advance(0.003)
        self.assertEqual(await captured.read(), daemon_frame[4:])
        await captured.close()

        serial_events = [
            event
            for event in timeline.events
            if event["kind"] in {"serial_bytes", "serial_frame"}
        ]
        self.assertEqual(
            [event["kind"] for event in serial_events],
            [
                "serial_bytes",
                "serial_frame",
                "serial_bytes",
                "serial_bytes",
                "serial_frame",
            ],
        )
        frame_events = [
            event for event in serial_events if event["kind"] == "serial_frame"
        ]
        self.assertEqual(
            [event["direction"] for event in frame_events],
            ["panel_to_aqualinkd", "aqualinkd_to_panel"],
        )
        self.assertEqual(
            [event["bytes_hex"] for event in frame_events],
            [panel_frame.hex(), daemon_frame.hex()],
        )

        packets = self._pcap_packets(artifacts.binary_values["serial.pcapng"])
        self.assertEqual(len(packets), 2)
        first_header = struct.unpack("<4sBBBBI", packets[0][1][:12])
        second_header = struct.unpack("<4sBBBBI", packets[1][1][:12])
        self.assertEqual(first_header, (b"AQV1", 1, 1, 1, 0x0F, len(panel_frame)))
        self.assertEqual(second_header, (b"AQV1", 1, 2, 1, 0x0F, len(daemon_frame)))
        self.assertEqual(packets[0][1][12:], panel_frame)
        self.assertEqual(packets[1][1][12:], daemon_frame)
        self.assertEqual(packets[0][0], 1_001_000_000)
        self.assertEqual(packets[1][0], 1_006_000_000)
        self.assertEqual(
            artifacts.json("serial-capture.json")["replay_clock"],
            "timeline.offset_ns",
        )

    def test_frame_buffer_handles_noise_fragmentation_and_escaped_dle(self) -> None:
        framer = JandyFrameBuffer()
        self.assertEqual(framer.feed(b"noise\x10"), ())
        self.assertEqual(framer.feed(b"\x02\x60\x10\x10"), ())
        self.assertEqual(
            framer.feed(b"\x01\x10\x03"),
            (b"\x10\x02\x60\x10\x10\x01\x10\x03",),
        )
        self.assertEqual(framer.flush(), b"")

    @staticmethod
    def _pcap_packets(data: bytes) -> list[tuple[int, bytes]]:
        packets: list[tuple[int, bytes]] = []
        offset = 0
        block_types: list[int] = []
        while offset < len(data):
            block_type, block_length = struct.unpack_from("<II", data, offset)
            block_types.append(block_type)
            trailing_length = struct.unpack_from(
                "<I", data, offset + block_length - 4
            )[0]
            if trailing_length != block_length:
                raise AssertionError("PCAPNG block lengths do not match")
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
        if block_types[:2] != [0x0A0D0D0A, 1]:
            raise AssertionError("PCAPNG headers are missing")
        if offset != len(data):
            raise AssertionError("PCAPNG block length exceeded the file")
        return packets


if __name__ == "__main__":
    unittest.main()
