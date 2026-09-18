from __future__ import annotations

import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aqualinkd_validator.adapters.power_center import PowerCenterAutomationError
from aqualinkd_validator.adapters.power_center_session import (
    ManagedWinePowerCenterSession,
)
from aqualinkd_validator.site_config import PowerCenterSiteConfig


class _FakeProcess:
    def __init__(self, *, stdout: str = "") -> None:
        self.returncode: int | None = None
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO("")

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def wait(self, timeout: float) -> int:
        del timeout
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class PowerCenterSessionTests(unittest.TestCase):
    def _config(self, root: Path) -> PowerCenterSiteConfig:
        installation = root / "drive_c/Program Files/Alwin32"
        installation.mkdir(parents=True)
        power_center = installation / "Pwrcntr.exe"
        simio = installation / "Simio.exe"
        helper = root / "pwrcntr-control.exe"
        for executable in (power_center, simio, helper):
            executable.touch()
        return PowerCenterSiteConfig(
            helper=helper,
            wine_prefix=root,
            model="B29231 (16 Combo)",
            port="COM3",
        )

    def test_headless_session_allocates_private_display_and_launches_both(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            xvfb = _FakeProcess(stdout="87\n")
            applications = [_FakeProcess(), _FakeProcess()]
            launched: list[list[str]] = []

            def popen(command: list[str], **_kwargs: object) -> _FakeProcess:
                launched.append(command)
                if command[0] == "/usr/bin/Xvfb":
                    return xvfb
                return applications.pop(0)

            completed = subprocess.CompletedProcess([], 0, "running", "")
            with (
                patch(
                    "aqualinkd_validator.adapters.power_center_session.shutil.which",
                    side_effect=lambda name: f"/usr/bin/{name}",
                ),
                patch(
                    "aqualinkd_validator.adapters.power_center_session.subprocess.Popen",
                    side_effect=popen,
                ),
                patch(
                    "aqualinkd_validator.adapters.power_center_session.subprocess.run",
                    return_value=completed,
                ),
                patch(
                    "aqualinkd_validator.adapters.power_center_session.select.select",
                    return_value=([xvfb.stdout], [], []),
                ),
                patch.object(
                    ManagedWinePowerCenterSession,
                    "_x11_socket_directory_is_usable",
                    return_value=True,
                ),
            ):
                session = ManagedWinePowerCenterSession(config)
                session.start()
                metadata = session.metadata
                session.close()

            self.assertEqual(metadata["display_mode"], "headless")
            self.assertEqual(metadata["display"], ":87")
            self.assertEqual(len(launched), 3)
            self.assertIn("Simio.exe", launched[1][-1])
            self.assertIn("Pwrcntr.exe", launched[2][-1])
            self.assertEqual(xvfb.returncode, -15)

    def test_desktop_session_requires_display(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            with patch.dict(os.environ, {}, clear=True):
                session = ManagedWinePowerCenterSession(
                    config,
                    display_mode="desktop",
                )
                with self.assertRaisesRegex(
                    PowerCenterAutomationError, "requires DISPLAY"
                ):
                    session.start()

    def test_same_wine_prefix_cannot_be_used_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            processes = [_FakeProcess(), _FakeProcess()]
            completed = subprocess.CompletedProcess([], 0, "running", "")
            with (
                patch.dict(os.environ, {"DISPLAY": ":1"}),
                patch(
                    "aqualinkd_validator.adapters.power_center_session.subprocess.Popen",
                    side_effect=lambda *_args, **_kwargs: processes.pop(0),
                ),
                patch(
                    "aqualinkd_validator.adapters.power_center_session.subprocess.run",
                    return_value=completed,
                ),
                patch(
                    "aqualinkd_validator.adapters.power_center_session.shutil.which",
                    return_value="/usr/bin/wineserver",
                ),
            ):
                first = ManagedWinePowerCenterSession(config, display_mode="desktop")
                second = ManagedWinePowerCenterSession(config, display_mode="desktop")
                first.start()
                try:
                    with self.assertRaisesRegex(
                        PowerCenterAutomationError,
                        "already in use",
                    ):
                        second.start()
                finally:
                    first.close()

    def test_parallel_sessions_allocate_different_tcp_displays(self) -> None:
        class FakeSocket:
            def __enter__(self) -> FakeSocket:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def bind(self, _address: tuple[str, int]) -> None:
                return None

        with (
            tempfile.TemporaryDirectory() as first_directory,
            tempfile.TemporaryDirectory() as second_directory,
            patch(
                "aqualinkd_validator.adapters.power_center_session.socket.socket",
                return_value=FakeSocket(),
            ),
        ):
            first = ManagedWinePowerCenterSession(
                self._config(Path(first_directory))
            )
            second = ManagedWinePowerCenterSession(
                self._config(Path(second_directory))
            )
            try:
                first_display = first._allocate_tcp_display()
                second_display = second._allocate_tcp_display()
            finally:
                first.close()
                second.close()

            self.assertNotEqual(first_display, second_display)


if __name__ == "__main__":
    unittest.main()
