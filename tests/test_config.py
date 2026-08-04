from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.config import (
    ConfigurationError,
    read_config_value,
    read_disabled_button_numbers,
    resolve_api_base_url,
    write_config_with_overrides,
)


class ConfigTests(unittest.TestCase):
    def test_reads_effective_none_button_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aqualinkd.conf"
            path.write_text(
                "# button_01_label=NONE\n"
                "button_02_label=NONE\n"
                "button_03_label=Cleaner\n"
                "button_03_label = 'none'\n"
                "button_04_label=NONE\n"
                "button_04_label=Waterfall\n",
                encoding="utf-8",
            )
            self.assertEqual(read_disabled_button_numbers(path), (2, 3))

    def test_derived_config_applies_final_private_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.conf"
            destination = root / "derived.conf"
            source.write_text(
                "serial_port = /dev/null\n"
                "pda_sleep_mode = yes\n"
                "mqtt_password = do-not-record\n",
                encoding="utf-8",
            )

            write_config_with_overrides(
                source,
                destination,
                {"pda_sleep_mode": "no"},
            )

            self.assertEqual(
                read_config_value(destination, "pda_sleep_mode"),
                "no",
            )
            self.assertNotIn(
                "pda_sleep_mode = no",
                source.read_text(encoding="utf-8"),
            )
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_resolves_wildcard_listen_address_to_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aqualinkd.conf"
            path.write_text(
                "listen_address=http://0.0.0.0:8080\n",
                encoding="utf-8",
            )
            self.assertEqual(
                resolve_api_base_url(path, None),
                "http://127.0.0.1:8080",
            )

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
