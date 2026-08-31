from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class PowerCenterCommand:
    arguments: tuple[str, ...]
    returncode: int
    duration_ms: float
    stdout: str
    stderr: str


@dataclass(frozen=True)
class PowerCenterPreparation:
    model: str
    port: str
    wine_version: str
    helper: Path
    helper_sha256: str
    initial_power: str
    final_power: str
    commands: tuple[PowerCenterCommand, ...]


class PowerCenterController(Protocol):
    """Prepare and verify an externally managed Jandy Power Center emulator."""

    def prepare(self, serial_device: Path) -> PowerCenterPreparation: ...

    def select_mode(self, mode: str) -> None: ...

    def set_temperature(self, sensor: str, value: int) -> None: ...
