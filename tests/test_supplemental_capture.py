from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.adapters.supplemental_capture import (
    SupplementalLogSpec,
    SupplementalSerialLogTracker,
)
from aqualinkd_validator.testing import MemoryArtifactStore


class SupplementalSerialLogTrackerTests(unittest.TestCase):
    def test_captures_only_file_changed_during_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            changed = root / "RS485.log"
            unchanged = root / "RS485raw.log"
            changed.write_bytes(b"stale")
            unchanged.write_bytes(b"same")
            artifacts = MemoryArtifactStore()
            tracker = SupplementalSerialLogTracker(
                (
                    self._spec("packet", changed, "RS485.log"),
                    self._spec("raw", unchanged, "RS485raw.log"),
                ),
                artifacts=artifacts,
            )

            changed.write_bytes(b"current run packet data")
            report = tracker.snapshot()

            packet, raw = report["files"]
            self.assertEqual(packet["status"], "captured")
            self.assertEqual(packet["artifact"], "RS485.log")
            self.assertEqual(packet["byte_count"], 23)
            self.assertEqual(
                packet["sha256"],
                hashlib.sha256(b"current run packet data").hexdigest(),
            )
            self.assertEqual(
                artifacts.binary_values["RS485.log"],
                b"current run packet data",
            )
            self.assertEqual(raw["status"], "unchanged_not_captured")
            self.assertNotIn("RS485raw.log", artifacts.binary_values)

    def test_reports_requested_file_that_was_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifacts = MemoryArtifactStore()
            tracker = SupplementalSerialLogTracker(
                (
                    self._spec(
                        "packet",
                        Path(directory) / "missing.log",
                        "RS485.log",
                    ),
                ),
                artifacts=artifacts,
            )

            report = tracker.snapshot()

            self.assertTrue(report["requested"])
            self.assertEqual(report["files"][0]["status"], "missing")
            self.assertFalse(artifacts.binary_values)

    @staticmethod
    def _spec(name: str, source: Path, artifact: str) -> SupplementalLogSpec:
        return SupplementalLogSpec(
            name=name,
            source=source,
            artifact=artifact,
            fidelity="test",
            limitations=("test limitation",),
        )


if __name__ == "__main__":
    unittest.main()
