from __future__ import annotations

import os
import pwd
import select
import subprocess
import termios
import time
import tty
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import sha256_file
from ..interfaces.power_center import (
    PowerCenterCommand,
    PowerCenterPreparation,
)
from ..site_config import PowerCenterSiteConfig


class PowerCenterAutomationError(RuntimeError):
    """Raised when Power Center cannot be configured or verified."""

    def __init__(
        self, message: str, commands: tuple[PowerCenterCommand, ...] = ()
    ) -> None:
        super().__init__(message)
        self.commands = commands


CommandExecutor = Callable[
    [list[str], dict[str, str], float], subprocess.CompletedProcess[str]
]
TrafficObserver = Callable[[Path, float], bool]


def _execute_command(
    command: list[str], environment: dict[str, str], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=timeout_seconds,
    )


def observe_serial_traffic(device: Path, seconds: float) -> bool:
    """Return whether fresh bytes arrive during one bounded observation window."""

    try:
        descriptor = os.open(device, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as error:
        raise PowerCenterAutomationError(
            f"cannot observe Power Center serial traffic on {device}: {error}"
        ) from error
    original: Any = None
    try:
        original = termios.tcgetattr(descriptor)
        tty.setraw(descriptor)
        while True:
            try:
                if not os.read(descriptor, 4096):
                    break
            except BlockingIOError:
                break
        ready, _, _ = select.select([descriptor], [], [], seconds)
        if not ready:
            return False
        return bool(os.read(descriptor, 4096))
    except OSError as error:
        raise PowerCenterAutomationError(
            f"failed while observing Power Center serial traffic on {device}: {error}"
        ) from error
    finally:
        if original is not None:
            termios.tcsetattr(descriptor, termios.TCSANOW, original)
        os.close(descriptor)


class WinePowerCenterController:
    """Drive Pwrcntr.exe menus under Wine and verify power through serial bytes."""

    def __init__(
        self,
        config: PowerCenterSiteConfig,
        *,
        execute: CommandExecutor = _execute_command,
        observe_traffic: TrafficObserver = observe_serial_traffic,
    ) -> None:
        self._config = config
        self._execute = execute
        self._observe_traffic = observe_traffic
        self._commands: list[PowerCenterCommand] = []
        self._environment = {**os.environ, "WINEPREFIX": str(config.wine_prefix)}
        self._wine_prefix = self._resolve_wine_command_prefix()

    def prepare(self, serial_device: Path) -> PowerCenterPreparation:
        wine_version = self._wine_version()
        self._helper("status")
        self._helper("model", self._config.model)
        self._helper("port", self._config.port)

        initial_on = self._observe_traffic(
            serial_device, self._config.observation_seconds
        )
        if initial_on:
            self._helper("power", "toggle")
            self._wait_for_power(serial_device, powered=False)
        self._helper("power", "toggle")
        self._wait_for_power(serial_device, powered=True)

        return PowerCenterPreparation(
            model=self._config.model,
            port=self._config.port,
            wine_version=wine_version,
            helper=self._config.helper,
            helper_sha256=sha256_file(self._config.helper),
            initial_power="on" if initial_on else "off",
            final_power="on",
            commands=tuple(self._commands),
        )

    def select_mode(self, mode: str) -> None:
        """Select and verify one Power Center operating mode."""

        if mode not in {"auto", "service", "timeout"}:
            raise ValueError(f"unsupported Power Center mode: {mode}")
        self._helper("mode", mode)

    @property
    def commands(self) -> tuple[PowerCenterCommand, ...]:
        """Return all helper commands issued during this run."""

        return tuple(self._commands)

    def _helper(self, *arguments: str) -> None:
        command = [
            *self._wine_prefix,
            self._config.wine,
            str(self._config.helper),
            *arguments,
        ]
        started = time.monotonic_ns()
        try:
            result = self._execute(
                command,
                self._environment,
                self._config.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PowerCenterAutomationError(
                f"Power Center helper failed to run {' '.join(arguments)}: {error}",
                tuple(self._commands),
            ) from error
        duration_ms = (time.monotonic_ns() - started) / 1_000_000
        record = PowerCenterCommand(
            arguments=tuple(arguments),
            returncode=result.returncode,
            duration_ms=round(duration_ms, 3),
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
        )
        self._commands.append(record)
        if result.returncode != 0:
            detail = record.stderr or record.stdout or "no diagnostic"
            raise PowerCenterAutomationError(
                f"Power Center helper {' '.join(arguments)} failed with "
                f"exit code {result.returncode}: {detail}",
                tuple(self._commands),
            )

    def _wine_version(self) -> str:
        try:
            result = self._execute(
                [*self._wine_prefix, self._config.wine, "--version"],
                self._environment,
                self._config.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PowerCenterAutomationError(
                f"cannot run Wine: {error}", tuple(self._commands)
            ) from error
        if result.returncode != 0:
            raise PowerCenterAutomationError(
                f"Wine version check failed: {result.stderr.strip()}",
                tuple(self._commands),
            )
        return result.stdout.strip()

    def _wait_for_power(self, serial_device: Path, *, powered: bool) -> None:
        deadline = time.monotonic() + self._config.power_timeout_seconds
        while time.monotonic() < deadline:
            observed = self._observe_traffic(
                serial_device, self._config.observation_seconds
            )
            if observed is powered:
                return
        state = "traffic" if powered else "serial silence"
        raise PowerCenterAutomationError(
            f"Power Center did not reach verified {state} on {serial_device} "
            f"within {self._config.power_timeout_seconds:g}s",
            tuple(self._commands),
        )

    def _resolve_wine_command_prefix(self) -> list[str]:
        owner = self._config.wine_prefix.stat().st_uid
        if os.geteuid() != 0 or owner == 0:
            return []
        try:
            user = pwd.getpwuid(owner).pw_name
        except KeyError as error:
            raise PowerCenterAutomationError(
                f"Wine prefix owner UID {owner} has no local account",
                tuple(self._commands),
            ) from error
        return [
            "runuser",
            "--user",
            user,
            "--",
            "env",
            f"WINEPREFIX={self._config.wine_prefix}",
        ]
