from __future__ import annotations

from pathlib import Path
from typing import Any, BinaryIO, Protocol, TextIO


class ArtifactStore(Protocol):
    """Run-scoped artifact persistence independent of filesystem layout."""

    @property
    def root(self) -> Path: ...

    def open_text(self, name: str) -> TextIO: ...

    def open_binary(self, name: str) -> BinaryIO: ...

    def write_text(self, name: str, value: str) -> None: ...

    def write_json(self, name: str, value: Any) -> None: ...
