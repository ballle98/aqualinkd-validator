from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.config import (
    ConfigurationError,
    read_config_value,
    read_config_values,
    read_disabled_button_numbers,
    resolve_api_base_url,
    write_config_with_overrides,
)
from aqualinkd_validator.site_config import SiteConfigError, load_site_config


class ConfigTests(unittest.TestCase):
    def test_reads_repeatable_assignments_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aqualinkd.conf"
            path.write_text(
                "RSSD_LOG_filter = 0x60\n"
                "rssd_log_FILTER = '0x61'\n"
                "# RSSD_LOG_filter = 0x62\n",
                encoding="utf-8",
            )

            self.assertEqual(
                read_config_values(path, "RSSD_LOG_filter"),
                ("0x60", "0x61"),
            )

    def test_loads_site_specific_spa_fill_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aqualinkd_config = root / "aqualinkd.conf"
            aqualinkd_config.touch()
            (root / "aqualinkd-validator.yaml").write_text(
                "schema: 1\nspa:\n  fill_time: 8m\n",
                encoding="utf-8",
            )

            site = load_site_config(aqualinkd_config)

            self.assertEqual(site.spa.fill_seconds, 480.0)
            self.assertEqual(site.source, root / "aqualinkd-validator.yaml")

    def test_missing_implicit_site_config_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "aqualinkd.conf"
            config.touch()
            self.assertIsNone(load_site_config(config).source)

    def test_loads_power_center_automation_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "aqualinkd.conf"
            config.touch()
            helper = root / "pwrcntr-control.exe"
            helper.touch()
            prefix = root / "wine-prefix"
            prefix.mkdir()
            profile = root / "site.yaml"
            profile.write_text(
                "schema: 1\n"
                "power_center:\n"
                "  helper: pwrcntr-control.exe\n"
                "  wine_prefix: wine-prefix\n"
                '  model: "E260808 (PD 8 Combo)"\n'
                "  port: COM3\n"
                "  observation_time: 500ms\n",
                encoding="utf-8",
            )

            site = load_site_config(config, profile)

            assert site.power_center is not None
            self.assertEqual(site.power_center.helper, helper)
            self.assertEqual(site.power_center.wine_prefix, prefix)
            self.assertEqual(site.power_center.model, "E260808 (PD 8 Combo)")
            self.assertEqual(site.power_center.port, "COM3")
            self.assertEqual(site.power_center.observation_seconds, 0.5)

    def test_explicit_site_config_must_be_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "aqualinkd.conf"
            site_config = root / "site.yaml"
            config.touch()
            site_config.write_text(
                "schema: 1\nspa:\n  fill_time: eight minutes\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SiteConfigError, "duration such as 8m"):
                load_site_config(config, site_config)

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
