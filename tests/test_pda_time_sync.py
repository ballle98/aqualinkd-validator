from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from aqualinkd_validator.protocols.pda.time_sync import (
    PdaTimeSyncValidator,
    ProcessTimeController,
    find_faketime_library,
)


class ProcessTimeControllerTests(unittest.TestCase):
    def test_boundary_error_is_signed_for_early_and_late_transitions(self) -> None:
        late = datetime(2026, 1, 1, 10, 5, 0, 250_000, tzinfo=UTC)
        early = datetime(2026, 1, 1, 10, 4, 59, 750_000, tzinfo=UTC)

        self.assertEqual(
            PdaTimeSyncValidator._boundary_error_seconds("10:05 AM", late),
            0.25,
        )
        self.assertEqual(
            PdaTimeSyncValidator._boundary_error_seconds("10:05 AM", early),
            -0.25,
        )

    def test_updates_process_local_offset_and_tracks_panel_restoration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "libfaketime.so.1"
            library.touch()
            controller = ProcessTimeController(7200, library)
            timestamp_file = Path(controller.environment["FAKETIME_TIMESTAMP_FILE"])
            try:
                self.assertEqual(timestamp_file.read_text(encoding="ascii"), "+7200s\n")
                self.assertTrue(controller.panel_requires_restore)
                self.assertEqual(
                    controller.environment["FAKETIME_DONT_FAKE_MONOTONIC"],
                    "1",
                )

                controller.set_offset(0)
                self.assertEqual(timestamp_file.read_text(encoding="ascii"), "+0s\n")
                self.assertTrue(controller.panel_requires_restore)

                controller.mark_panel_restored()
                self.assertFalse(controller.panel_requires_restore)
            finally:
                controller.close()
            self.assertFalse(timestamp_file.exists())

    def test_finds_explicit_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "libfaketime.so.1"
            library.touch()
            with mock.patch.dict(
                "os.environ",
                {"LIBFAKETIME_PATH": str(library)},
            ):
                self.assertEqual(find_faketime_library(), library.resolve())


if __name__ == "__main__":
    unittest.main()
