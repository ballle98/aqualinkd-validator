from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from aqualinkd_validator.correlation import correlate_http_actions_with_serial


class SerialCorrelationTests(unittest.TestCase):
    def test_correlates_http_ack_command_and_panel_response(self) -> None:
        report = self._correlate(
            timeline=(
                self._event(100, "scenario_action_started"),
                self._event(110, "scenario_http_acknowledged"),
                self._event(200, "scenario_action_finished"),
            ),
            serial=(
                self._packet(105, "panel_to_aqualinkd", "Status", "10026002"),
                self._packet(
                    120,
                    "aqualinkd_to_panel",
                    "Ack w/ Command",
                    "100200014002551003",
                ),
                self._packet(130, "panel_to_aqualinkd", "Clear", "10026004"),
            ),
        )

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["passed_count"], 1)
        action = report["actions"][0]
        self.assertEqual(action["outbound"]["command_byte"], 2)
        self.assertEqual(action["inbound"]["packet_type"], "Clear")

    def test_reports_missing_command_and_response(self) -> None:
        report = self._correlate(
            timeline=(
                self._event(100, "scenario_action_started"),
                self._event(110, "scenario_http_acknowledged"),
                self._event(200, "scenario_action_finished"),
            ),
            serial=(
                self._packet(
                    120,
                    "aqualinkd_to_panel",
                    "Ack",
                    "100200014000531003",
                ),
            ),
        )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failed_count"], 1)
        self.assertIn("no outbound PDA", report["actions"][0]["errors"][0])

    def test_no_actions_is_not_applicable(self) -> None:
        report = self._correlate(timeline=(), serial=())
        self.assertEqual(report["status"], "not_applicable")

    def _correlate(
        self,
        *,
        timeline: tuple[dict[str, Any], ...],
        serial: tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timeline_path = root / "timeline.jsonl"
            serial_path = root / "serial.jsonl"
            self._write_jsonl(timeline_path, timeline)
            self._write_jsonl(serial_path, serial)
            return correlate_http_actions_with_serial(timeline_path, serial_path)

    @staticmethod
    def _event(offset: int, kind: str) -> dict[str, Any]:
        return {
            "offset_ns": offset,
            "kind": kind,
            "phase": "devices.fast.Filter_Pump.on",
            "action": "set_device",
            "target": "Filter_Pump",
            "value": True,
        }

    @staticmethod
    def _packet(
        offset: int,
        direction: str,
        packet_type: str,
        data: str,
    ) -> dict[str, Any]:
        return {
            "offset_ns": offset,
            "valid": True,
            "direction": direction,
            "protocol": "jandy",
            "packet_type": packet_type,
            "data": data,
        }

    @staticmethod
    def _write_jsonl(path: Path, records: tuple[dict[str, Any], ...]) -> None:
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
