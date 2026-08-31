from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.adapters.power_center import (
    PowerCenterAutomationError,
    WinePowerCenterController,
)
from aqualinkd_validator.site_config import PowerCenterSiteConfig


class PowerCenterControllerTests(unittest.TestCase):
    def test_configures_and_power_cycles_running_emulator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "pwrcntr-control.exe"
            helper.write_bytes(b"helper")
            commands: list[list[str]] = []
            traffic = iter((True, False, True))

            def execute(
                command: list[str], environment: dict[str, str], timeout: float
            ) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                self.assertEqual(environment["WINEPREFIX"], str(root))
                self.assertGreater(timeout, 0)
                stdout = "wine-10.0\n" if command[-1] == "--version" else "selected\n"
                return subprocess.CompletedProcess(command, 0, stdout, "")

            controller = WinePowerCenterController(
                PowerCenterSiteConfig(
                    helper=helper,
                    wine_prefix=root,
                    model="E260808 (PD 8 Combo)",
                    port="COM3",
                ),
                execute=execute,
                observe_traffic=lambda _device, _seconds: next(traffic),
            )

            result = controller.prepare(Path("/dev/ttyVIRTUAL"))

            self.assertEqual(result.initial_power, "on")
            self.assertEqual(result.final_power, "on")
            self.assertEqual(result.wine_version, "wine-10.0")
            self.assertEqual(
                [record.arguments for record in result.commands],
                [
                    ("status",),
                    ("model", "E260808 (PD 8 Combo)"),
                    ("port", "COM3"),
                    ("power", "toggle"),
                    ("power", "toggle"),
                ],
            )
            self.assertEqual(len(commands), 6)

    def test_starts_panel_when_it_is_already_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "pwrcntr-control.exe"
            helper.write_bytes(b"helper")
            traffic = iter((False, True))

            def execute(
                command: list[str], _environment: dict[str, str], _timeout: float
            ) -> subprocess.CompletedProcess[str]:
                stdout = "wine-10.0" if command[-1] == "--version" else "selected"
                return subprocess.CompletedProcess(command, 0, stdout, "")

            result = WinePowerCenterController(
                PowerCenterSiteConfig(
                    helper=helper,
                    wine_prefix=root,
                    model="E260808 (PD 8 Combo)",
                    port="COM3",
                ),
                execute=execute,
                observe_traffic=lambda _device, _seconds: next(traffic),
            ).prepare(Path("/dev/ttyVIRTUAL"))

            self.assertEqual(result.initial_power, "off")
            self.assertEqual(
                [record.arguments for record in result.commands].count(
                    ("power", "toggle")
                ),
                1,
            )

    def test_failed_helper_selection_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "pwrcntr-control.exe"
            helper.write_bytes(b"helper")

            def execute(
                command: list[str], _environment: dict[str, str], _timeout: float
            ) -> subprocess.CompletedProcess[str]:
                if command[-1] == "--version":
                    return subprocess.CompletedProcess(command, 0, "wine-10.0", "")
                return subprocess.CompletedProcess(command, 5, "", "not found")

            controller = WinePowerCenterController(
                PowerCenterSiteConfig(
                    helper=helper,
                    wine_prefix=root,
                    model="missing",
                    port="COM3",
                ),
                execute=execute,
            )

            with self.assertRaisesRegex(
                PowerCenterAutomationError, "exit code 5: not found"
            ):
                controller.prepare(Path("/dev/ttyVIRTUAL"))

    def test_select_mode_uses_helper_and_records_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "pwrcntr-control.exe"
            helper.write_bytes(b"helper")
            commands: list[list[str]] = []

            def execute(
                command: list[str], _environment: dict[str, str], _timeout: float
            ) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                return subprocess.CompletedProcess(command, 0, "selected", "")

            controller = WinePowerCenterController(
                PowerCenterSiteConfig(
                    helper=helper,
                    wine_prefix=root,
                    model="B29231 (16 Combo)",
                    port="COM3",
                ),
                execute=execute,
            )

            controller.select_mode("service")
            controller.set_temperature("air", 34)

            self.assertEqual(commands[0][-2:], ["mode", "service"])
            self.assertEqual(controller.commands[0].arguments, ("mode", "service"))
            self.assertEqual(
                controller.commands[1].arguments,
                ("temperature", "air", "34"),
            )
            with self.assertRaisesRegex(ValueError, "unsupported"):
                controller.select_mode("invalid")
            with self.assertRaisesRegex(ValueError, "unsupported"):
                controller.set_temperature("invalid", 34)


if __name__ == "__main__":
    unittest.main()
