from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO


class FileArtifactStore:
    """Persist one validator run beneath a filesystem directory."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def open_text(self, name: str) -> TextIO:
        return self._path(name).open("w", encoding="utf-8")

    def write_text(self, name: str, value: str) -> None:
        self._path(name).write_text(value, encoding="utf-8")

    def write_json(self, name: str, value: Any) -> None:
        self.write_text(name, json.dumps(value, indent=2, sort_keys=True) + "\n")

    def _path(self, name: str) -> Path:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"artifact name must stay beneath the run root: {name}")
        resolved = self._root / path
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved
