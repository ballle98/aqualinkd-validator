from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.config import ConfigurationError, read_config_value


class ConfigTests(unittest.TestCase):
    def test_reads_last_active_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aqualinkd.conf"
            path.write_text(
                "# serial_port=/dev/ignored\n"
                "serial_port = /dev/ttyUSB0\n"
                'serial_port="/dev/serial/by-id/panel"\n',
                encoding="utf-8",
            )
            self.assertEqual(
                read_config_value(path, "serial_port"),
                "/dev/serial/by-id/panel",
            )

    def test_missing_assignment_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aqualinkd.conf"
            path.write_text("# serial_port=/dev/ignored\n", encoding="utf-8")
            self.assertIsNone(read_config_value(path, "serial_port"))

    def test_configuration_error_is_value_error(self) -> None:
        self.assertTrue(issubclass(ConfigurationError, ValueError))


if __name__ == "__main__":
    unittest.main()

